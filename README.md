# DPMesh: Exploiting Diffusion Prior for Occluded Human Mesh Recovery
*Yixuan Zhu\*, Ao Li\*, Yansong Tang†, Wenliang Zhao, Jie Zhou, Jiwen Lu*
----
The repository contains the official implementation for the paper "DPMesh: Exploiting Diffusion Prior for Occluded Human Mesh Recovery" (CVPR 2024).

DPMesh is an innovative framework for occluded human <ins>**Mesh**</ins> recovery that capitalizes on the profound <ins>**D**</ins>iffusion <ins>**P**</ins>rior about object structure and spatial relationships embedded in a pre-trained text-to-image diffusion model.
## 📋 To-Do List
* [ ] Environment Settings.
* [ ] Release model and inference code.

## ⭐️ Pipeline

![](./assets/pipeline.png)

## ⭐️ Performance

![](./assets/performance.png)
![](./assets/table.png)

## 🚪Quick Start
### ⚙️ 1. Installation
``` bash
conda env create -f environment.yaml
conda activate dpmesh
```
### 2. Data Preparation

**For evaluation only, you can just prepare 3DPW dataset.**


### 3. Download Checkpoints

Please download our pretrained checkpoints from [this link](https://cloud.tsinghua.edu.cn/d/1d6cd3ee30204bb59fce/) and put them under `./checkpoints`.

### 4. Evaluation


## 🫰 Acknowledgments

We would like to express our sincere thanks to the author of [JOTR](https://github.com/xljh0520/JOTR) for the clear code base and quick response to our issues. 

We also thank [ControlNet](https://github.com/lllyasviel/ControlNet), [VPD](https://github.com/wl-zhao/VPD) and [LoRA](https://github.com/cloneofsimo/lora), for our code is partially borrowing from them.

## 🔖 Citation

## 🔑 License

This code is distributed under an [MIT LICENSE](./LICENSE).
