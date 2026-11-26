# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# NewGELUActivation is copied verbatim from transformers/activations.py;
# ACT2FN is a rewrite with only the names a config can ask for.
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

import math

import torch
from torch import Tensor, nn

class NewGELUActivation(nn.Module):
    """
    Implementation of the GELU activation function currently in Google BERT repo (identical to OpenAI GPT). Also see
    the Gaussian Error Linear Units paper: https://arxiv.org/abs/1606.08415
    """

    def forward(self, input: Tensor) -> Tensor:
        return 0.5 * input * (1.0 + torch.tanh(math.sqrt(2.0 / math.pi) * (input + 0.044715 * torch.pow(input, 3.0))))


class _Acts(dict):
    def __getitem__(self, key):
        cls = dict.__getitem__(self, key)
        return cls()


ACT2FN = _Acts({
    "relu": nn.ReLU, "gelu": nn.GELU, "gelu_new": NewGELUActivation, "silu": nn.SiLU, "swish": nn.SiLU,
    "tanh": nn.Tanh, "gelu_pytorch_tanh": lambda: nn.GELU(approximate="tanh"), "sigmoid": nn.Sigmoid,
})
