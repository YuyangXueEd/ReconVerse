from argparse import ArgumentParser
from typing import Any
import torch
import numpy as np
from torch.nn import functional as F
from collections import defaultdict

from .mri_module import MriModule

import sys
sys.path.append('../')
from models.unet import Unet
from utils import evaluate
from utils.complex import complex_abs

class UnetModule(MriModule):
    def __init__(
        self,
        in_chans: int = 1,
        out_chans: int = 1,
        chans: int = 32,
        num_pool_layers: int = 4,
        drop_prob: float = 0.0,
        lr: float = 1e-3,
        lr_step_size: int = 40,
        lr_gamma: float = 0.1,
        weight_decay: float = 0.0,
        **kwargs
    ):
        """_summary_

        Args:
            in_chans (int, optional): _description_. Defaults to 1.
            out_chans (int, optional): _description_. Defaults to 1.
            chans (int, optional): _description_. Defaults to 32.
            num_pool_layers (int, optional): _description_. Defaults to 4.
            drop_prob (float, optional): _description_. Defaults to 0.0.
            lr (float, optional): _description_. Defaults to 1e-3.
            lr_step_size (int, optional): _description_. Defaults to 40.
            lr_gamma (float, optional): _description_. Defaults to 0.1.
            weight_decay (float, optional): _description_. Defaults to 0.0.
        """
        super().__init__(**kwargs)
        
        self.save_hyperparameters()
        
        self.in_chans = in_chans
        self.out_chans = out_chans
        self.chans = chans
        self.num_pool_layers = num_pool_layers
        self.drop_prob = drop_prob
        self.lr = lr
        self.lr_step_size = lr_step_size
        self.lr_gamma = lr_gamma
        self.weight_decay = weight_decay
        
        self.unet = Unet(
            in_chans=self.in_chans,
            out_chans=self.out_chans,
            chans=self.chans,
            num_pool_layers=self.num_pool_layers,
            drop_prob=self.drop_prob,
        )
        
    def forward(self, image):
        if image.ndim == 3:
            return self.unet(image.unsqueeze(1)).squeeze(1)
        else:
            return self.unet(image.permute(0, 3, 1, 2)).permute(0, 2, 3, 1)
			
    def _post_process(self, output, mean, std):
        mean = mean.unsqueeze(1).unsqueeze(2).unsqueeze(3)
        std = std.unsqueeze(1).unsqueeze(2).unsqueeze(3)  
        return complex_abs(output * std + mean)
    
    def training_step(self, batch, batch_idx):
        output = self(batch.image)
        loss = F.l1_loss(output, batch.target)
        
        self.log("train_loss", loss.detach())
        
        return loss
    
    def log_image(self, name, image):
        self.logger.experiment.add_image(name, image, self.global_step)
        
    
    def validation_step(self, batch, batch_idx):
        output = self(batch.image)
        if output.ndim == 4:
            post_image = self._post_process(batch.image, batch.mean, batch.std)
            post_output = self._post_process(output, batch.mean, batch.std)
            post_target = self._post_process(batch.target, batch.mean, batch.std)
            val_logs = {
            "batch_idx": batch_idx,
            "image": post_image,
            "fname": batch.fname,
            "slice_num": batch.slice_num,
            "max_value": batch.max_value,
            "output": post_output,
            "target": post_target,
            "val_loss": F.l1_loss(output, batch.target)
        }
        else:
            mean = batch.mean.unsqueeze(1).unsqueeze(2)
            std = batch.std.unsqueeze(1).unsqueeze(2)
    
            val_logs = {
	            "image": batch.image * std + mean,
                "batch_idx": batch_idx,
                "fname": batch.fname,
                "slice_num": batch.slice_num,
                "max_value": batch.max_value,
	            "output": output * std + mean,
	            "target": batch.target * std + mean,
                "val_loss": F.l1_loss(output, batch.target)
            }
        
        for k in ("batch_idx", "fname", "slice_num", "max_value", "output", "target", "val_loss"):
            assert k in val_logs, f"Missing {k} in val_logs"
        
        print(val_logs["output"].ndim)
        if val_logs["output"].ndim == 2:
            val_logs["output"] = val_logs["output"]
        elif val_logs["output"].ndim != 3:
            raise ValueError(f"Unexpected output size from validation step {val_logs['output'].shape}")
        
        if val_logs["target"].ndim == 2:
            val_logs["target"] = val_logs["target"]
        elif val_logs["target"].ndim != 3:
            raise ValueError(f"Unexpected target size from validation step {val_logs['target'].shape}")
        
        # * pick an image to log
        if self.val_log_indices is None:
            self.val_log_indices = list(np.random.permutation(len(self.trainer.val_dataloaders))[: self.num_log_images])            
            
        # * log the image to tensorboard
        if isinstance(val_logs["batch_idx"], int):
            batch_indices = [val_logs["batch_idx"]]
        else:
            batch_indices = val_logs["batch_idx"]
            
        for i, batch_idx in enumerate(batch_indices):
            if batch_idx in self.val_log_indices:
                key = f"val_image_{batch_idx}"
                image = val_logs["image"][i].unsqueeze(0)
                target = val_logs["target"][i].unsqueeze(0)
                output = val_logs["output"][i].unsqueeze(0)
                error = torch.abs(target - output)
                image = image / image.max()
                output = output / output.max()
                target = target / target.max()
                error = error / error.max()
                
                self.log_image(f"{key}/image", image)
                self.log_image(f"{key}/target", target)
                self.log_image(f"{key}/recon", output)
                self.log_image(f"{key}/error", error)
                
        # compute evaluation metrics
        mse_vals = defaultdict(dict)
        target_norms = defaultdict(dict)
        ssim_vals = defaultdict(dict)
        max_vals = dict()
        
        for i, fname in enumerate(val_logs["fname"]):
            slice_num = int(val_logs["slice_num"][i].cpu())
            maxval = val_logs["max_value"][i].cpu().numpy()
            target = val_logs["target"][i].cpu().numpy()
            output = val_logs["output"][i].cpu().numpy()
            
            mse_vals[fname][slice_num] = torch.tensor(evaluate.mse(target, output)).view(1)
            target_norms[fname][slice_num] = torch.tensor(evaluate.mse(target, np.zeros_like(target))).view(1)
            ssim_vals[fname][slice_num] = torch.tensor(evaluate.ssim(target[None, ...], output[None, ...], maxval=maxval)).view(1)
            max_vals[fname] = maxval
            
        
        pred = {
            "val_loss": val_logs["val_loss"],
            "mse_vals": mse_vals,
            "target_norms": target_norms,
            "ssim_vals": ssim_vals,
            "max_vals": max_vals
        }        
        self.validation_step_outputs.append(pred)
        return pred
        
    def test_step(self, batch, batch_idx):
        output = self.forward(batch.image)
        if output.ndim == 4:
            post_image = self._post_process(batch.image, batch.mean, batch.std)
            post_output = self._post_process(output, batch.mean, batch.std)
            post_target = self._post_process(batch.target, batch.mean, batch.std)
            test_logs = {
                "fname": batch.fname,
                "slice_num": batch.slice_num,
                "output": post_output.cpu().numpy(),
                "target": post_target.cpu().numpy(),
                "max_value": batch.max_value,

            }
        else:
            mean = batch.mean.unsqueeze(1).unsqueeze(2)
            std = batch.std.unsqueeze(1).unsqueeze(2)
            test_logs = {
                "fname": batch.fname,
                "slice_num": batch.slice_num,
	            "output": (output * std + mean).cpu().numpy(),
	            "target": (batch.target * std + mean).cpu().numpy(),
                "max_value": batch.max_value,
            }
        
        for k in ("fname", "slice_num", "output", "target", "max_value"):
            assert k in test_logs, f"Missing {k} in val_logs"
        
        if test_logs["output"].ndim == 2:
            test_logs["output"] = test_logs["output"].unsqueeze(0)
        elif test_logs["output"].ndim != 3:
            raise RuntimeError("Unexpected output size from test.")

        if test_logs["target"].ndim == 2:
            test_logs["target"] = test_logs["target"].unsqueeze(0)
        elif test_logs["target"].ndim != 3:
            raise RuntimeError("Unexpected target size from test.")
        
        # compute evaluation metrics
        mse_vals = defaultdict(dict)
        target_norms = defaultdict(dict)
        ssim_vals = defaultdict(dict)
        max_vals = dict()
        for i, fname in enumerate(test_logs["fname"]):

            slice_num = int(test_logs["slice_num"][i].cpu())
            maxval = test_logs["max_value"][i].cpu().numpy()
            target = test_logs["target"][i]
            output = test_logs["output"][i]
            
            mse_vals[fname][slice_num] = torch.tensor(evaluate.mse(target, output)).view(1)
            target_norms[fname][slice_num] = torch.tensor(evaluate.mse(target, np.zeros_like(target))).view(1)
            ssim_vals[fname][slice_num] = torch.tensor(evaluate.ssim(target[None, ...], output[None, ...], maxval=maxval)).view(1)
            max_vals[fname] = maxval

        pred = {
            "mse_vals": dict(mse_vals),
            "target_norms": dict(target_norms),
            "ssim_vals": dict(ssim_vals),
            "max_vals": max_vals

        }
        test_logs.update(pred)
        
        self.test_step_outputs.append(test_logs)
        
        return test_logs
        
    def configure_optimizers(self):
        
        optim = torch.optim.RMSprop(
            self.parameters(),
            lr=self.lr,
            weight_decay=self.weight_decay
        )
        scheduler = torch.optim.lr_scheduler.StepLR(
            optim,
            step_size=self.lr_step_size,
            gamma=self.lr_gamma
        )
        
        return [optim], [scheduler]
    
    @staticmethod
    def add_model_specific_args(parent_parser: ArgumentParser) -> ArgumentParser:
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser = MriModule.add_model_specific_args(parser)
        
        parser.add_argument("--in_chans", type=int, default=1)
        parser.add_argument("--out_chans", type=int, default=1)
        parser.add_argument("--chans", type=int, default=32)
        parser.add_argument("--num_pool_layers", type=int, default=4)
        parser.add_argument("--drop_prob", type=float, default=0.0)
        parser.add_argument("--lr", type=float, default=1e-4)
        parser.add_argument("--lr_step_size", type=int, default=40)
        parser.add_argument("--lr_gamma", type=float, default=0.1)
        parser.add_argument("--weight_decay", type=float, default=0.0)
        
        return parser
    
