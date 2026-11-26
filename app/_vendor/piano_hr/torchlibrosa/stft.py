import math
import argparse

import librosa
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter


class DFTBase(nn.Module):
    def __init__(self):
        r"""Base class for DFT and IDFT matrix.
        """
        super(DFTBase, self).__init__()

    def dft_matrix(self, n):
        (x, y) = np.meshgrid(np.arange(n), np.arange(n))
        omega = np.exp(-2 * np.pi * 1j / n)
        W = np.power(omega, x * y)  # shape: (n, n)
        return W

    def idft_matrix(self, n):
        (x, y) = np.meshgrid(np.arange(n), np.arange(n))
        omega = np.exp(2 * np.pi * 1j / n)
        W = np.power(omega, x * y)  # shape: (n, n)
        return W


class DFT(DFTBase):
    def __init__(self, n, norm):
        r"""Calculate discrete Fourier transform (DFT), inverse DFT (IDFT, 
        right DFT (RDFT) RDFT, and inverse RDFT (IRDFT.) 

        Args:
          n: fft window size
          norm: None | 'ortho'
        """
        super(DFT, self).__init__()

        self.W = self.dft_matrix(n)
        self.inv_W = self.idft_matrix(n)

        self.W_real = torch.Tensor(np.real(self.W))
        self.W_imag = torch.Tensor(np.imag(self.W))
        self.inv_W_real = torch.Tensor(np.real(self.inv_W))
        self.inv_W_imag = torch.Tensor(np.imag(self.inv_W))

        self.n = n
        self.norm = norm

    def dft(self, x_real, x_imag):
        r"""Calculate DFT of a signal.

        Args:
            x_real: (n,), real part of a signal
            x_imag: (n,), imag part of a signal

        Returns:
            z_real: (n,), real part of output
            z_imag: (n,), imag part of output
        """
        z_real = torch.matmul(x_real, self.W_real) - torch.matmul(x_imag, self.W_imag)
        z_imag = torch.matmul(x_imag, self.W_real) + torch.matmul(x_real, self.W_imag)
        # shape: (n,)

        if self.norm is None:
            pass
        elif self.norm == 'ortho':
            z_real /= math.sqrt(self.n)
            z_imag /= math.sqrt(self.n)

        return z_real, z_imag

    def idft(self, x_real, x_imag):
        r"""Calculate IDFT of a signal.

        Args:
            x_real: (n,), real part of a signal
            x_imag: (n,), imag part of a signal
        Returns:
            z_real: (n,), real part of output
            z_imag: (n,), imag part of output
        """
        z_real = torch.matmul(x_real, self.inv_W_real) - torch.matmul(x_imag, self.inv_W_imag)
        z_imag = torch.matmul(x_imag, self.inv_W_real) + torch.matmul(x_real, self.inv_W_imag)
        # shape: (n,)

        if self.norm is None:
            z_real /= self.n
        elif self.norm == 'ortho':
            z_real /= math.sqrt(n)
            z_imag /= math.sqrt(n)

        return z_real, z_imag

    def rdft(self, x_real):
        r"""Calculate right RDFT of signal.

        Args:
            x_real: (n,), real part of a signal
            x_imag: (n,), imag part of a signal

        Returns:
            z_real: (n // 2 + 1,), real part of output
            z_imag: (n // 2 + 1,), imag part of output
        """
        n_rfft = self.n // 2 + 1
        z_real = torch.matmul(x_real, self.W_real[..., 0 : n_rfft])
        z_imag = torch.matmul(x_real, self.W_imag[..., 0 : n_rfft])
        # shape: (n // 2 + 1,)

        if self.norm is None:
            pass
        elif self.norm == 'ortho':
            z_real /= math.sqrt(self.n)
            z_imag /= math.sqrt(self.n)

        return z_real, z_imag

    def irdft(self, x_real, x_imag):
        r"""Calculate IRDFT of signal.
        
        Args:
            x_real: (n // 2 + 1,), real part of a signal
            x_imag: (n // 2 + 1,), imag part of a signal

        Returns:
            z_real: (n,), real part of output
            z_imag: (n,), imag part of output
        """
        n_rfft = self.n // 2 + 1

        flip_x_real = torch.flip(x_real, dims=(-1,))
        flip_x_imag = torch.flip(x_imag, dims=(-1,))
        # shape: (n // 2 + 1,)

        x_real = torch.cat((x_real, flip_x_real[..., 1 : n_rfft - 1]), dim=-1)
        x_imag = torch.cat((x_imag, -1. * flip_x_imag[..., 1 : n_rfft - 1]), dim=-1)
        # shape: (n,)

        z_real = torch.matmul(x_real, self.inv_W_real) - torch.matmul(x_imag, self.inv_W_imag)
        # shape: (n,)

        if self.norm is None:
            z_real /= self.n
        elif self.norm == 'ortho':
            z_real /= math.sqrt(n)

        return z_real


class STFT(DFTBase):
    def __init__(self, n_fft=2048, hop_length=None, win_length=None,
        window='hann', center=True, pad_mode='reflect', freeze_parameters=True):
        r"""PyTorch implementation of STFT with Conv1d. The function has the 
        same output as librosa.stft.

        Args:
            n_fft: int, fft window size, e.g., 2048
            hop_length: int, hop length samples, e.g., 441
            win_length: int, window length e.g., 2048
            window: str, window function name, e.g., 'hann'
            center: bool
            pad_mode: str, e.g., 'reflect'
            freeze_parameters: bool, set to True to freeze all parameters. Set
                to False to finetune all parameters.
        """
        super(STFT, self).__init__()

        assert pad_mode in ['constant', 'reflect']

        self.n_fft = n_fft
        self.hop_length = hop_length
        self.win_length = win_length
        self.window = window
        self.center = center
        self.pad_mode = pad_mode

        # By default, use the entire frame.
        if self.win_length is None:
            self.win_length = n_fft

        # Set the default hop, if it's not already specified.
        if self.hop_length is None:
            self.hop_length = int(self.win_length // 4)

        fft_window = librosa.filters.get_window(window, self.win_length, fftbins=True)

        # Pad the window out to n_fft size.
        fft_window = librosa.util.pad_center(fft_window, size=n_fft)

        # DFT & IDFT matrix.
        self.W = self.dft_matrix(n_fft)

        out_channels = n_fft // 2 + 1

        self.conv_real = nn.Conv1d(in_channels=1, out_channels=out_channels,
            kernel_size=n_fft, stride=self.hop_length, padding=0, dilation=1,
            groups=1, bias=False)

        self.conv_imag = nn.Conv1d(in_channels=1, out_channels=out_channels,
            kernel_size=n_fft, stride=self.hop_length, padding=0, dilation=1,
            groups=1, bias=False)

        # Initialize Conv1d weights.
        self.conv_real.weight.data = torch.Tensor(
            np.real(self.W[:, 0 : out_channels] * fft_window[:, None]).T)[:, None, :]
        # (n_fft // 2 + 1, 1, n_fft)

        self.conv_imag.weight.data = torch.Tensor(
            np.imag(self.W[:, 0 : out_channels] * fft_window[:, None]).T)[:, None, :]
        # (n_fft // 2 + 1, 1, n_fft)

        if freeze_parameters:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, input):
        r"""Calculate STFT of batch of signals.

        Args: 
            input: (batch_size, data_length), input signals.

        Returns:
            real: (batch_size, 1, time_steps, n_fft // 2 + 1)
            imag: (batch_size, 1, time_steps, n_fft // 2 + 1)
        """

        x = input[:, None, :]   # (batch_size, channels_num, data_length)

        if self.center:
            x = F.pad(x, pad=(self.n_fft // 2, self.n_fft // 2), mode=self.pad_mode)

        real = self.conv_real(x)
        imag = self.conv_imag(x)
        # (batch_size, n_fft // 2 + 1, time_steps)

        real = real[:, None, :, :].transpose(2, 3)
        imag = imag[:, None, :, :].transpose(2, 3)
        # (batch_size, 1, time_steps, n_fft // 2 + 1)

        return real, imag


def magphase(real, imag):
    r"""Calculate magnitude and phase from real and imag part of signals.

    Args:
        real: tensor, real part of signals
        imag: tensor, imag part of signals

    Returns:
        mag: tensor, magnitude of signals
        cos: tensor, cosine of phases of signals
        sin: tensor, sine of phases of signals
    """
    mag = (real ** 2 + imag ** 2) ** 0.5
    cos = real / torch.clamp(mag, 1e-10, np.inf)
    sin = imag / torch.clamp(mag, 1e-10, np.inf)

    return mag, cos, sin


class ISTFT(DFTBase):
    def __init__(self, n_fft=2048, hop_length=None, win_length=None,
        window='hann', center=True, pad_mode='reflect', freeze_parameters=True, 
        onnx=False, frames_num=None, device=None):
        """PyTorch implementation of ISTFT with Conv1d. The function has the 
        same output as librosa.istft.

        Args:
            n_fft: int, fft window size, e.g., 2048
            hop_length: int, hop length samples, e.g., 441
            win_length: int, window length e.g., 2048
            window: str, window function name, e.g., 'hann'
            center: bool
            pad_mode: str, e.g., 'reflect'
            freeze_parameters: bool, set to True to freeze all parameters. Set
                to False to finetune all parameters.
            onnx: bool, set to True when exporting trained model to ONNX. This
                will replace several operations to operators supported by ONNX.
            frames_num: None | int, number of frames of audio clips to be 
                inferneced. Only useable when onnx=True.
            device: None | str, device of ONNX. Only useable when onnx=True.
        """
        super(ISTFT, self).__init__()

        assert pad_mode in ['constant', 'reflect']

        if not onnx:
            assert frames_num is None, "When onnx=False, frames_num must be None!"
            assert device is None, "When onnx=False, device must be None!"

        self.n_fft = n_fft
        self.hop_length = hop_length
        self.win_length = win_length
        self.window = window
        self.center = center
        self.pad_mode = pad_mode
        self.onnx = onnx

        # By default, use the entire frame.
        if self.win_length is None:
            self.win_length = self.n_fft

        # Set the default hop, if it's not already specified.
        if self.hop_length is None:
            self.hop_length = int(self.win_length // 4)

        # Initialize Conv1d modules for calculating real and imag part of DFT.
        self.init_real_imag_conv()

        # Initialize overlap add window for reconstruct time domain signals.
        self.init_overlap_add_window()

        if self.onnx:
            # Initialize ONNX modules.
            self.init_onnx_modules(frames_num, device)
        
        if freeze_parameters:
            for param in self.parameters():
                param.requires_grad = False

    def init_real_imag_conv(self):
        r"""Initialize Conv1d for calculating real and imag part of DFT.
        """
        self.W = self.idft_matrix(self.n_fft) / self.n_fft

        self.conv_real = nn.Conv1d(in_channels=self.n_fft, out_channels=self.n_fft,
            kernel_size=1, stride=1, padding=0, dilation=1,
            groups=1, bias=False)

        self.conv_imag = nn.Conv1d(in_channels=self.n_fft, out_channels=self.n_fft,
            kernel_size=1, stride=1, padding=0, dilation=1,
            groups=1, bias=False)

        ifft_window = librosa.filters.get_window(self.window, self.win_length, fftbins=True)
        # (win_length,)

        # Pad the window to n_fft
        ifft_window = librosa.util.pad_center(ifft_window, size=self.n_fft)

        self.conv_real.weight.data = torch.Tensor(
            np.real(self.W * ifft_window[None, :]).T)[:, :, None]
        # (n_fft // 2 + 1, 1, n_fft)

        self.conv_imag.weight.data = torch.Tensor(
            np.imag(self.W * ifft_window[None, :]).T)[:, :, None]
        # (n_fft // 2 + 1, 1, n_fft)

    def init_overlap_add_window(self):
        r"""Initialize overlap add window for reconstruct time domain signals.
        """
        
        ola_window = librosa.filters.get_window(self.window, self.win_length, fftbins=True)
        # (win_length,)

        ola_window = librosa.util.normalize(ola_window, norm=None) ** 2
        ola_window = librosa.util.pad_center(ola_window, size=self.n_fft)
        ola_window = torch.Tensor(ola_window)

        self.register_buffer('ola_window', ola_window)
        # (win_length,)

    def init_onnx_modules(self, frames_num, device):
        r"""Initialize ONNX modules.

        Args:
            frames_num: int
            device: str | None
        """

        # Use Conv1d to implement torch.flip(), because torch.flip() is not 
        # supported by ONNX.
        self.reverse = nn.Conv1d(in_channels=self.n_fft // 2 + 1,
            out_channels=self.n_fft // 2 - 1, kernel_size=1, bias=False)

        tmp = np.zeros((self.n_fft // 2 - 1, self.n_fft // 2 + 1, 1))
        tmp[:, 1 : -1, 0] = np.array(np.eye(self.n_fft // 2 - 1)[::-1])
        self.reverse.weight.data = torch.Tensor(tmp)
        # (n_fft // 2 - 1, n_fft // 2 + 1, 1)

        # Use nn.ConvTranspose2d to implement torch.nn.functional.fold(), 
        # because torch.nn.functional.fold() is not supported by ONNX.
        self.overlap_add = nn.ConvTranspose2d(in_channels=self.n_fft,
            out_channels=1, kernel_size=(self.n_fft, 1), stride=(self.hop_length, 1), bias=False)

        self.overlap_add.weight.data = torch.Tensor(np.eye(self.n_fft)[:, None, :, None])
        # (n_fft, 1, n_fft, 1)

        if frames_num:
            # Pre-calculate overlap-add window sum for reconstructing signals
            # when using ONNX.
            self.ifft_window_sum = self._get_ifft_window_sum_onnx(frames_num, device)
        else:
            self.ifft_window_sum = []

    def forward(self, real_stft, imag_stft, length):
        r"""Calculate inverse STFT.

        Args:
            real_stft: (batch_size, channels=1, time_steps, n_fft // 2 + 1)
            imag_stft: (batch_size, channels=1, time_steps, n_fft // 2 + 1)
            length: int
        
        Returns:
            real: (batch_size, data_length), output signals.
        """
        assert real_stft.ndimension() == 4 and imag_stft.ndimension() == 4
        batch_size, _, frames_num, _ = real_stft.shape

        real_stft = real_stft[:, 0, :, :].transpose(1, 2)
        imag_stft = imag_stft[:, 0, :, :].transpose(1, 2)
        # (batch_size, n_fft // 2 + 1, time_steps)

        # Get full stft representation from spectrum using symmetry attribute.
        if self.onnx:
            full_real_stft, full_imag_stft = self._get_full_stft_onnx(real_stft, imag_stft)
        else:
            full_real_stft, full_imag_stft = self._get_full_stft(real_stft, imag_stft)
        # full_real_stft: (batch_size, n_fft, time_steps)
        # full_imag_stft: (batch_size, n_fft, time_steps)

        # Calculate IDFT frame by frame.
        s_real = self.conv_real(full_real_stft) - self.conv_imag(full_imag_stft)
        # (batch_size, n_fft, time_steps)

        # Overlap add signals in frames to reconstruct signals.
        if self.onnx:
            y = self._overlap_add_divide_window_sum_onnx(s_real, frames_num)
        else:
            y = self._overlap_add_divide_window_sum(s_real, frames_num)
        # y: (batch_size, audio_samples + win_length,)
        
        y = self._trim_edges(y, length)
        # (batch_size, audio_samples,)
            
        return y

    def _get_full_stft(self, real_stft, imag_stft):
        r"""Get full stft representation from spectrum using symmetry attribute.

        Args:
            real_stft: (batch_size, n_fft // 2 + 1, time_steps)
            imag_stft: (batch_size, n_fft // 2 + 1, time_steps)

        Returns:
            full_real_stft: (batch_size, n_fft, time_steps)
            full_imag_stft: (batch_size, n_fft, time_steps)
        """
        full_real_stft = torch.cat((real_stft, torch.flip(real_stft[:, 1 : -1, :], dims=[1])), dim=1)
        full_imag_stft = torch.cat((imag_stft, - torch.flip(imag_stft[:, 1 : -1, :], dims=[1])), dim=1)

        return full_real_stft, full_imag_stft

    def _get_full_stft_onnx(self, real_stft, imag_stft):
        r"""Get full stft representation from spectrum using symmetry attribute
        for ONNX. Replace several pytorch operations in self._get_full_stft() 
        that are not supported by ONNX.

        Args:
            real_stft: (batch_size, n_fft // 2 + 1, time_steps)
            imag_stft: (batch_size, n_fft // 2 + 1, time_steps)

        Returns:
            full_real_stft: (batch_size, n_fft, time_steps)
            full_imag_stft: (batch_size, n_fft, time_steps)
        """

        # Implement torch.flip() with Conv1d.
        full_real_stft = torch.cat((real_stft, self.reverse(real_stft)), dim=1)
        full_imag_stft = torch.cat((imag_stft, - self.reverse(imag_stft)), dim=1)

        return full_real_stft, full_imag_stft

    def _overlap_add_divide_window_sum(self, s_real, frames_num):
        r"""Overlap add signals in frames to reconstruct signals.

        Args:
            s_real: (batch_size, n_fft, time_steps), signals in frames
            frames_num: int

        Returns:
            y: (batch_size, audio_samples)
        """
        
        output_samples = (s_real.shape[-1] - 1) * self.hop_length + self.win_length
        # (audio_samples,)

        # Overlap-add signals in frames to signals. Ref: 
        # asteroid_filterbanks.torch_stft_fb.torch_stft_fb() from
        # https://github.com/asteroid-team/asteroid-filterbanks
        y = torch.nn.functional.fold(input=s_real, output_size=(1, output_samples), 
            kernel_size=(1, self.win_length), stride=(1, self.hop_length))
        # (batch_size, 1, 1, audio_samples,)
        
        y = y[:, 0, 0, :]
        # (batch_size, audio_samples)

        # Get overlap-add window sum to be divided.
        ifft_window_sum = self._get_ifft_window(frames_num)
        # (audio_samples,)

        # Following code is abandaned for divide overlap-add window, because
        # not supported by half precision training and ONNX.
        # min_mask = ifft_window_sum.abs() < 1e-11
        # y[:, ~min_mask] = y[:, ~min_mask] / ifft_window_sum[None, ~min_mask]
        # # (batch_size, audio_samples)

        ifft_window_sum = torch.clamp(ifft_window_sum, 1e-11, np.inf)
        # (audio_samples,)

        y = y / ifft_window_sum[None, :]
        # (batch_size, audio_samples,)

        return y

    def _get_ifft_window(self, frames_num):
        r"""Get overlap-add window sum to be divided.

        Args:
            frames_num: int

        Returns:
            ifft_window_sum: (audio_samlpes,), overlap-add window sum to be 
            divided.
        """
        
        output_samples = (frames_num - 1) * self.hop_length + self.win_length
        # (audio_samples,)

        window_matrix = self.ola_window[None, :, None].repeat(1, 1, frames_num)
        # (batch_size, win_length, time_steps)

        ifft_window_sum = F.fold(input=window_matrix, 
            output_size=(1, output_samples), kernel_size=(1, self.win_length), 
            stride=(1, self.hop_length))
        # (1, 1, 1, audio_samples)
        
        ifft_window_sum = ifft_window_sum.squeeze()
        # (audio_samlpes,)

        return ifft_window_sum

    def _overlap_add_divide_window_sum_onnx(self, s_real, frames_num):
        r"""Overlap add signals in frames to reconstruct signals for ONNX. 
        Replace several pytorch operations in 
        self._overlap_add_divide_window_sum() that are not supported by ONNX.

        Args:
            s_real: (batch_size, n_fft, time_steps), signals in frames
            frames_num: int

        Returns:
            y: (batch_size, audio_samples)
        """

        s_real = s_real[..., None]
        # (batch_size, n_fft, time_steps, 1)

        # Implement overlap-add with Conv1d, because torch.nn.functional.fold()
        # is not supported by ONNX.
        y = self.overlap_add(s_real)[:, 0, :, 0]    
        # y: (batch_size, samples_num)
        
        if len(self.ifft_window_sum) != y.shape[1]:
            device = s_real.device

            self.ifft_window_sum = self._get_ifft_window_sum_onnx(frames_num, device)
            # (audio_samples,)

        # Use torch.clamp() to prevent from underflow to make sure all 
        # operations are supported by ONNX.
        ifft_window_sum = torch.clamp(self.ifft_window_sum, 1e-11, np.inf)
        # (audio_samples,)

        y = y / ifft_window_sum[None, :]
        # (batch_size, audio_samples,)
        
        return y

    def _get_ifft_window_sum_onnx(self, frames_num, device):
        r"""Pre-calculate overlap-add window sum for reconstructing signals when
        using ONNX.

        Args:
            frames_num: int
            device: str | None

        Returns:
            ifft_window_sum: (audio_samples,)
        """
        
        ifft_window_sum = librosa.filters.window_sumsquare(window=self.window, 
            n_frames=frames_num, win_length=self.win_length, n_fft=self.n_fft, 
            hop_length=self.hop_length)
        # (audio_samples,)

        ifft_window_sum = torch.Tensor(ifft_window_sum)

        if device:
            ifft_window_sum = ifft_window_sum.to(device)

        return ifft_window_sum

    def _trim_edges(self, y, length):
        r"""Trim audio.

        Args:
            y: (audio_samples,)
            length: int

        Returns:
            (trimmed_audio_samples,)
        """
        # Trim or pad to length
        if length is None:
            if self.center:
                y = y[:, self.n_fft // 2 : -self.n_fft // 2]
        else:
            if self.center:
                start = self.n_fft // 2
            else:
                start = 0

            y = y[:, start : start + length]

        return y


class Spectrogram(nn.Module):
    def __init__(self, n_fft=2048, hop_length=None, win_length=None,
        window='hann', center=True, pad_mode='reflect', power=2.0,
        freeze_parameters=True):
        r"""Calculate spectrogram using pytorch. The STFT is implemented with 
        Conv1d. The function has the same output of librosa.stft
        """
        super(Spectrogram, self).__init__()

        self.power = power

        self.stft = STFT(n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center,
            pad_mode=pad_mode, freeze_parameters=True)

    def forward(self, input):
        r"""Calculate spectrogram of input signals.
        Args: 
            input: (batch_size, data_length)

        Returns:
            spectrogram: (batch_size, 1, time_steps, n_fft // 2 + 1)
        """

        (real, imag) = self.stft.forward(input)
        # (batch_size, n_fft // 2 + 1, time_steps)

        spectrogram = real ** 2 + imag ** 2

        if self.power == 2.0:
            pass
        else:
            spectrogram = spectrogram ** (self.power / 2.0)

        return spectrogram


class LogmelFilterBank(nn.Module):
    def __init__(self, sr=22050, n_fft=2048, n_mels=64, fmin=0.0, fmax=None, 
        is_log=True, ref=1.0, amin=1e-10, top_db=80.0, freeze_parameters=True):
        r"""Calculate logmel spectrogram using pytorch. The mel filter bank is 
        the pytorch implementation of as librosa.filters.mel 
        """
        super(LogmelFilterBank, self).__init__()

        self.is_log = is_log
        self.ref = ref
        self.amin = amin
        self.top_db = top_db
        if fmax == None:
            fmax = sr//2

        self.melW = librosa.filters.mel(sr=sr, n_fft=n_fft, n_mels=n_mels,
            fmin=fmin, fmax=fmax).T
        # (n_fft // 2 + 1, mel_bins)

        self.melW = nn.Parameter(torch.Tensor(self.melW))

        if freeze_parameters:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, input):
        r"""Calculate (log) mel spectrogram from spectrogram.

        Args:
            input: (*, n_fft), spectrogram
        
        Returns: 
            output: (*, mel_bins), (log) mel spectrogram
        """

        # Mel spectrogram
        mel_spectrogram = torch.matmul(input, self.melW)
        # (*, mel_bins)

        # Logmel spectrogram
        if self.is_log:
            output = self.power_to_db(mel_spectrogram)
        else:
            output = mel_spectrogram

        return output


    def power_to_db(self, input):
        r"""Power to db, this function is the pytorch implementation of 
        librosa.power_to_lb
        """
        ref_value = self.ref
        log_spec = 10.0 * torch.log10(torch.clamp(input, min=self.amin, max=np.inf))
        log_spec -= 10.0 * np.log10(np.maximum(self.amin, ref_value))

        if self.top_db is not None:
            if self.top_db < 0:
                raise librosa.util.exceptions.ParameterError('top_db must be non-negative')
            log_spec = torch.clamp(log_spec, min=log_spec.max().item() - self.top_db, max=np.inf)

        return log_spec


class Enframe(nn.Module):
    def __init__(self, frame_length=2048, hop_length=512):
        r"""Enframe a time sequence. This function is the pytorch implementation 
        of librosa.util.frame
        """
        super(Enframe, self).__init__()

        self.enframe_conv = nn.Conv1d(in_channels=1, out_channels=frame_length,
            kernel_size=frame_length, stride=hop_length,
            padding=0, bias=False)

        self.enframe_conv.weight.data = torch.Tensor(torch.eye(frame_length)[:, None, :])
        self.enframe_conv.weight.requires_grad = False

    def forward(self, input):
        r"""Enframe signals into frames.
        Args:
            input: (batch_size, samples)
        
        Returns: 
            output: (batch_size, window_length, frames_num)
        """
        output = self.enframe_conv(input[:, None, :])
        return output


    def power_to_db(self, input):
        r"""Power to db, this function is the pytorch implementation of 
        librosa.power_to_lb.
        """
        ref_value = self.ref
        log_spec = 10.0 * torch.log10(torch.clamp(input, min=self.amin, max=np.inf))
        log_spec -= 10.0 * np.log10(np.maximum(self.amin, ref_value))

        if self.top_db is not None:
            if self.top_db < 0:
                raise librosa.util.exceptions.ParameterError('top_db must be non-negative')
            log_spec = torch.clamp(log_spec, min=log_spec.max() - self.top_db, max=np.inf)

        return log_spec


class Scalar(nn.Module):
    def __init__(self, scalar, freeze_parameters):
        super(Scalar, self).__init__()

        self.scalar_mean = Parameter(torch.Tensor(scalar['mean']))
        self.scalar_std = Parameter(torch.Tensor(scalar['std']))

        if freeze_parameters:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, input):
        return (input - self.scalar_mean) / self.scalar_std


def debug(select, device):
    """Compare numpy + librosa and torchlibrosa results. For debug. 

    Args:
        select: 'dft' | 'logmel'
        device: 'cpu' | 'cuda'
    """

    if select == 'dft':
        n = 10
        norm = None     # None | 'ortho'
        np.random.seed(0)

        # Data
        np_data = np.random.uniform(-1, 1, n)
        pt_data = torch.Tensor(np_data)

        # Numpy FFT
        np_fft = np.fft.fft(np_data, norm=norm)
        np_ifft = np.fft.ifft(np_fft, norm=norm)
        np_rfft = np.fft.rfft(np_data, norm=norm)
        np_irfft = np.fft.ifft(np_rfft, norm=norm)

        # Pytorch FFT
        obj = DFT(n, norm)
        pt_dft = obj.dft(pt_data, torch.zeros_like(pt_data))
        pt_idft = obj.idft(pt_dft[0], pt_dft[1])
        pt_rdft = obj.rdft(pt_data)
        pt_irdft = obj.irdft(pt_rdft[0], pt_rdft[1])

        print('Comparing librosa and pytorch implementation of DFT. All numbers '
            'below should be close to 0.')
        print(np.mean((np.abs(np.real(np_fft) - pt_dft[0].cpu().numpy()))))
        print(np.mean((np.abs(np.imag(np_fft) - pt_dft[1].cpu().numpy()))))

        print(np.mean((np.abs(np.real(np_ifft) - pt_idft[0].cpu().numpy()))))
        print(np.mean((np.abs(np.imag(np_ifft) - pt_idft[1].cpu().numpy()))))

        print(np.mean((np.abs(np.real(np_rfft) - pt_rdft[0].cpu().numpy()))))
        print(np.mean((np.abs(np.imag(np_rfft) - pt_rdft[1].cpu().numpy()))))

        print(np.mean(np.abs(np_data - pt_irdft.cpu().numpy())))

    elif select == 'stft':
        device = torch.device(device)
        np.random.seed(0)

        # Spectrogram parameters (the same as librosa.stft)
        sample_rate = 22050
        data_length = sample_rate * 1
        n_fft = 2048
        hop_length = 512
        win_length = 2048
        window = 'hann'
        center = True
        pad_mode = 'reflect'

        # Data
        np_data = np.random.uniform(-1, 1, data_length)
        pt_data = torch.Tensor(np_data).to(device)

        # Numpy stft matrix
        np_stft_matrix = librosa.stft(y=np_data, n_fft=n_fft,
            hop_length=hop_length, window=window, center=center).T

        # Pytorch stft matrix
        pt_stft_extractor = STFT(n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center, pad_mode=pad_mode,
            freeze_parameters=True)

        pt_stft_extractor.to(device)

        (pt_stft_real, pt_stft_imag) = pt_stft_extractor.forward(pt_data[None, :])

        print('Comparing librosa and pytorch implementation of STFT & ISTFT. \
            All numbers below should be close to 0.')
        print(np.mean(np.abs(np.real(np_stft_matrix) - pt_stft_real.data.cpu().numpy()[0, 0])))
        print(np.mean(np.abs(np.imag(np_stft_matrix) - pt_stft_imag.data.cpu().numpy()[0, 0])))

        # Numpy istft
        np_istft_s = librosa.istft(stft_matrix=np_stft_matrix.T,
            hop_length=hop_length, window=window, center=center, length=data_length)

        # Pytorch istft
        pt_istft_extractor = ISTFT(n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center, pad_mode=pad_mode,
            freeze_parameters=True)
        pt_istft_extractor.to(device)

        # Recover from real and imag part
        pt_istft_s = pt_istft_extractor.forward(pt_stft_real, pt_stft_imag, data_length)[0, :]

        # Recover from magnitude and phase
        (pt_stft_mag, cos, sin) = magphase(pt_stft_real, pt_stft_imag)
        pt_istft_s2 = pt_istft_extractor.forward(pt_stft_mag * cos, pt_stft_mag * sin, data_length)[0, :]

        print(np.mean(np.abs(np_istft_s - pt_istft_s.data.cpu().numpy())))
        print(np.mean(np.abs(np_data - pt_istft_s.data.cpu().numpy())))
        print(np.mean(np.abs(np_data - pt_istft_s2.data.cpu().numpy())))

    elif select == 'logmel':
        dtype = np.complex64
        device = torch.device(device)
        np.random.seed(0)

        # Spectrogram parameters (the same as librosa.stft)
        sample_rate = 22050
        data_length = sample_rate * 1
        n_fft = 2048
        hop_length = 512
        win_length = 2048
        window = 'hann'
        center = True
        pad_mode = 'reflect'

        # Mel parameters (the same as librosa.feature.melspectrogram)
        n_mels = 128
        fmin = 0.
        fmax = sample_rate / 2.0

        # Power to db parameters (the same as default settings of librosa.power_to_db
        ref = 1.0
        amin = 1e-10
        top_db = 80.0

        # Data
        np_data = np.random.uniform(-1, 1, data_length)
        pt_data = torch.Tensor(np_data).to(device)

        print('Comparing librosa and pytorch implementation of logmel '
            'spectrogram. All numbers below should be close to 0.')

        # Numpy librosa
        np_stft_matrix = librosa.stft(y=np_data, n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center, dtype=dtype,
            pad_mode=pad_mode)

        np_pad = np.pad(np_data, int(n_fft // 2), mode=pad_mode)

        np_melW = librosa.filters.mel(sr=sample_rate, n_fft=n_fft, n_mels=n_mels,
            fmin=fmin, fmax=fmax).T

        np_mel_spectrogram = np.dot(np.abs(np_stft_matrix.T) ** 2, np_melW)

        np_logmel_spectrogram = librosa.power_to_db(
            np_mel_spectrogram, ref=ref, amin=amin, top_db=top_db)

        # Pytorch
        stft_extractor = STFT(n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center, pad_mode=pad_mode,
            freeze_parameters=True)

        logmel_extractor = LogmelFilterBank(sr=sample_rate, n_fft=n_fft,
            n_mels=n_mels, fmin=fmin, fmax=fmax, ref=ref, amin=amin,
            top_db=top_db, freeze_parameters=True)

        stft_extractor.to(device)
        logmel_extractor.to(device)

        pt_pad = F.pad(pt_data[None, None, :], pad=(n_fft // 2, n_fft // 2), mode=pad_mode)[0, 0]
        print(np.mean(np.abs(np_pad - pt_pad.cpu().numpy())))

        pt_stft_matrix_real = stft_extractor.conv_real(pt_pad[None, None, :])[0]
        pt_stft_matrix_imag = stft_extractor.conv_imag(pt_pad[None, None, :])[0]
        print(np.mean(np.abs(np.real(np_stft_matrix) - pt_stft_matrix_real.data.cpu().numpy())))
        print(np.mean(np.abs(np.imag(np_stft_matrix) - pt_stft_matrix_imag.data.cpu().numpy())))

        # Spectrogram
        spectrogram_extractor = Spectrogram(n_fft=n_fft, hop_length=hop_length,
            win_length=win_length, window=window, center=center, pad_mode=pad_mode,
            freeze_parameters=True)

        spectrogram_extractor.to(device)

        pt_spectrogram = spectrogram_extractor.forward(pt_data[None, :])
        pt_mel_spectrogram = torch.matmul(pt_spectrogram, logmel_extractor.melW)
        print(np.mean(np.abs(np_mel_spectrogram - pt_mel_spectrogram.data.cpu().numpy()[0, 0])))

        # Log mel spectrogram
        pt_logmel_spectrogram = logmel_extractor.forward(pt_spectrogram)
        print(np.mean(np.abs(np_logmel_spectrogram - pt_logmel_spectrogram[0, 0].data.cpu().numpy())))

    elif select == 'enframe':
        device = torch.device(device)
        np.random.seed(0)

        # Spectrogram parameters (the same as librosa.stft)
        sample_rate = 22050
        data_length = sample_rate * 1
        hop_length = 512
        win_length = 2048

        # Data
        np_data = np.random.uniform(-1, 1, data_length)
        pt_data = torch.Tensor(np_data).to(device)

        print('Comparing librosa and pytorch implementation of '
            'librosa.util.frame. All numbers below should be close to 0.')

        # Numpy librosa
        np_frames = librosa.util.frame(np_data, frame_length=win_length,
            hop_length=hop_length)

        # Pytorch
        pt_frame_extractor = Enframe(frame_length=win_length, hop_length=hop_length)
        pt_frame_extractor.to(device)

        pt_frames = pt_frame_extractor(pt_data[None, :])
        print(np.mean(np.abs(np_frames - pt_frames.data.cpu().numpy())))

    elif select == 'default':
        device = torch.device(device)
        np.random.seed(0)

        # Spectrogram parameters (the same as librosa.stft)
        sample_rate = 22050
        data_length = sample_rate * 1
        hop_length = 512
        win_length = 2048

        # Mel parameters (the same as librosa.feature.melspectrogram)
        n_mels = 128

        # Data
        np_data = np.random.uniform(-1, 1, data_length)
        pt_data = torch.Tensor(np_data).to(device)

        feature_extractor = nn.Sequential(
            Spectrogram(
                hop_length=hop_length,
                win_length=win_length,
            ), LogmelFilterBank(
                sr=sample_rate,
                n_mels=n_mels,
                is_log=False, #Default is true
            ))

        feature_extractor.to(device)

        print(
            'Comparing default mel spectrogram from librosa to the pytorch implementation.'
        )

        # Numpy librosa
        np_melspect = librosa.feature.melspectrogram(np_data,
                                                     hop_length=hop_length,
                                                     sr=sample_rate,
                                                     win_length=win_length,
                                                     n_mels=n_mels).T
        #Pytorch
        pt_melspect = feature_extractor(pt_data[None, :]).squeeze()
        passed = np.allclose(pt_melspect.data.to('cpu').numpy(), np_melspect)
        print(f"Passed? {passed}")



if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--device', type=str, default='cpu', choices=['cpu', 'cuda'])
    args = parser.parse_args()

    device = args.device
    norm = None     # None | 'ortho'
    np.random.seed(0)

    # Spectrogram parameters (the same as librosa.stft)
    sample_rate = 22050
    data_length = sample_rate * 1
    n_fft = 2048
    hop_length = 512
    win_length = 2048
    window = 'hann'
    center = True
    pad_mode = 'reflect'

    # Mel parameters (the same as librosa.feature.melspectrogram)
    n_mels = 128
    fmin = 0.
    fmax = sample_rate / 2.0

    # Power to db parameters (the same as default settings of librosa.power_to_db
    ref = 1.0
    amin = 1e-10
    top_db = 80.0

    # Data
    np_data = np.random.uniform(-1, 1, data_length)
    pt_data = torch.Tensor(np_data).to(device)

    # Pytorch
    spectrogram_extractor = Spectrogram(n_fft=n_fft, hop_length=hop_length,
        win_length=win_length, window=window, center=center, pad_mode=pad_mode,
        freeze_parameters=True)

    logmel_extractor = LogmelFilterBank(sr=sample_rate, n_fft=n_fft,
        n_mels=n_mels, fmin=fmin, fmax=fmax, ref=ref, amin=amin, top_db=top_db,
        freeze_parameters=True)

    spectrogram_extractor.to(device)
    logmel_extractor.to(device)

    # Spectrogram
    pt_spectrogram = spectrogram_extractor.forward(pt_data[None, :])

    # Log mel spectrogram
    pt_logmel_spectrogram = logmel_extractor.forward(pt_spectrogram)

    # Uncomment for debug
    if True:
        debug(select='dft', device=device)
        debug(select='stft', device=device)
        debug(select='logmel', device=device)
        debug(select='enframe', device=device)

        try:
            debug(select='default', device=device)
        except:
            raise Exception('Torchlibrosa does support librosa>=0.6.0, for \
                comparison with librosa, please use librosa>=0.7.0!')