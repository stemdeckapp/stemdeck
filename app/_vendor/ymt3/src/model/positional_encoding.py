"""positional_encoding.py """
from typing import Optional, Literal
from inspect import isfunction
from math import log, log2, pi, floor

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn, einsum
from einops import rearrange, repeat

from model.RoPE.RoPE import RotaryEmbedding


class AlibiPositionalBias(nn.Module):
    """ 
    Alibi Positional Bias for Transformer Attention
    : modified to support trainalbe slope similar to "little bird" paper, based on 
        https://github.com/lucidrains/x-transformers/ 
        https://github.com/ofirpress/attention_with_linear_biases/issues/5

    This is Alibi positional bias extension for:
        - bi-directional self/cross attention
        - supporting extrapolation.

    References:
        Ofir, Noah A. Smith, and Mike Lewis. "Train short, test long: Attention with linear
          biases enables input length extrapolation." arXiv preprint arXiv:2108.12409 (2021).

        Lee, Minchul, Kijong Han, and Myeong Cheol Shin. "LittleBird: Efficient Faster & Longer
          Transformer for Question Answering." arXiv preprint arXiv:2210.11870 (2022).
    """

    def __init__(self,
                 heads: int = 8,
                 total_heads: int = 8,
                 trainable_slope: bool = False,
                 trainable_slope_init: Literal['random', 'log'] = 'random',
                 **kwargs) -> None:
        super().__init__()
        self.heads = heads  # number of heads to be activated
        self.total_heads = total_heads  # number of heads in attention module
        self.trainable_slope = trainable_slope
        self.trainable_slope_init = trainable_slope_init

        if trainable_slope:
            self.slopes = nn.Parameter(torch.Tensor(heads, 1, 1), requires_grad=True)
        else:
            slopes = torch.Tensor(self._get_slopes(heads))
            slopes = rearrange(slopes, 'h -> h 1 1')
            self.register_buffer('slopes', slopes, persistent=False)

        self.register_buffer('bias', None, persistent=False)

    def reset_parameters(self) -> None:
        if self.trainable_slope:
            if self.trainable_slope_init == 'random':
                nn.init.normal_(self.slopes, -2, 1)
            else:
                raise NotImplementedError(f'Unknown trainable_slope_init: {self.trainable_slope_init}')

    def get_bias(self, i, j, device):
        i_arange = torch.arange(j - i, j, device=device)
        j_arange = torch.arange(j, device=device)
        bias = -torch.abs(rearrange(j_arange, 'j -> 1 1 j') - rearrange(i_arange, 'i -> 1 i 1'))
        return bias

    @staticmethod
    def _get_slopes(heads):

        def get_slopes_power_of_2(n):
            start = (2**(-2**-(log2(n) - 3)))
            ratio = start
            return [start * ratio**i for i in range(n)]

        if log2(heads).is_integer():
            return get_slopes_power_of_2(heads)

        closest_power_of_2 = 2**floor(log2(heads))
        return get_slopes_power_of_2(closest_power_of_2) + get_slopes_power_of_2(
            2 * closest_power_of_2)[0::2][:heads - closest_power_of_2]

    @staticmethod
    def pad_at_dim(t, pad, dim=-1, value=0.):
        dims_from_right = (-dim - 1) if dim < 0 else (t.ndim - dim - 1)
        zeros = ((0, 0) * dims_from_right)
        return F.pad(t, (*zeros, *pad), value=value)

    @property
    def device(self):
        if self.trainable_slope:
            return self.slopes.device
        else:
            return next(self.buffers()).device

    def forward(self, i, j):
        """
        Args:
            i (int): end index of query
            j (int): end index of key 

        Returns:
            torch.Tensor: (num_total_heads, i, j) positional bias for each head

        Usage:
            >>> alibi_bias = AlibiPositionalBias(heads=8, total_heads=8, trainable_slope=False)
            >>> pos_bias = alibi_bias(len(q), len(k))
            >>> q_dot_k = ...
            >>> q_dot_k += pos_bias
            >>> q_dot_k = q_dot_k.softmax(dim=-1)

        """
        h, device = self.total_heads, self.device
        if self.trainable_slope:
            if self.bias is not None and self.bias.shape[-1] >= j and self.bias.shape[-2] >= i:
                bias = self.bias[..., :i, :j]
            else:
                bias = self.get_bias(i, j, device)
                num_heads_unalibied = h - bias.shape[0]
                bias = self.pad_at_dim(bias, (0, num_heads_unalibied), dim=0)
                self.register_buffer('bias', bias, persistent=False)

            return self.bias * torch.sigmoid(self.slopes)

        else:
            if self.bias is not None and self.bias.shape[-1] >= j and self.bias.shape[-2] >= i:
                return self.bias[..., :i, :j]

            bias = self.get_bias(i, j, device)
            bias = bias * self.slopes

            num_heads_unalibied = h - bias.shape[0]
            bias = self.pad_at_dim(bias, (0, num_heads_unalibied), dim=0)
            self.register_buffer('bias', bias, persistent=False)

            return self.bias


class FixedSinusoidalPositionalEmbedding(nn.Embedding):
    """
    Sinusoidal Absolute Positional Embeddings (APE) of any length.
    
    Adapted from transformers.models.marian.modeling_marian.MarianSinusoidalPositionalEmbedding
    
    """

    def __init__(self, num_positions: int, embedding_dim: int, padding_idx: Optional[int] = None):
        super().__init__(num_positions, embedding_dim)
        self.weight = self._init_weight(self.weight)

    @staticmethod
    def _init_weight(out: nn.Parameter):
        """
        Identical to the XLM create_sinusoidal_embeddings except features are not interleaved. The cos features are in
        the 2nd half of the vector. [dim // 2:]
        """
        n_pos, dim = out.shape
        position_enc = np.array([[pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)] for pos in range(n_pos)
                                ])
        out.requires_grad = False  # set early to avoid an error in pytorch-1.8+
        sentinel = dim // 2 if dim % 2 == 0 else (dim // 2) + 1
        out[:, 0:sentinel] = torch.FloatTensor(np.sin(position_enc[:, 0::2]))
        out[:, sentinel:] = torch.FloatTensor(np.cos(position_enc[:, 1::2]))
        out.detach_()
        return out

    @torch.no_grad()
    def forward(self, seq_len: int, past_key_values_length: int = 0):
        """`input_ids_shape` is expected to be [bsz x seqlen]."""
        positions = torch.arange(past_key_values_length,
                                 past_key_values_length + seq_len,
                                 dtype=torch.long,
                                 device=self.weight.device)
        return super().forward(positions)


class Wav2Vec2ConformerRotaryPositionalEmbedding(nn.Module):
    """Rotary positional embedding
    Reference : https://blog.eleuther.ai/rotary-embeddings/ Paper: https://arxiv.org/pdf/2104.09864.pdf
    """

    def __init__(self, config):
        super().__init__()
        dim = config.d_model // config.num_heads
        base = config.rotary_embedding_base

        inv_freq = 1.0 / (base**(torch.arange(0, dim, 2).float() / dim))
        self.register_buffer("inv_freq", inv_freq)
        self.cached_sequence_length = None
        self.cached_rotary_positional_embedding = None

    def forward(self, hidden_states):
        sequence_length = hidden_states.shape[1]

        if sequence_length == self.cached_sequence_length and self.cached_rotary_positional_embedding is not None:
            return self.cached_rotary_positional_embedding

        self.cached_sequence_length = sequence_length
        time_stamps = torch.arange(sequence_length).type_as(self.inv_freq)
        freqs = torch.einsum("i,j->ij", time_stamps, self.inv_freq)
        embeddings = torch.cat((freqs, freqs), dim=-1)

        cos_embeddings = embeddings.cos()[:, None, None, :]
        sin_embeddings = embeddings.sin()[:, None, None, :]
        self.cached_rotary_positional_embedding = torch.stack([cos_embeddings, sin_embeddings])
        return self.cached_rotary_positional_embedding


class Wav2Vec2ConformerRelPositionalEmbedding(nn.Module):
    """Relative positional encoding module."""

    def __init__(self, config):
        super().__init__()
        self.max_len = config.num_max_positions
        self.d_model = config.d_model
        self.pe = None
        self.extend_pe(torch.tensor(0.0).expand(1, self.max_len))

    def extend_pe(self, x):
        # Reset the positional encodings
        if self.pe is not None:
            # self.pe contains both positive and negative parts
            # the length of self.pe is 2 * input_len - 1
            if self.pe.size(1) >= x.size(1) * 2 - 1:
                if self.pe.dtype != x.dtype or self.pe.device != x.device:
                    self.pe = self.pe.to(dtype=x.dtype, device=x.device)
                return
        # Suppose `i` is the position of query vector and `j` is the
        # position of key vector. We use positive relative positions when keys
        # are to the left (i>j) and negative relative positions otherwise (i<j).
        pe_positive = torch.zeros(x.size(1), self.d_model)
        pe_negative = torch.zeros(x.size(1), self.d_model)
        position = torch.arange(0, x.size(1), dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, self.d_model, 2, dtype=torch.float32) * -(log(10000.0) / self.d_model))
        pe_positive[:, 0::2] = torch.sin(position * div_term)
        pe_positive[:, 1::2] = torch.cos(position * div_term)
        pe_negative[:, 0::2] = torch.sin(-1 * position * div_term)
        pe_negative[:, 1::2] = torch.cos(-1 * position * div_term)

        # Reverse the order of positive indices and concat both positive and
        # negative indices. This is used to support the shifting trick
        # as in https://arxiv.org/abs/1901.02860
        pe_positive = torch.flip(pe_positive, [0]).unsqueeze(0)
        pe_negative = pe_negative[1:].unsqueeze(0)
        pe = torch.cat([pe_positive, pe_negative], dim=1)
        self.pe = pe.to(device=x.device, dtype=x.dtype)

    def forward(self, hidden_states: torch.Tensor):
        self.extend_pe(hidden_states)
        start_idx = self.pe.size(1) // 2 - hidden_states.size(1) + 1
        end_idx = self.pe.size(1) // 2 + hidden_states.size(1)
        relative_position_embeddings = self.pe[:, start_idx:end_idx]

        return relative_position_embeddings


#================================================================================================
# Rotary Positional Embedding
#================================================================================================
def get_rotary_emb(d_by_head: int,
                   freqs_for: Literal["l", "lang", "p", "pixel"],
                   partial_pe: bool = False,
                   learned_freq: bool = False):
    if partial_pe is True:
        rdim = d_by_head // 2
    else:
        rdim = d_by_head

    if freqs_for in ["l", "lang"]:
        freqs_for = "lang"
    elif freqs_for in ["p", "pixel"]:
        freqs_for = "pixel"
    else:
        raise ValueError(f"freqs_for must be 'l' or 'lang' or 'p' or 'pixel', but got {freqs_for}")
    return RotaryEmbedding(dim=rdim, freqs_for=freqs_for, learned_freq=learned_freq)


def test_rotary_embedding_lang():
    d = 128
    num_heads = 8
    d_by_head = d // num_heads

    rotary = get_rotary_emb(d_by_head, freqs_for="lang", partial_pe=False, learned_freq=False)
    q = torch.ones(1, 8, 110, d_by_head)
    q = rotary.apply_rotary_custom(q)

    import matplotlib.pyplot as plt
    plt.imshow(q[0, 0, :, :].detach().numpy().T, origin='lower')
