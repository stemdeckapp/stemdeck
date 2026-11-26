# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
from typing import Literal
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange


def init_layer(layer: nn.Module) -> None:
    """Initialize a Linear or Convolutional layer."""
    nn.init.xavier_uniform_(layer.weight)
    if hasattr(layer, "bias") and layer.bias is not None:
        layer.bias.data.zero_()


def init_bn(bn: nn.Module) -> None:
    """Initialize a Batchnorm layer."""
    bn.bias.data.zero_()
    bn.weight.data.fill_(1.0)
    bn.running_mean.data.zero_()
    bn.running_var.data.fill_(1.0)


def act(x: torch.Tensor, activation: str) -> torch.Tensor:
    """Activation function."""
    funcs = {"relu": F.relu_, "leaky_relu": lambda x: F.leaky_relu_(x, 0.01), "swish": lambda x: x * torch.sigmoid(x)}
    return funcs.get(activation, lambda x: Exception("Incorrect activation!"))(x)


class Res2DAVPBlock(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size, avp_kernel_size, activation):
        """Convolutional residual block modified fromr bytedance/music_source_separation."""
        super().__init__()

        padding = kernel_size[0] // 2, kernel_size[1] // 2

        self.activation = activation
        self.bn1, self.bn2 = nn.BatchNorm2d(out_channels), nn.BatchNorm2d(out_channels)

        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size, padding=padding, bias=False)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size, padding=padding, bias=False)

        self.is_shortcut = in_channels != out_channels
        if self.is_shortcut:
            self.shortcut = nn.Conv2d(in_channels, out_channels, kernel_size=(1, 1))

        self.avp = nn.AvgPool2d(avp_kernel_size)
        self.init_weights()

    def init_weights(self):
        for m in [self.conv1, self.conv2] + ([self.shortcut] if self.is_shortcut else []):
            init_layer(m)
        for m in [self.bn1, self.bn2]:
            init_bn(m)

    def forward(self, x):
        origin = x
        x = act(self.bn1(self.conv1(x)), self.activation)
        x = self.bn2(self.conv2(x))
        x += self.shortcut(origin) if self.is_shortcut else origin
        x = act(x, self.activation)
        return self.avp(x)


class PreEncoderBlockRes3B(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size=(3, 3), avp_kernerl_size=(1, 2), activation='relu'):
        """Pre-Encoder with 3 Res2DAVPBlocks."""
        super().__init__()

        self.blocks = nn.ModuleList([
            Res2DAVPBlock(in_channels if i == 0 else out_channels, out_channels, kernel_size, avp_kernerl_size,
                          activation) for i in range(3)
        ])

    def forward(self, x):  # (B, T, F)
        x = rearrange(x, 'b t f -> b 1 t f')
        for block in self.blocks:
            x = block(x)
        return rearrange(x, 'b c t f -> b t f c')


def test_res3b():
    # mel-spec input
    x = torch.randn(2, 256, 512)  # (B, T, F)
    pre = PreEncoderBlockRes3B(in_channels=1, out_channels=128)
    x = pre(x)  # (2, 256, 64, 128): B T,F,C

    x = torch.randn(2, 110, 1024)  # (B, T, F)
    pre = PreEncoderBlockRes3B(in_channels=1, out_channels=128)
    x = pre(x)  # (2, 110, 128, 128): B,T,F,C


# ====================================================================================================================
# PreEncoderBlockHFTT: hFT-Transformer-like Pre-encoder
# ====================================================================================================================
class PreEncoderBlockHFTT(nn.Module):

    def __init__(self, margin_pre=15, margin_post=16) -> None:
        """Pre-Encoder with hFT-Transformer-like convolutions."""
        super().__init__()

        self.margin_pre, self.margin_post = margin_pre, margin_post
        self.conv = nn.Conv2d(1, 4, kernel_size=(1, 5), padding='same', padding_mode='zeros')
        self.emb_freq = nn.Linear(128, 128)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, F)
        x = rearrange(x, 'b t f -> b 1 f t')  # (B, 1, F, T) or (2, 1, 128, 110)
        x = F.pad(x, (self.margin_pre, self.margin_post), value=1e-7)  # (B, 1, F, T+margin) or (2,1,128,141)
        x = self.conv(x)  # (B, C, F, T+margin) or (2, 4, 128, 141)
        x = x.unfold(dimension=3, size=32, step=1)  # (B, c1, T, F, c2) or (2, 4, 128, 110, 32)
        x = rearrange(x, 'b c1 f t c2 -> b t f (c1 c2)')  # (B, T, F, C) or (2, 110, 128, 128)
        return self.emb_freq(x)  # (B, T, F, C) or (2, 110, 128, 128)


def test_hftt():
    # from model.spectrogram import get_spectrogram_layer_from_audio_cfg
    # from config.config import audio_cfg as default_audio_cfg
    # audio_cfg = default_audio_cfg
    # audio_cfg['codec'] = 'melspec'
    # audio_cfg['hop_length'] = 300
    # audio_cfg['n_mels'] = 128
    # x = torch.randn(2, 1, 32767)
    # mspec, _ = get_spectrogram_layer_from_audio_cfg(audio_cfg)
    # x = mspec(x)
    x = torch.randn(2, 110, 128)  # (B, T, F)
    pre_enc_hftt = PreEncoderBlockHFTT()
    y = pre_enc_hftt(x)  # (2, 110, 128, 128): B, T, F, C


# ====================================================================================================================
# PreEncoderBlockRes3BHFTT: hFT-Transformer-like Pre-encoder with Res2DAVPBlock and spec input
# ====================================================================================================================
class PreEncoderBlockRes3BHFTT(nn.Module):

    def __init__(self, margin_pre: int = 15, margin_post: int = 16) -> None:
        """Pre-Encoder with hFT-Transformer-like convolutions.
        
        Args:
            margin_pre (int): padding before the input
            margin_post (int): padding after the input
            stack_dim (Literal['c', 'f']): stack dimension. channel or frequency

        """
        super().__init__()
        self.margin_pre, self.margin_post = margin_pre, margin_post
        self.res3b = PreEncoderBlockRes3B(in_channels=1, out_channels=4)
        self.emb_freq = nn.Linear(128, 128)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, F) or (2, 110, 1024), input spectrogram
        x = rearrange(x, 'b t f -> b f t')  # (2, 1024, 110): B,F,T
        x = F.pad(x, (self.margin_pre, self.margin_post), value=1e-7)  # (2, 1024, 141): B,F,T+margin
        x = rearrange(x, 'b f t -> b t f')  # (2, 141, 1024): B,T+margin,F
        x = self.res3b(x)  # (2, 141, 128, 4): B,T+margin,F,C
        x = x.unfold(dimension=1, size=32, step=1)  # (B, T, F, C1, C2) or (2, 110, 128, 4, 32)
        x = rearrange(x, 'b t f c1 c2 -> b t f (c1 c2)')  # (B, T, F, C) or (2, 110, 128, 128)
        return self.emb_freq(x)  # (B, T, F, C) or (2, 110, 128, 128)


def test_res3b_hftt():
    # from model.spectrogram import get_spectrogram_layer_from_audio_cfg
    # from config.config import audio_cfg as default_audio_cfg
    # audio_cfg = default_audio_cfg
    # audio_cfg['codec'] = 'spec'
    # audio_cfg['hop_length'] = 300
    # x = torch.randn(2, 1, 32767)
    # spec, _ = get_spectrogram_layer_from_audio_cfg(audio_cfg)
    # x = spec(x)  # (2, 110, 1024): B,T,F
    x = torch.randn(2, 110, 1024)  # (B, T, F)
    pre_enc_res3b_hftt = PreEncoderBlockRes3BHFTT()
    y = pre_enc_res3b_hftt(x)  # (2, 110, 128, 128): B, T, F, C


# # ====================================================================================================================
# # PreEncoderBlockConv1D: Pre-encoder without activation, with Melspec input
# # ====================================================================================================================
# class PreEncoderBlockConv1D(nn.Module):

#     def __init__(self,
#                  in_channels,
#                  out_channels,
#                  kernel_size=3) -> None:
#         """Pre-Encoder with 1D convolution."""
#         super().__init__()
#         self.conv1 = nn.Conv1d(in_channels, out_channels, kernel_size=kernel_size, stride=1)
#         self.conv2 = nn.Conv1d(out_channels, out_channels, kernel_size=kernel_size, stride=1)

#     def forward(self, x: torch.Tensor) -> torch.Tensor:
#         # x: (B, T, F) or (2, 128, 256), input melspec
#         x = rearrange(x, 'b t f -> b f t')  # (2, 256, 128): B,F,T
#         x = self.conv1(x)  # (2, 128, 128): B,F,T
#         return rearrange(x, 'b f t -> b t f')  # (2, 110, 128): B,T,F

# def test_conv1d():
#     # from model.spectrogram import get_spectrogram_layer_from_audio_cfg
#     # from config.config import audio_cfg as default_audio_cfg
#     # audio_cfg = default_audio_cfg
#     # audio_cfg['codec'] = 'melspec'
#     # audio_cfg['hop_length'] = 256
#     # audio_cfg['n_mels'] = 512
#     # x = torch.randn(2, 1, 32767)
#     # mspec, _ = get_spectrogram_layer_from_audio_cfg(audio_cfg)
#     # x = mspec(x)
#     x = torch.randn(2, 128, 128)  # (B, T, F)
#     pre_enc_conv1d = PreEncoderBlockConv1D(in_channels=1, out_channels=128)
#     y = pre_enc_conv1d(x)  # (2, 110, 128, 128): B, T, F, C
