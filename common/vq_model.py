import os
import torch
# import horovod.torch as hvd
import torch.nn as nn
from torch.nn import functional as F
import sys
import math
import yaml
from einops import rearrange, repeat
import numpy as np
import torch.distributed.nn as distnn
sys.path.append('./')
# from nets.resnet import ResNetBackbone
from nets.module import Pose2Feat, Vposer, PositionalEncoding1D
from nets.loss import CoordLoss, ParamLoss, VQLoss
from utils.smpl import SMPL
from nets.transformer import bulid_transformer_encoder, PositionEmbeddingSine, bulid_transformer
from nets.infonce import AllGather, intra_info_nce_loss, inter_info_nce_loss
from utils.transforms import rot6d_to_axis_angle,rotation_matrix_to_angle_axis
from common.vqvae.pct_vqvae import PCT_VQVAE_MODEL
from common.vpd.vpdencoder import VPDEncoder
allgather = AllGather.apply

class VQ_Model(nn.Module):
    def __init__(self, backbone, vqvae, cfg):
        super(VQ_Model, self).__init__()
        self.backbone = backbone
        # self.pose2feat = pose2feat
        self.vqvae = vqvae
        self.ft_vposer = False
        self.cfg = cfg

        self.human_model = SMPL()
        self.human_model_layer = self.human_model.layer['neutral'].cuda()
        self.root_joint_idx = self.human_model.root_joint_idx
        self.mesh_face = self.human_model.face
        self.joint_regressor = self.human_model.joint_regressor

        self.coord_loss = CoordLoss()
        self.param_loss = ParamLoss()
        self.vq_loss = VQLoss()
        self.vqtokens = int(cfg.vqtokens)
        self.vqclasses = int(cfg.vqclasses)
        num_layers = cfg.dec_layers
        self.down_linear = nn.Conv2d(2048 , 256, 1, 1)
        self.transformer = bulid_transformer(
            d_model=256, 
            nhead=8,
            num_encoder_layers=cfg.enc_layers,
            num_decoder_layers=cfg.dec_layers, 
            dim_feedforward=2048, 
            dropout=0.1,
            activation="relu", 
            normalize_before=False,
            return_intermediate_dec=True,
            smpl_params=self.vqtokens+3
        )
        self.pos_embed = PositionEmbeddingSine(128, normalize=True)
        self.pos_embed_1d = PositionalEncoding1D()
        
        self.spose_shape_cam_param = nn.Parameter(torch.randn(self.vqtokens+3, 256).float())
        self.query = nn.Embedding(15, 256)

        self.cascade_root_pose_out = nn.ModuleList(
            [nn.Linear(256, 6)] + \
            [nn.Linear(256 + 6, 6) for _ in range(num_layers-1)]
        )
        self.cascade_pose_out = nn.ModuleList(
            [nn.Linear(256, self.vqclasses)] + \
            [nn.Linear(256 + self.vqclasses, self.vqclasses) for _ in range(num_layers-1)]
        )
        self.cascade_shaoe_out = nn.ModuleList(
            [nn.Linear(256, 10)] + \
            [nn.Linear(256 + 10, 10) for _ in range(num_layers-1)]
        )
        self.cascade_cam_out = nn.ModuleList(
            [nn.Linear(256, 3)] + \
            [nn.Linear(256 + 3, 3) for _ in range(num_layers-1)]
        )
        self.conv2d_to_3d = nn.Conv2d(2048, 256*8, 1, 1)
        self.exponent = nn.Parameter(torch.tensor(3, dtype=torch.float32))
        self.conv_3d_coord = nn.Sequential(
            nn.Conv3d(256 + 3, 256, 1, 1),
            # BasicBlock_3D(256, 256)
        ) 
        self.refine_3d = bulid_transformer_encoder(
            d_model=256,
            nhead=8,
            dim_feedforward=1024,
            dropout=0.1,
            activation="relu",
            num_encoder_layers=cfg.enc_layers,
            normalize_before=False,
        )
        
    
    def get_camera_trans(self, cam_param, meta_info, is_render,focal_length):
        # camera translation
        t_xy = cam_param[:,:2]
        gamma = torch.sigmoid(cam_param[:,2]) # apply sigmoid to make it positive
        k_value = torch.FloatTensor([math.sqrt(focal_length*focal_length*self.cfg.camera_3d_size*self.cfg.camera_3d_size/(self.cfg.input_img_shape[0]*self.cfg.input_img_shape[1]))]).cuda().view(-1)
        if is_render:
            bbox = meta_info['bbox']
            alphafactor = math.sqrt(self.cfg.input_img_shape[0]*self.cfg.input_img_shape[1]) / (bbox[:, 2]*bbox[:, 3]).sqrt().cuda()
            k_value = k_value * alphafactor
        t_z = k_value * gamma
        cam_trans = torch.cat((t_xy, t_z[:,None]),1)
        return cam_trans

    def make_2d_gaussian_heatmap(self, joint_coord_img):
        x = torch.arange(self.cfg.input_hm_shape[1])
        y = torch.arange(self.cfg.input_hm_shape[0])
        yy, xx = torch.meshgrid(y, x)
        xx = xx[None, None, :, :].cuda().float()
        yy = yy[None, None, :, :].cuda().float()

        x = joint_coord_img[:, :, 0, None, None]
        y = joint_coord_img[:, :, 1, None, None]
        heatmap = torch.exp(
            -(((xx - x) / self.cfg.sigma) ** 2) / 2 - (((yy - y) / self.cfg.sigma) ** 2) / 2)
        return heatmap

    def get_coord(self, smpl_pose, smpl_shape, smpl_trans, focal_length):
        batch_size = smpl_pose.shape[0]
        mesh_cam, _ = self.human_model_layer(smpl_pose, smpl_shape, smpl_trans) # B, 6890, 3
        # camera-centered 3D coordinate
        joint_cam = torch.bmm(torch.from_numpy(self.joint_regressor).cuda()[None,:,:].repeat(batch_size,1,1), mesh_cam) # B, 30, 3
        root_joint_idx = self.human_model.root_joint_idx

        # project 3D coordinates to 2D space
        x = joint_cam[:,:,0] / (joint_cam[:,:,2] + 1e-4) * focal_length + self.cfg.princpt[0]
        y = joint_cam[:,:,1] / (joint_cam[:,:,2] + 1e-4) * focal_length + self.cfg.princpt[1]
        x = x / self.cfg.input_img_shape[1] * self.cfg.output_hm_shape[2]
        y = y / self.cfg.input_img_shape[0] * self.cfg.output_hm_shape[1]
        joint_proj = torch.stack((x,y),2)

        mesh_cam_render = mesh_cam.clone()
        # root-relative 3D coordinates
        root_cam = joint_cam[:,root_joint_idx,None,:]
        joint_cam = joint_cam - root_cam
        mesh_cam = mesh_cam - root_cam
        return joint_proj, joint_cam, mesh_cam, mesh_cam_render

    def cascade_fc(self, hs, net_list):
        assert len(hs) == len(net_list)
        for i in range(len(hs)):
            if i == 0:
                out = net_list[i](hs[i])
            else:
                offset = net_list[i](torch.cat([hs[i],out],dim=-1))
                out = out + offset
        return out

    def get_relative_depth_anchour(self, k , map_size=8):
        range_arr = torch.arange(map_size, dtype=torch.float32, device=k.device) / map_size # (0, 1)
        Y_map = range_arr.reshape(1,1,1,map_size,1).repeat(1,1,map_size,1,map_size) 
        X_map = range_arr.reshape(1,1,1,1,map_size).repeat(1,1,map_size,map_size,1) 
        Z_map = torch.pow(range_arr, k)
        Z_map = Z_map.reshape(1,1,map_size,1,1).repeat(1,1,1,map_size,map_size) 
        return torch.cat([Z_map, Y_map, X_map], dim=1) # 1, 3, 8, 8, 8

    def forward(self, inputs, targets, meta_info, mode):
        with torch.no_grad():
            if self.training:
                mask_prop = torch.rand_like(inputs['joints_mask'])  # B, N, 1
                mask = mask_prop < 0.1 # 10% of joints are masked
                inputs['joints_mask'] = inputs['joints_mask'] * (1 - mask.float())
            joint_heatmap = self.make_2d_gaussian_heatmap(inputs['joints'].detach())
            # remove blob centered at (0,0) == invalid ones
            input_heatmap = joint_heatmap * inputs['joints_mask'][:,:,:,None]
        focal_length = float(meta_info["cliff_focal_length"][0]) if self.cfg.focal == None else self.cfg.focal[0]
        # img = self.backbone(inputs['img'], skip_early=False)    #B,64,64,64
        # img = self.pose2feat(img, input_heatmap)
        # img = self.backbone(img, skip_early=True)
        img = self.backbone(x = inputs['img'], control=input_heatmap) #B, 2048, 8, 8
        dense_feat = self.conv2d_to_3d(img)
        dense_feat = rearrange(dense_feat, 'b (c d) h w -> b c d h w', c=256, d=8)
        exponent = torch.clamp(self.exponent, 1, 20)
        relative_depth_anchour = self.get_relative_depth_anchour(exponent)
        cam_anchour_maps = repeat(relative_depth_anchour, 'n c d h w -> (b n) c d h w', b=dense_feat.size(0))
        dense_feat = torch.cat([dense_feat, cam_anchour_maps], dim=1)
        dense_feat = self.conv_3d_coord(dense_feat)
        dense_feat = rearrange(dense_feat, 'b c d h w -> (d h w) b c', c=256, d=8).contiguous()
        pos_3d = repeat(self.pos_embed_1d.pos_table, 'n c -> n b c', b=inputs['img'].size(0))
        dense_feat = self.refine_3d(dense_feat, pos=pos_3d)
        dense_feat = rearrange(dense_feat, '(d h w) b c -> b c d h w', d=8, h=8, w=8).contiguous()
        img = self.down_linear(img)
        with torch.no_grad():
            img_pos = self.pos_embed(img) # B, C, H, W
            src_masks = None # B, H, W
        query_pos = self.query.weight
        query_pos = torch.cat([self.spose_shape_cam_param, query_pos], dim=0)
        query_pos = repeat(query_pos, 'n c -> n b c', b=inputs['img'].shape[0])
        img = rearrange(img, 'b c h w -> (h w) b c')
        img_pos = rearrange(img_pos, 'b c h w -> (h w) b c')

        hs, joint_img = self.transformer(img, src_masks, query_pos, img_pos, dense_feat, exponent) 

        hs = rearrange(hs, 'l n b c -> l b n c')
        pose_token, shape_token, cam_token = hs[:,:,0], hs[:,:,-2], hs[:,:,-1]# L, B, C (average over joints)
        vq_token = hs[:,:,1:-2]

        L, B, _= pose_token.size()
        root_pose_6d = self.cascade_fc(pose_token, self.cascade_root_pose_out)
        vq_param = self.cascade_fc(vq_token, self.cascade_pose_out)
        shape_param = self.cascade_fc(shape_token, self.cascade_shaoe_out)
        cam_param = self.cascade_fc(cam_token, self.cascade_cam_out)
        # print(vq_param.shape)
        vq_param = vq_param.reshape(-1, vq_param.shape[-1])
        logit_softmax = vq_param.clone().softmax(1)   #need B*N, 2048
        # print(logit_softmax.shape)
        L = 1

        root_pose = rot6d_to_axis_angle(root_pose_6d)
        if self.training and self.ft_vposer == False:
            self.vqvae.eval()
        if self.training == False:
            self.vqvae.eval()
        pose_param = self.vqvae.get_decode_pose(logit_softmax)
        # print(pose_param.shape)
        pose_param = rotation_matrix_to_angle_axis(pose_param.reshape(-1,3,3))
        cam_trans = self.get_camera_trans(cam_param, meta_info, is_render=(self.cfg.render and (mode == 'test')),focal_length=focal_length)
        pose_param = pose_param.view(-1, self.human_model.orig_joint_num - 1, 3)

        pose_param = torch.cat((root_pose[:, None, :], pose_param), 1).view(-1, self.human_model.orig_joint_num * 3)
        joint_proj, joint_cam, mesh_cam, mesh_cam_render = self.get_coord(pose_param, shape_param, cam_trans,focal_length=focal_length)
        
        if mode == 'train':
            # loss functions
            joint_img = joint_img.reshape((L, B) + joint_img.shape[1:])
            pose_param = pose_param.reshape((L, B) + pose_param.shape[1:])
            shape_param = shape_param.reshape((L, B) + shape_param.shape[1:])
            joint_proj = joint_proj.reshape((L, B) + joint_proj.shape[1:]) # 2D kp
            joint_cam = joint_cam.reshape((L, B) + joint_cam.shape[1:]) # 3D kp
            # gt_latent = self.vqvae.get_first_latent_code(targets['pose_param'][:,3:])

            
            loss = {}
            reduced_orig_joint_img = self.human_model.reduce_joint_set(targets['orig_joint_img'])
            reduced_orig_joint_trunc = self.human_model.reduce_joint_set(meta_info['orig_joint_trunc'])
            reduced_fit_joint_img = self.human_model.reduce_joint_set(targets['fit_joint_img'])
            reduced_fit_joint_trunc = self.human_model.reduce_joint_set(meta_info['fit_joint_trunc'])

            for layer_idx in range(L):
                loss['body_joint_img_{}'.format(layer_idx)] = (1/64) * self.coord_loss(
                    joint_img[layer_idx]*64, 
                     reduced_orig_joint_img,
                    reduced_orig_joint_trunc, 
                    meta_info['is_3D'])
                loss['smpl_joint_img_{}'.format(layer_idx)] = (1/64) * self.coord_loss(
                    joint_img[layer_idx]*64, 
                    reduced_fit_joint_img,
                    reduced_fit_joint_trunc * meta_info['is_valid_fit'][:, None, None])
                loss['smpl_pose_{}'.format(layer_idx)] = self.param_loss(pose_param[layer_idx], targets['pose_param'], meta_info['fit_param_valid'] * meta_info['is_valid_fit'][:, None])
                loss['smpl_shape_{}'.format(layer_idx)] =  self.param_loss(shape_param[layer_idx], targets['shape_param'], meta_info['is_valid_fit'][:, None])
                loss['body_joint_proj_{}'.format(layer_idx)] = (1/8) * self.coord_loss(joint_proj[layer_idx], targets['orig_joint_img'][:, :, :2], meta_info['orig_joint_trunc'])
                loss['body_joint_cam_{}'.format(layer_idx)] = 5 * self.coord_loss(joint_cam[layer_idx], targets['orig_joint_cam'], meta_info['orig_joint_valid'] * meta_info['is_3D'][:, None, None])
                # loss['vqvae_ce_{}'.format(layer_idx)] = 0.5* self.vq_loss(gt_latent, vq_param,self.vqclasses, meta_info['is_valid_fit'])
            return loss

        else:
            _,_,_,gt_mesh_render = self.get_coord(targets['smpl_pose'],
                                                  targets['smpl_shape'],
                                                  cam_trans,
                                                  focal_length)
            # test output
            out = {'cam_param': cam_param}
            # out['input_joints'] = joint_coord_img
            out['joint_img'] = joint_img
            out['joint_proj'] = joint_proj
            # out['joint_score'] = joint_score
            out['smpl_mesh_cam'] = mesh_cam
            out['smpl_pose'] = pose_param
            out['smpl_shape'] = shape_param
            out['joints_mask'] = inputs['joints_mask']
            out['input_hm'] = input_heatmap
            # out['focal_length'] = focal_length

            out['mesh_cam_render'] = mesh_cam_render

            if 'smpl_mesh_cam' in targets:
                out['smpl_mesh_cam_target'] = targets['smpl_mesh_cam']
            if 'smpl_mesh_cam_render' in targets:
                out['smpl_mesh_cam_render'] = gt_mesh_render
            if 'bb2img_trans' in meta_info:
                out['bb2img_trans'] = meta_info['bb2img_trans']
            if 'img2bb_trans' in meta_info:
                out['img2bb_trans'] = meta_info['img2bb_trans']
            if 'bbox' in meta_info:
                out['bbox'] = meta_info['bbox']
            if 'tight_bbox' in meta_info:
                out['tight_bbox'] = meta_info['tight_bbox']
            if 'aid' in meta_info:
                out['aid'] = meta_info['aid']
            if 'kp3d_gt' in targets:
                out['kp3d_gt'] = targets['kp3d_gt']
            if 'kp2d_gt' in targets:
                out['kp2d_gt'] = targets['kp2d_gt']

            return out

def mlp(in_feat, out_feat, layers):
    net = []
    for _ in range(layers):
        net.append(nn.Linear(in_feat, in_feat))
        net.append(nn.ReLU())
    net.append(nn.Linear(in_feat, out_feat))
    return nn.Sequential(*net)

def init_weights(m):
    if type(m) == nn.ConvTranspose2d:
        nn.init.normal_(m.weight,std=0.001)
    elif type(m) == nn.Conv2d:
        nn.init.normal_(m.weight,std=0.001)
        nn.init.constant_(m.bias, 0)
    elif type(m) == nn.BatchNorm2d:
        nn.init.constant_(m.weight,1)
        nn.init.constant_(m.bias,0)
    elif type(m) == nn.Linear:
        nn.init.normal_(m.weight,std=0.01)
        nn.init.constant_(m.bias,0)

def get_model(joint_num, mode, cfg):

    # backbone = ResNetBackbone(cfg.resnet_type)
    # pose2feat = Pose2Feat(joint_num,feat_dim=64)
    # vposer = Vposer()
    cldm = VPDEncoder()
    vqcfgpath = os.path.join("common/vqvae/config/pct_vqvae.yaml")
    with open(vqcfgpath, 'r', encoding='utf-8') as f:
        vqcfgfile = f.read()
    vqconfig = yaml.load(vqcfgfile,Loader=yaml.FullLoader)
    vqvae = PCT_VQVAE_MODEL(vqconfig,"common/vqvae/models/256x2048x48.pth")
    for param in vqvae.parameters():
        param.requires_grad = False

    # if mode == 'train':
    #     pose2feat.apply(init_weights)
   
    model = VQ_Model(backbone=cldm, vqvae=vqvae, cfg=cfg)
    return model

