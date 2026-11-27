# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""lm_head.py"""
import torch
from torch import nn
from typing import Optional, Dict


class LMHead(nn.Module):
    """Language Model Head with tied weights."""

    def __init__(self, decoder_config: Dict, init_factor: float = 1.0, tie_word_embeddings: bool = True):

        super().__init__()
        self.d_model = decoder_config["d_model"]
        self.init_factor = init_factor
        self.tie_word_embeddings = tie_word_embeddings

        self.lm_head = nn.Linear(decoder_config["d_model"], decoder_config["vocab_size"], bias=False)
        self._init_weights()

    def _init_weights(self):
        if self.tie_word_embeddings is False:
            self.lm_head.weight.data.normal_(mean=0.0, std=self.init_factor * 1.0)

    def forward(self, decoder_hs: torch.FloatTensor) -> torch.FloatTensor:
        if self.tie_word_embeddings is True:
            # Rescale output before projecting on vocab
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/transformer.py#L586
            decoder_hs = decoder_hs * (self.d_model**-0.5)

        lm_logits = self.lm_head(decoder_hs)
        return lm_logits
