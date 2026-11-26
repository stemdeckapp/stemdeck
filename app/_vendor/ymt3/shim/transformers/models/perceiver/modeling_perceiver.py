# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# PerceiverSelfOutput is a rewrite of the class in
# transformers/models/perceiver/modeling_perceiver.py (its Linear only).
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

import torch
from torch import nn


class PerceiverSelfOutput(nn.Module):
    def __init__(self, config, input_channels, output_channels):
        super().__init__()
        self.dense = nn.Linear(input_channels, output_channels)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.dense(hidden_states)
        return hidden_states