"""
FID, IS, JS divergence.
"""
import os
from typing import List, Union

import torch
import torch.nn.functional as F
import torch.nn as nn
import wandb
import numpy as np
import matplotlib.pyplot as plt
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE

from experiments.exp_stage2 import ExpStage2
from generators.maskgit import MaskGIT
from preprocessing.data_pipeline import build_data_pipeline
from preprocessing.preprocess_ucr import DatasetImporterUCR, DatasetImporterCustom
from generators.sample import unconditional_sample, conditional_sample
from supervised_FCN_2.example_pretrained_model_loading import load_pretrained_FCN
from supervised_FCN_2.example_compute_FID import calculate_fid
from supervised_FCN_2.example_compute_IS import calculate_inception_score
from utils import time_to_timefreq, timefreq_to_time
from generators.fidelity_enhancer import FidelityEnhancer
from evaluation.rocket_functions import generate_kernels, apply_kernels
from utils import zero_pad_low_freq, zero_pad_high_freq, remove_outliers


class Evaluation(nn.Module):
    """
    - FID
    - IS
    - visual inspection
    - PCA
    - t-SNE
    """
    def __init__(self, 
                 subset_dataset_name: str, 
                 input_length:int, 
                 n_classes:int, 
                 gpu_device_index:int, 
                 config:dict, 
                 use_fidelity_enhancer:bool=False,
                 feature_extractor_type:str='rocket',
                 rocket_num_kernels:int=1000,
                 use_custom_dataset:bool=False
                 ):
        super().__init__()
        self.subset_dataset_name = dataset_name = subset_dataset_name
        self.device = torch.device(gpu_device_index)
        self.config = config
        self.batch_size = self.config['evaluation']['batch_size']
        self.feature_extractor_type = feature_extractor_type
        assert feature_extractor_type in ['supervised_fcn', 'rocket'], 'unavailable feature extractor type.'

        if not use_custom_dataset:
            self.fcn = load_pretrained_FCN(subset_dataset_name).to(gpu_device_index)
            self.fcn.eval()
        if feature_extractor_type == 'rocket':
            self.rocket_kernels = generate_kernels(input_length, num_kernels=rocket_num_kernels)

        # load the numpy matrix of the test samples
        dataset_importer = DatasetImporterUCR(dataset_name, data_scaling=True) if not use_custom_dataset else DatasetImporterCustom()
        self.X_train = dataset_importer.X_train
        self.X_test = dataset_importer.X_test
        self.Y_train = dataset_importer.Y_train
        self.Y_test = dataset_importer.Y_test

        self.ts_len = self.X_train.shape[-1]  # time series length
        self.n_classes = len(np.unique(dataset_importer.Y_train))

        # load the stage2 model
        self.stage2 = ExpStage2.load_from_checkpoint(os.path.join('saved_models', f'stage2-{dataset_name}.ckpt'), 
                                                      dataset_name=dataset_name, 
                                                      input_length=input_length, 
                                                      config=config,
                                                      n_classes=n_classes,
                                                      use_fidelity_enhancer=False,
                                                      feature_extractor_type=feature_extractor_type,
                                                      use_custom_dataset=use_custom_dataset,
                                                      map_location='cpu',
                                                      strict=False)
        self.stage2.eval()
        self.maskgit = self.stage2.maskgit
        self.stage1 = self.stage2.maskgit.stage1

        # load the fidelity enhancer
        if use_fidelity_enhancer:
            self.fidelity_enhancer = FidelityEnhancer(self.ts_len, 1, config)
            fname = f'fidelity_enhancer-{dataset_name}.ckpt'
            ckpt_fname = os.path.join('saved_models', fname)
            self.fidelity_enhancer.load_state_dict(torch.load(ckpt_fname))
        else:
            self.fidelity_enhancer = nn.Identity()

        # fit PCA on a training set
        self.pca = PCA(n_components=2, random_state=0)
        self.z_train = self.compute_z('train')
        self.z_test = self.compute_z('test')

        z_train = remove_outliers(self.z_train)  # only used to fit pca because `def fid_score` already contains `remove_outliers`
        z_transform_pca = self.pca.fit_transform(z_train)

        self.xmin_pca, self.xmax_pca = np.min(z_transform_pca[:,0]), np.max(z_transform_pca[:,0])
        self.ymin_pca, self.ymax_pca = np.min(z_transform_pca[:,1]), np.max(z_transform_pca[:,1])

    @torch.no_grad()
    def sample(self, n_samples: int, kind: str, class_index:Union[int,None]=None):
        assert kind in ['unconditional', 'conditional']

        # sampling
        if kind == 'unconditional':
            x_new_l, x_new_h, x_new = unconditional_sample(self.maskgit, n_samples, self.device, batch_size=self.batch_size)  # (b c l); b=n_samples, c=1 (univariate)
        elif kind == 'conditional':
            x_new_l, x_new_h, x_new = conditional_sample(self.maskgit, n_samples, self.device, class_index, self.batch_size)  # (b c l); b=n_samples, c=1 (univariate)
        else:
            raise ValueError

        # FE
        num_batches = x_new.shape[0] // self.batch_size + (1 if x_new.shape[0] % self.batch_size != 0 else 0)
        X_new_R = []
        for i in range(num_batches):
            start_idx = i * self.batch_size
            end_idx = start_idx + self.batch_size
            mini_batch = x_new[start_idx:end_idx]
            x_new_R = self.fidelity_enhancer(mini_batch.to(self.device)).cpu()
            X_new_R.append(x_new_R)
        X_new_R = torch.cat(X_new_R)

        return (x_new_l, x_new_h, x_new), X_new_R

    def _extract_feature_representations(self, x:np.ndarray):
        """
        x: (b 1 l)
        """
        if self.feature_extractor_type == 'supervised_fcn':
            z = self.fcn(torch.from_numpy(x).float().to(self.device), return_feature_vector=True).cpu().detach().numpy()  # (b d)
        elif self.feature_extractor_type == 'rocket':
            x = x[:,0,:]  # (b l)
            z = apply_kernels(x, self.rocket_kernels)
        else:
            raise ValueError
        return z

    def compute_z_rec(self, kind:str):
        """
        compute representations of X_rec
        """
        assert kind in ['train', 'test']
        if kind == 'train':
            X = self.X_train  # (b 1 l)
        elif kind == 'test':
            X = self.X_test  # (b 1 l)
        else:
            raise ValueError
        
        n_samples = X.shape[0]
        n_iters = n_samples // self.batch_size
        if n_samples % self.batch_size > 0:
            n_iters += 1

        # get feature vectors from `X_test`
        zs = []
        for i in range(n_iters):
            s = slice(i * self.batch_size, (i + 1) * self.batch_size)
            x = X[s]  # (b 1 l)
            x = torch.from_numpy(x).float().to(self.device)
            x_rec = self.stage1.forward(batch=(x, None), batch_idx=-1, return_x_rec=True).cpu().detach().numpy().astype(float)  # (b 1 l)
            z_t = self._extract_feature_representations(x_rec)
            zs.append(z_t)
        zs = np.concatenate(zs, axis=0)
        return zs

    @torch.no_grad()
    def compute_z_svq(self, kind:str):
        """
        compute representations of X', a stochastic variant of X with SVQ
        """
        assert kind in ['train', 'test']
        if kind == 'train':
            X = self.X_train  # (b 1 l)
        elif kind == 'test':
            X = self.X_test  # (b 1 l)
        else:
            raise ValueError
        
        n_samples = X.shape[0]
        n_iters = n_samples // self.batch_size
        if n_samples % self.batch_size > 0:
            n_iters += 1

        # get feature vectors from `X_test`
        zs = []
        xs_a = []
        for i in range(n_iters):
            s = slice(i * self.batch_size, (i + 1) * self.batch_size)
            x = X[s]  # (b 1 l)
            x = torch.from_numpy(x).float().to(self.device)
            
            # x_rec = self.stage1.forward(batch=(x, None), batch_idx=-1, return_x_rec=True).cpu().detach().numpy().astype(float)  # (b 1 l)
            # svq_temp_rng = self.config['fidelity_enhancer']['svq_temp_rng']
            # svq_temp = np.random.uniform(*svq_temp_rng)
            # tau = self.config['fidelity_enhancer']['tau']
            tau = self.fidelity_enhancer.tau.item()
            _, s_a_l = self.maskgit.encode_to_z_q(x, self.stage1.encoder_l, self.stage1.vq_model_l, zero_pad_high_freq, svq_temp=tau)  # (b n)
            _, s_a_h = self.maskgit.encode_to_z_q(x, self.stage1.encoder_h, self.stage1.vq_model_h, zero_pad_low_freq, svq_temp=tau)  # (b m)
            x_a_l = self.maskgit.decode_token_ind_to_timeseries(s_a_l, 'lf')  # (b 1 l)
            x_a_h = self.maskgit.decode_token_ind_to_timeseries(s_a_h, 'hf')  # (b 1 l)
            x_a = x_a_l + x_a_h  # (b c l)
            x_a = x_a.cpu().numpy().astype(float)
            xs_a.append(x_a)

            z_t = self._extract_feature_representations(x_a)
            zs.append(z_t)
        zs = np.concatenate(zs, axis=0)
        xs_a = np.concatenate(xs_a, axis=0)
        return zs, xs_a

    def compute_z(self, kind: str) -> np.ndarray:
        """
        It computes representation z given input x
        :param X_gen: generated X
        :return: z_test (z on X_test), z_gen (z on X_generated)
        """
        assert kind in ['train', 'test']
        if kind == 'train':
            X = self.X_train  # (b 1 l)
        elif kind == 'test':
            X = self.X_test  # (b 1 l)
        else:
            raise ValueError

        n_samples = X.shape[0]
        n_iters = n_samples // self.batch_size
        if n_samples % self.batch_size > 0:
            n_iters += 1

        # get feature vectors from `X_test`
        zs = []
        for i in range(n_iters):
            s = slice(i * self.batch_size, (i + 1) * self.batch_size)
            z_t = self._extract_feature_representations(X[s])
            zs.append(z_t)
        zs = np.concatenate(zs, axis=0)
        return zs

    def compute_z_gen(self, X_gen: torch.Tensor) -> np.ndarray:
        """
        It computes representation z given input x
        :param X_gen: generated X
        :return: z_test (z on X_test), z_gen (z on X_generated)
        """
        n_samples = X_gen.shape[0]
        n_iters = n_samples // self.batch_size
        if n_samples % self.batch_size > 0:
            n_iters += 1

        # get feature vectors from `X_gen`
        z_gen = []
        for i in range(n_iters):
            s = slice(i * self.batch_size, (i + 1) * self.batch_size)

            # z_g = self.fcn(X_gen[s].float().to(self.device), return_feature_vector=True).cpu().detach().numpy()
            z_g = self._extract_feature_representations(X_gen[s].numpy().astype(float))

            z_gen.append(z_g)
        z_gen = np.concatenate(z_gen, axis=0)
        return z_gen

    def fid_score(self, z1:np.ndarray, z2:np.ndarray) -> int:
        z1, z2 = remove_outliers(z1), remove_outliers(z2)
        fid = calculate_fid(z1, z2)
        return fid

    def inception_score(self, X_gen: torch.Tensor):
        # assert self.X_test.shape[0] == X_gen.shape[0], "shape of `X_test` must be the same as that of `X_gen`."

        n_samples = self.X_test.shape[0]
        n_iters = n_samples // self.batch_size
        if n_samples % self.batch_size > 0:
            n_iters += 1

        # get the softmax distribution from `X_gen`
        p_yx_gen = []
        for i in range(n_iters):
            s = slice(i * self.batch_size, (i + 1) * self.batch_size)

            p_yx_g = self.fcn(X_gen[s].float().to(self.device))  # p(y|x)
            p_yx_g = torch.softmax(p_yx_g, dim=-1).cpu().detach().numpy()

            p_yx_gen.append(p_yx_g)
        p_yx_gen = np.concatenate(p_yx_gen, axis=0)

        IS_mean, IS_std = calculate_inception_score(p_yx_gen)
        return IS_mean, IS_std

    def log_visual_inspection(self, X1, X2, title: str, ylim: tuple = (-5, 5), n_plot_samples:int=200, alpha:float=0.1):
        # `X_test`
        sample_ind = np.random.randint(0, X1.shape[0], n_plot_samples)
        fig, axes = plt.subplots(2, 1, figsize=(4, 4))
        plt.suptitle(title)
        for i in sample_ind:
            axes[0].plot(X1[i, 0, :], alpha=alpha, color='C0')
        # axes[0].set_xticks([])
        axes[0].set_ylim(*ylim)
        # axes[0].set_title('test samples')

        # `X_gen`
        sample_ind = np.random.randint(0, X2.shape[0], n_plot_samples)
        for i in sample_ind:
            axes[1].plot(X2[i, 0, :], alpha=alpha, color='C0')
        axes[1].set_ylim(*ylim)
        # axes[1].set_title('generated samples')

        plt.tight_layout()
        wandb.log({f"visual comp ({title})": wandb.Image(plt)})
        plt.close()

    # def log_pca(self, n_plot_samples: int, Z1: np.ndarray, Z2: np.ndarray, labels):
    #     # sample_ind_test = np.random.choice(range(self.X_test.shape[0]), size=n_plot_samples, replace=True)
    #     ind1 = np.random.choice(range(Z1.shape[0]), size=n_plot_samples, replace=True)
    #     ind2 = np.random.choice(range(Z2.shape[0]), size=n_plot_samples, replace=True)

    #     # PCA: latent space
    #     # pca = PCA(n_components=2, random_state=0)
    #     Z1_embed = self.pca.transform(Z1[ind1])
    #     Z2_embed = self.pca.transform(Z2[ind2])

    #     plt.figure(figsize=(4, 4))
    #     # plt.title("PCA in the representation space by the trained encoder");
    #     plt.scatter(Z1_embed[:, 0], Z1_embed[:, 1], alpha=0.1, label=labels[0])
    #     plt.scatter(Z2_embed[:, 0], Z2_embed[:, 1], alpha=0.1, label=labels[1])
    #     plt.legend()
    #     plt.tight_layout()
    #     wandb.log({f"PCA on Z ({labels[0]} vs  {labels[1]})": wandb.Image(plt)})
    #     plt.close()

    def log_pca(self, Zs:List[np.ndarray], labels:List[str], n_plot_samples:int=1000):
        assert len(Zs) == len(labels)

        plt.figure(figsize=(4, 4))

        for Z, label in zip(Zs, labels):
            ind = np.random.choice(range(Z.shape[0]), size=n_plot_samples, replace=True)
            Z_embed = self.pca.transform(Z[ind])
            
            plt.scatter(Z_embed[:, 0], Z_embed[:, 1], alpha=0.1, label=label)
            
            xpad = (self.xmax_pca - self.xmin_pca) * 0.02
            ypad = (self.ymax_pca - self.ymin_pca) * 0.02
            plt.xlim(self.xmin_pca-xpad, self.xmax_pca+xpad)
            plt.ylim(self.ymin_pca-ypad, self.ymax_pca+ypad)

        plt.legend(loc='upper right')
        plt.tight_layout()
        wandb.log({f"PCA on Z ({labels})": wandb.Image(plt)})
        plt.close()

    def log_tsne(self, n_plot_samples: int, X_gen, z_test: np.ndarray, z_gen: np.ndarray):
        X_gen = F.interpolate(X_gen, size=self.X_test.shape[-1], mode='linear', align_corners=True)
        X_gen = X_gen.cpu().numpy()

        sample_ind_test = np.random.randint(0, self.X_test.shape[0], n_plot_samples)
        sample_ind_gen = np.random.randint(0, X_gen.shape[0], n_plot_samples)

        # TNSE: data space
        X = np.concatenate((self.X_test.squeeze()[sample_ind_test], X_gen.squeeze()[sample_ind_gen]), axis=0).squeeze()
        labels = np.array(['C0'] * len(sample_ind_test) + ['C1'] * len(sample_ind_gen))
        X_embedded = TSNE(n_components=2, learning_rate='auto', init='random').fit_transform(X)

        plt.figure(figsize=(4, 4))
        plt.scatter(X_embedded[:, 0], X_embedded[:, 1], c=labels, alpha=0.1)
        # plt.legend()
        plt.tight_layout()
        wandb.log({"TNSE-data_space": wandb.Image(plt)})
        plt.close()

        # TNSE: latent space
        Z = np.concatenate((z_test[sample_ind_test], z_gen[sample_ind_gen]), axis=0).squeeze()
        labels = np.array(['C0'] * len(sample_ind_test) + ['C1'] * len(sample_ind_gen))
        Z_embedded = TSNE(n_components=2, learning_rate='auto', init='random').fit_transform(Z)

        plt.figure(figsize=(4, 4))
        plt.scatter(Z_embedded[:, 0], Z_embedded[:, 1], c=labels, alpha=0.1)
        # plt.legend()
        plt.tight_layout()
        wandb.log({"TSNE-latent_space": wandb.Image(plt)})
        plt.close()
