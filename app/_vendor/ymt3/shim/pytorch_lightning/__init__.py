# StemDeck stand-in for pytorch_lightning's LightningModule: the vendored
# YourMT3 class (app/_vendor/ymt3/src/model/ymt3.py) subclasses it, and inference needs only
# a torch nn.Module with the attributes it reads. Written for StemDeck, no
# code of Lightning. See app/_vendor/ymt3/README.md.

import torch
from torch import nn


class LightningModule(nn.Module):
    global_rank = 0
    local_rank = 0

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.hparams = {}

    def save_hyperparameters(self, *args, **kwargs):
        pass

    @property
    def device(self):
        try:
            return next(self.parameters()).device
        except StopIteration:
            return torch.device("cpu")
