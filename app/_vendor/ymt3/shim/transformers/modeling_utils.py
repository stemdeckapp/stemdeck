# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# Written for StemDeck: a small rewrite, no code of the original.
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

import torch
from torch import nn


class PreTrainedModel(nn.Module):
    config_class = None
    base_model_prefix = ""
    main_input_name = "input_ids"

    def __init__(self, config, *args, **kwargs):
        super().__init__()
        self.config = config

    def post_init(self):  # weights come from the checkpoint
        pass

    @property
    def dtype(self):
        return next(self.parameters()).dtype

    @property
    def device(self):
        return next(self.parameters()).device

    def invert_attention_mask(self, encoder_attention_mask):
        if encoder_attention_mask.dim() == 3:
            ext = encoder_attention_mask[:, None, :, :]
        else:
            ext = encoder_attention_mask[:, None, None, :]
        ext = ext.to(dtype=self.dtype)
        return (1.0 - ext) * torch.finfo(self.dtype).min

    def get_extended_attention_mask(self, attention_mask, input_shape, device=None, dtype=None):
        dtype = dtype or self.dtype
        if attention_mask.dim() == 3:
            ext = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            if self.config.is_decoder:
                batch_size, seq_length = input_shape
                device = device or attention_mask.device
                seq_ids = torch.arange(seq_length, device=device)
                causal = seq_ids[None, None, :].repeat(batch_size, seq_length, 1) <= seq_ids[None, :, None]
                causal = causal.to(attention_mask.dtype)
                if causal.shape[1] < attention_mask.shape[1]:
                    prefix = attention_mask.shape[1] - causal.shape[1]
                    causal = torch.cat([torch.ones((batch_size, seq_length, prefix), device=device,
                                                   dtype=causal.dtype), causal], axis=-1)
                ext = causal[:, None, :, :] * attention_mask[:, None, None, :]
            else:
                ext = attention_mask[:, None, None, :]
        else:
            raise ValueError("bad attention mask shape")
        ext = ext.to(dtype=dtype)
        return (1.0 - ext) * torch.finfo(dtype).min

    def get_head_mask(self, head_mask, num_hidden_layers, is_attention_chunked=False):
        if head_mask is not None:
            raise NotImplementedError("head masks are not used at inference")
        return [None] * num_hidden_layers
