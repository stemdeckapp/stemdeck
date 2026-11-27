# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
""" op.py """
import math
from packaging.version import parse as VersionParse

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from transformers.models.t5.modeling_t5 import T5LayerNorm as RMSNorm


def get_layer_norm(dim: int, layer_norm_type: str = "layer_norm", layer_norm_eps: float = 1e-5):
    """Get layer normalization layer.
    Args:
        dim (int): Feature dimension
        layer_norm_type (str): "layer_norm" or "rms_norm"
        layer_norm_eps (float): Epsilon value for numerical stability

    Returns:
        nn.Module: Layer normalization layer
    """
    if layer_norm_type == "rms_norm":
        # T5LayerNorm is equivalent to RMSNorm. https://arxiv.org/abs/1910.07467
        return RMSNorm(hidden_size=dim, eps=layer_norm_eps)
    else:
        return nn.LayerNorm(normalized_shape=dim, eps=layer_norm_eps)


def check_all_elements_equal(x: torch.Tensor) -> bool:
    return x.eq(x[0]).all().item()


def minmax_normalize(x: torch.Tensor, eps: float = 0.008) -> torch.FloatTensor:
    """Min-max normalization:

    x_norm = (x - x_min) / (x_max - x_min + eps)

    Args:
        x (torch.Tensor): (B, T, F)
    Returns:
        torch.Tensor: (B, T, F) with output range of [0, 1]
    """
    x_max = rearrange(x, "b t f -> b (t f)").max(1, keepdim=True)[0]
    x_min = rearrange(x, "b t f -> b (f t)").min(1, keepdim=True)[0]
    x_max = x_max[:, None, :]  # (B,1,1)
    x_min = x_min[:, None, :]  # (B,1,1)
    return (x - x_min) / (x_max - x_min + eps)


def count_parameters(model):
    num_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    num_params = sum(p.numel() for p in model.parameters())
    return num_trainable_params, num_params


def adjust_b_to_gcd(a, b, min_gcd=16):
    """
    Adjust the value of b to ensure the GCD(a, b) is at least min_gcd with minimum change to b.
    
    Parameters:
    - a (int): A positive integer
    - b (int): A positive integer
    - min_gcd (int): The minimum desired GCD
    
    Returns:
    - int: The adjusted value of b
    """
    current_gcd = math.gcd(a, b)

    # If current GCD is already greater than or equal to min_gcd, return b as it is.
    if current_gcd >= min_gcd:
        return b

    # If a is less than min_gcd, then it's impossible to get a GCD of at least min_gcd.
    if a < min_gcd:
        raise ValueError("a must be at least as large as min_gcd.")

    # Adjust b by trying increments and decrements, preferring the smallest absolute change.
    adjusted_b_up = b
    adjusted_b_down = b

    while True:
        adjusted_b_up += 1
        adjusted_b_down -= 1

        if math.gcd(a, adjusted_b_up) >= min_gcd:
            return adjusted_b_up
        elif math.gcd(a, adjusted_b_down) >= min_gcd:
            return adjusted_b_down


def optional_compiler_disable(func):
    if VersionParse(torch.__version__) >= VersionParse("2.1"):
        # If the version is 2.1 or higher, apply the torch.compiler.disable decorator.
        return torch.compiler.disable(func)
    else:
        # If the version is below 2.1, return the original function.
        return func


def optional_compiler_dynamic(func):
    return torch.compile(func, dynamic=True)
