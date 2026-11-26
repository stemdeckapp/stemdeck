# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""spectrogram.py"""
import importlib
from typing import Optional, Literal, Dict, Tuple
from packaging.version import parse as VersionParse

import torch
import torch.nn as nn
from einops import rearrange
from model.ops import minmax_normalize
from config.config import audio_cfg as default_audio_cfg
"""
Example usage:

# MT3 setup
>>> hop = 8 ms or 128 samples 
>>> melspec = Melspectrogram(sample_rate=16000, n_fft=2048, hop_length=128,
                            f_min=50, f_max=8000, n_mels=512)
>>> x = torch.randn(2, 1, 32767) # (B, C=1, T): 2.048 s
>>> y = melspec(x) # (2, 256, 512) (B, T, F)

# PerceiverTF-like setup
>>> hop = 18.75 ms or 300 samples
>>> spec = Spectrogram(n_fft=2048, hop_length=300)
                            )
>>> x = torch.randn(2, 1, 95999) # (B, C=1, T): 6.000 s
>>> y = spec(x) # (2, 320, 1024) (B, T, F)

# Hybrid setup (2.048 seconds segment and spectrogram with hop=300)
>>> hop = 18.75 ms or 300 samples
>>> spec = Spectrogram(n_fft=2048, hop_length=300)
>>> x = torch.randn(2, 1, 32767) # (B, C=1, T): 2.048 s
>>> y = spec(x) # (2, 110, 1024) (B, T, F)

# PerceiverTF-like setup, hop=256
>>> hop = 16 ms or 256 samples
>>> spec256 = Spectrogram(sample_rate=16000, n_fft=2048, hop_length=256,
                                f_min=20, f_max=8000, n_mels=256)
>>> x = torch.randn(2, 1, 32767) # (B, C=1, T): 2.048 s
>>> y = spec256(x) # (2, 128, 1024) (B, T, F)
"""


def optional_compiler_disable(func):
    if VersionParse(torch.__version__) >= VersionParse("2.1"):
        # If the version is 2.1 or higher, apply the torch.compiler.disable decorator.
        return torch.compiler.disable(func)
    else:
        # If the version is below 2.1, return the original function.
        return func


# -------------------------------------------------------------------------------------
# Log-Mel spectrogram
# -------------------------------------------------------------------------------------
class Melspectrogram(nn.Module):

    def __init__(
        self,
        audio_backend: Literal['torchaudio', 'nnaudio'] = 'torchaudio',
        sample_rate: int = 16000,
        n_fft: int = 2048,
        hop_length: int = 128,
        f_min: int = 50,  # 20 Hz in the MT3 paper, but we can only use 20 Hz with nnAudio
        f_max: Optional[int] = 8000,
        n_mels: int = 512,
        eps: float = 1e-5,
        **kwargs,
    ):
        """
        Log-Melspectrogram

        Args:
            audio_backend (str): 'torchaudio' or 'nnaudio'
            sample_rate (int): sample rate in Hz
            n_fft (int): FFT window size
            hop_length (int): hop length in samples
            f_min (int): minimum frequency in Hz
            f_max (int): maximum frequency in Hz
            n_mels (int): number of mel frequency bins
            eps (float): epsilon for numerical stability

        """
        super(Melspectrogram, self).__init__()
        self.audio_backend = audio_backend.lower()

        if audio_backend.lower() == 'torchaudio':
            torchaudio = importlib.import_module('torchaudio')
            self.mel_stft = torchaudio.transforms.MelSpectrogram(
                sample_rate=sample_rate,
                n_fft=n_fft,
                hop_length=hop_length,
                f_min=f_min,
                f_max=f_max,
                n_mels=n_mels,
            )
        elif audio_backend.lower() == 'nnaudio':
            nnaudio = importlib.import_module('nnAudio.features')
            self.mel_stft_nnaudio = nnaudio.mel.MelSpectrogram(
                sr=sample_rate,
                win_length=n_fft,
                n_mels=n_mels,
                hop_length=hop_length,
                fmin=20,  #f_min,
                fmax=f_max)
        else:
            raise NotImplementedError(audio_backend)
        self.eps = eps

    @optional_compiler_disable
    def forward(self, x: torch.Tensor) -> torch.Tensor:  # (B, 1, T)
        """
        Args:
            x (torch.Tensor): (B, 1, T)
            
        Returns:
            torch.Tensor: (B, T, F)
        
        """
        if self.audio_backend == 'torchaudio':
            x = self.mel_stft(x)  # (B, 1, F, T)
            x = rearrange(x, 'b 1 f t -> b t f')
            x = minmax_normalize(torch.log(x + self.eps))
            # some versions of torchaudio returns nan when input is all-zeros
            return torch.nan_to_num(x)

        elif self.audio_backend == 'nnaudio':
            x = self.mel_stft_nnaudio(x)  # (B, F, T)
            x = rearrange(x, 'b f t -> b t f')
            x = minmax_normalize(torch.log(x + self.eps))
            return x


# -------------------------------------------------------------------------------------
# Log-spectrogram
# -------------------------------------------------------------------------------------
class Spectrogram(nn.Module):

    def __init__(
        self,
        audio_backend: Literal['torchaudio', 'nnaudio'] = 'torchaudio',
        n_fft: int = 2048,
        hop_length: int = 128,
        eps: float = 1e-5,
        **kwargs,
    ):
        """
        Log-Magnitude Spectrogram 

        Args:
            audio_backend (str): 'torchaudio' or 'nnaudio'
            n_fft (int): FFT window size, creates n_fft // 2 + 1 freq-bins
            hop_length (int): hop length in samples
            eps (float): epsilon for numerical stability

        """
        super(Spectrogram, self).__init__()
        self.audio_backend = audio_backend.lower()

        if audio_backend.lower() == 'torchaudio':
            torchaudio = importlib.import_module('torchaudio')
            self.stft = torchaudio.transforms.Spectrogram(n_fft=n_fft,
                                                          hop_length=hop_length,
                                                          window_fn=torch.hann_window,
                                                          power=1.)  # (B, 1, F, T), remove DC component
        elif audio_backend.lower() == 'nnaudio':
            # TODO: nnAudio spectrogram
            raise NotImplementedError(audio_backend)
        else:
            raise NotImplementedError(audio_backend)
        self.eps = eps

    @optional_compiler_disable
    def forward(self, x: torch.Tensor) -> torch.Tensor:  # (B, 1, T)
        """
        Args:
            x (torch.Tensor): (B, 1, T)
            
        Returns:
            torch.Tensor: (B, T, F)
        
        """
        if self.audio_backend == 'torchaudio':
            x = self.stft(x)[:, :, 1:, :]  # (B, 1, F, T) remove DC component
            x = rearrange(x, 'b 1 f t -> b t f')
            x = minmax_normalize(torch.log(x + self.eps))
            return torch.nan_to_num(x)  # some versions of torchaudio returns nan when input is all-zeros
        elif self.audio_backend == 'nnaudio':
            raise NotImplementedError(self.audio_backend)


def get_spectrogram_layer_from_audio_cfg(audio_cfg: Optional[Dict] = None) -> Tuple[nn.Module, Tuple[int]]:
    """Get mel-/spectrogram layer from config.
    - Used by 'ymt3' to create a spectrogram layer.
    - Returns output shape of spectrogram layer, which is used to determine input shape of model.
    
    Args:
        audio_cfg (dict): see config/config.py

    Returns:
        layer (nn.Module): mel-/spectrogram layer
        output_shape (tuple): inferred output shape of layer excluding batch dim. (T, F)
    """
    if audio_cfg is None:
        audio_cfg = default_audio_cfg

    if audio_cfg['codec'] == 'melspec':
        layer = Melspectrogram(**audio_cfg)
    elif audio_cfg['codec'] == 'spec':
        layer = Spectrogram(**audio_cfg)
    else:
        raise NotImplementedError(audio_cfg['codec'])

    # Infer output shape of the spectrogram layer
    with torch.no_grad():
        output_shape = layer(torch.randn(1, 1, audio_cfg['input_frames'])).shape[1:]
    return layer, output_shape
