# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
""" projection_layer.py """
from typing import Tuple

import math
import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import Linear, LayerNorm

from einops import rearrange
from model.ops import count_parameters


class GroupLinearFlatten(nn.Module):
    """
    Implements a grouped linear layer with a flattened output.

    This module applies individual linear transformations for each group in the input tensor 
    and then flattens the group dimension to produce the final output. It's useful when you 
    have distinct groups in the input tensor and you want separate linear transformations for 
    each of these groups.

    Args:
    - in_features (int): The number of input features per group.
    - flatten_out_features (int): The total number of flattened output features. This value must 
                                  be divisible by num_groups. The actual number of output features 
                                  per group is computed as flatten_out_features/num_groups.
    - num_groups (int): The number of distinct groups in the input tensor.
    - use_bmm (bool, optional): Whether to use batch matrix multiplication for computation. 
                                Default is True.
    
    Shape:
    - Input: (batch_size, sequence_length, num_groups, in_features)
    - Output: (batch_size, sequence_length, flatten_out_features)
    
    Examples:
    >>> m = GroupLinearFlatten(128, 512, 24) #  
    >>> input = torch.randn(16, 10, 24, 128) # (B, T, C, F)
    >>> output = m(input)
    >>> output.size()
    torch.Size([16, 10, 512]) # (B, T, D)
    """

    def __init__(self, in_features, flatten_out_features, num_groups, use_bmm=True):
        super().__init__()
        self.in_features = in_features
        self.flatten_out_features = flatten_out_features
        self.num_groups = num_groups
        self.use_bmm = use_bmm

        # Assuming flatten_out_features is divisible by num_groups
        self.out_features_per_group = self.flatten_out_features // self.num_groups

        # Each group gets its own weights
        self.weight = nn.Parameter(torch.Tensor(num_groups, self.out_features_per_group, in_features))
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))

    def forward(self, input):
        # input shape: (batch, seq_length, groups, in_features)
        # weight shape: (groups, out_features_per_group, in_features)

        batch_size, t, k, source_d = input.size()

        if self.use_bmm:
            # Reshape input for bmm operation
            input_reshaped = rearrange(input, 'b t k d -> k d (b t)')

            # Matrix multiplication: dot((k, out_features_per_group, d), (k, d, b*t)) -> (k, out_features_per_group, b*t)
            output_bmm = torch.bmm(self.weight, input_reshaped)

            # Reshape back to original shape and flatten the group dimension
            output = rearrange(output_bmm, 'k d_out (b t) -> b t (k d_out)', b=batch_size, t=t, k=k)
        else:
            output = torch.einsum('bsgi,goi->bsgo', input, self.weight)
            output = rearrange(output, 'b t k d_out -> b t (k d_out)')

        return output


# class MultiChannelGroupLinear(nn.Module):
#     """ Not Implemented Yet """
#     def __init__(self, in_ch=26, in_dim=128, out_ch=13, out_dim=512):
#         super().__init__()

#         self.in_ch = in_ch
#         self.in_dim = in_dim
#         self.out_ch = out_ch
#         self.out_dim = out_dim
#         self.in_ch_per_group = in_ch // out_ch

#         self.layer = GroupLinearFlatten(in_features=)


class MultiChannelLinearProjection(nn.Module):

    def __init__(self, in_ch=26, in_dim=128, out_ch=13, out_dim=512):
        super().__init__()
        self.in_ch = in_ch
        self.in_dim = in_dim
        self.out_ch = out_ch
        self.out_dim = out_dim

        self.in_ch_per_group = in_ch // out_ch
        self.linear_in_ch = in_ch // self.in_ch_per_group
        self.linear_in_dim = in_dim * self.in_ch_per_group

        # Reshaped Input shape: (b, t, in_dim//in_ch_per_group, in_dim*in_ch_per_group)
        # Output shape: (b, t, out_ch, out_dim)
        if in_dim * self.in_ch_per_group == out_dim:
            self.linear = nn.Identity()
        else:
            self.linear = nn.Linear(in_features=self.linear_in_dim, out_features=out_dim, bias=False)

    def forward(self, x):
        """
        Args:
            x: (B, T, C, D)
        
        Returns:
            x: (B, C_target, T, D_target)
        """
        x = rearrange(x, 'b t (c1 c2) d -> b c1 t (c2 d)', c1=self.linear_in_ch, c2=self.in_ch_per_group)
        return self.linear(x)


def get_multi_channel_projection_layer(input_shape: Tuple[int], output_shape: Tuple[int], proj_type: str) -> nn.Module:
    """ This function returns one of the projection layers for multi-channel models."""
    in_ch = input_shape[-2]
    in_dim = input_shape[-1]
    out_ch = output_shape[-2]
    out_dim = output_shape[-1]

    if proj_type == 'mc_shared_linear':
        return MultiChannelLinearProjection(in_ch, in_dim, out_ch, out_dim)


def test_multi_channel_linear_projection():
    x = torch.randn(2, 10, 26, 128)  # (b, t, c, d)
    mclp = MultiChannelLinearProjection(in_ch=26, in_dim=128, out_ch=13, out_dim=256)  # actually nn.Identity()
    assert type(nn.Identity()) == type(mclp.linear)
    assert mclp(x).shape == (2, 13, 10, 256)  # (b, _c, t, _d)

    x = torch.randn(2, 10, 26, 128)  # (b, t, c, d)
    mclp = MultiChannelLinearProjection(in_ch=26, in_dim=128, out_ch=13, out_dim=512)  # actually nn.Identity()
    assert torch.nn.modules.linear.Linear == type(mclp.linear)
    assert mclp(x).shape == (2, 13, 10, 512)  # (b, _c, t, _d)


class FlattenMLP(nn.Module):

    def __init__(self, in_features, flatten_out_features, num_groups, hidden_dim=None, activation=None):
        super().__init__()

        self.in_features = in_features
        self.num_groups = num_groups

        # Calculate flattened input dimension
        self.flat_in_dim = in_features * num_groups
        if hidden_dim is None:
            hidden_dim = self.flat_in_dim // 2
        self.hidden_dim = hidden_dim

        # Check if flatten_out_features is divisible by in_features
        assert flatten_out_features % in_features == 0, "flatten_out_features should be divisible by in_features."

        # Define layers
        self.layers = nn.Sequential(nn.Flatten(2, 3), nn.Linear(self.flat_in_dim, hidden_dim), nn.LayerNorm(hidden_dim),
                                    activation() if activation else nn.Identity(), nn.Linear(hidden_dim, hidden_dim),
                                    nn.LayerNorm(hidden_dim),
                                    activation() if activation else nn.Identity(),
                                    nn.Linear(hidden_dim, flatten_out_features))

    def forward(self, x):
        # x shape: (batch, seq, num_groups, in_features)
        return self.layers(x)


class LinearProjection(nn.Module):

    def __init__(self, in_features, flatten_out_features, num_groups):
        super().__init__()

        # Calculate flattened input dimension
        self.flat_in_dim = in_features * num_groups
        self.projection_layer = nn.Linear(in_features=self.flat_in_dim, out_features=flatten_out_features, bias=False)

    def forward(self, x):
        # x shape: (batch, seq, num_groups, in_features)
        batch_size, t, _, _ = x.size()
        x_flattened = x.reshape(batch_size, t, -1)  # Flattening num_groups and in_features
        return self.projection_layer(x_flattened)


class DepthwiseConvProjection(nn.Module):

    def __init__(self, in_features, flatten_out_features, num_groups, depth):
        super().__init__()
        d_out = flatten_out_features // in_features

        self.conv = nn.Conv2d(in_channels=num_groups,
                              out_channels=num_groups * d_out,
                              kernel_size=(1, depth),
                              groups=num_groups)

        self.fc = nn.Linear(num_groups * d_out * (in_features - depth + 1), flatten_out_features)

    def forward(self, x):
        # Swap the dimensions of k and t to match expected input for depthwise convolution
        x = x.permute(0, 2, 1, 3)  # shape: (b, k, t, d)

        # Convolutional layer
        x = self.conv(x)  # shape: (b, k*d_out, t, d-depth+1)

        # Reshape the tensor for the Linear layer
        batch_size, _, t, _ = x.size()
        x = x.reshape(batch_size, t, -1)
        return self.fc(x)


def get_projection_layer(input_shape: Tuple[int], output_shape: Tuple[int], proj_type: str) -> nn.Module:
    """ This function returns one of the projection layers defined below. """
    if len(input_shape) == 2:
        _, d_source = input_shape
    elif len(input_shape) == 3:
        _, k_source, d_source = input_shape
    if len(output_shape) == 2:
        _, d_target = output_shape
    elif len(output_shape) == 3:
        _, k_target, d_target = output_shape

    if 'linear' == proj_type:
        return LinearProjection(in_features=d_source, flatten_out_features=d_target, num_groups=k_source)
    elif 'mlp' in proj_type:
        if 'gelu' in proj_type:
            return FlattenMLP(in_features=d_source,
                              flatten_out_features=d_target,
                              num_groups=k_source,
                              activation=nn.GELU)
        elif 'relu' in proj_type:
            return FlattenMLP(in_features=d_source,
                              flatten_out_features=d_target,
                              num_groups=k_source,
                              activation=nn.ReLU)
        else:
            return FlattenMLP(in_features=d_source, flatten_out_features=d_target, num_groups=k_source, activation=None)
    elif 'conv' in proj_type:
        if 'conv4' == proj_type:
            return DepthwiseConvProjection(in_features=d_source,
                                           flatten_out_features=d_target,
                                           num_groups=k_source,
                                           depth=4)
        elif 'conv16' == proj_type:
            return DepthwiseConvProjection(in_features=d_source,
                                           flatten_out_features=d_target,
                                           num_groups=k_source,
                                           depth=16)
        elif 'conv32' == proj_type:
            return DepthwiseConvProjection(in_features=d_source,
                                           flatten_out_features=d_target,
                                           num_groups=k_source,
                                           depth=32)
        elif 'conv64' == proj_type:
            return DepthwiseConvProjection(in_features=d_source,
                                           flatten_out_features=d_target,
                                           num_groups=k_source,
                                           depth=64)
        else:  # conv depth 1
            return DepthwiseConvProjection(in_features=d_source,
                                           flatten_out_features=d_target,
                                           num_groups=k_source,
                                           depth=1)
    elif 'group_linear' == proj_type:
        assert d_source % k_source == 0, "d_source and k_source must be divisible for group_linear projection."
        return GroupLinearFlatten(in_features=d_source,
                                  flatten_out_features=d_target,
                                  num_groups=k_source,
                                  use_bmm=True)
    else:
        raise ValueError(f"Invalid projection type: {proj_type}")


def test_projection_layers():
    # encoder hidden states: (B, T, K, D)
    b = 2
    t = 110  #10
    k = 24  #16
    d = 128
    enc_hs = torch.randn(b, t, k, d)

    # target shape: (B, T, K, D//4)
    target_flatten_d = 512

    # GroupLinear
    gl = GroupLinearFlatten(in_features=d, flatten_out_features=target_flatten_d, num_groups=k, use_bmm=True)
    enc_hs_hat = gl(enc_hs)
    assert enc_hs_hat.shape == (b, t, target_flatten_d)
    print('GroupLinear: ', f'{count_parameters(gl)//1000}k')  # 65k

    # FlattenMLP
    fm = FlattenMLP(in_features=d,
                    flatten_out_features=target_flatten_d,
                    num_groups=k,
                    hidden_dim=None,
                    activation=nn.GELU)
    enc_hs_hat = fm(enc_hs)
    assert enc_hs_hat.shape == (b, t, target_flatten_d)
    print('FlattenMLP: ', f'{count_parameters(fm)//1000}k')  # 3.6M

    # LinearProjection
    lp = LinearProjection(in_features=d, flatten_out_features=target_flatten_d, num_groups=k)
    enc_hs_hat = lp(enc_hs)
    assert enc_hs_hat.shape == (b, t, target_flatten_d)
    print('LinearProjection: ', f'{count_parameters(lp)//1000}k')  # 1M

    # DepthwiseConvProjection
    dc = DepthwiseConvProjection(in_features=d, flatten_out_features=target_flatten_d, num_groups=k, depth=16)
    enc_hs_hat = dc(enc_hs)
    assert enc_hs_hat.shape == (b, t, target_flatten_d)
    print('DepthwiseConvProjection: ', f'{count_parameters(dc)//1000}k')  # 4M
