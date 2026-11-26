# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""ff_layer.py

This module contains the implementation of the feedforward layers.

    Supported ff_layer_type:
        'mlp': Multi-Layer Perceptron
        'gmlp': Gated Multi-Layer Perceptron, simplified version of Mixtral Expert with num_experts=1 and top_k=1.
                This is not the spatial gating MLP (https://arxiv.org/abs/2105.08050).
        'moe': Mixtral of Experts, modified from the original source code:
            https://github.com/huggingface/transformers/blob/v4.38.2/src/transformers/models/mixtral/modeling_mixtral.py

    Usage:
        from model.ff_layer import get_ff_layer

        config = PerceiverTFConfig() # or any type of PretrainedConfig()
        config.ff_layer_type = 'moe' # or 'mlp'
        config.moe_num_experts = 4
        config.moe_topk = 2
        config.hidden_act = 'gelu' # or any type of activation function, e.g., 'silu'

        ff_layer = get_ff_layer(config, input_size, widening_factor)

    What ff_layer returns:
        - It returns (hidden_states, router_logits) for MoE and (hidden_states, None) for MLP.
        - router_logits has the shape of (batch_size * sequence_length, n_experts) for MoE.

   
"""
from typing import Any, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.configuration_utils import PretrainedConfig
from transformers.activations import ACT2FN
from model.ops import get_layer_norm
from model.ops import optional_compiler_disable, optional_compiler_dynamic


class MixtralBlockSparseTop2MLP(nn.Module):
    """
    The Gated Multilayer Perceptron (GMLP) used in Mixtral of Experts (MoE).

    """

    def __init__(self, config: PretrainedConfig, input_size: int, widening_factor: int):
        super().__init__()
        self.hidden_dim = input_size
        self.ffn_dim = int(input_size * widening_factor)

        self.w1 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.w2 = nn.Linear(self.ffn_dim, self.hidden_dim, bias=False)
        self.gate = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        current_hidden_states = self.act_fn(self.w1(hidden_states)) * self.gate(hidden_states)
        current_hidden_states = self.w2(current_hidden_states)
        return current_hidden_states


class MixtralSparseMoeBlock(nn.Module):
    """
    This implementation is
    strictly equivalent to standard MoE with full capacity (no
    dropped tokens). It's faster since it formulates MoE operations
    in terms of block-sparse operations to accomodate imbalanced
    assignments of tokens to experts, whereas standard MoE either
    (1) drop tokens at the cost of reduced performance or (2) set
    capacity factor to number of experts and thus waste computation
    and memory on padding.
    """

    def __init__(self, config, input_size: int, widening_factor: int):
        super().__init__()
        self.hidden_dim = input_size
        self.widening_factor = widening_factor
        self.num_experts = config.moe_num_experts
        self.top_k = config.moe_topk

        # gating
        self.gate = nn.Linear(self.hidden_dim, self.num_experts, bias=False)
        self.experts = nn.ModuleList(
            [MixtralBlockSparseTop2MLP(config, self.hidden_dim, self.widening_factor) for _ in range(self.num_experts)])

    @optional_compiler_disable
    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """ """
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.view(-1, hidden_dim)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits = self.gate(hidden_states)

        routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
        routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
        # we cast back to the input dtype
        routing_weights = routing_weights.to(hidden_states.dtype)

        final_hidden_states = torch.zeros((batch_size * sequence_length, hidden_dim),
                                          dtype=hidden_states.dtype,
                                          device=hidden_states.device)

        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = torch.nn.functional.one_hot(selected_experts, num_classes=self.num_experts).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.num_experts):
            expert_layer = self.experts[expert_idx]
            idx, top_x = torch.where(expert_mask[expert_idx])

            if top_x.shape[0] == 0:
                continue

            # in torch it is faster to index using lists than torch tensors
            top_x_list = top_x.tolist()
            idx_list = idx.tolist()

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states[None, top_x_list].reshape(-1, hidden_dim)
            current_hidden_states = expert_layer(current_state) * routing_weights[top_x_list, idx_list, None]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states.to(hidden_states.dtype))
        final_hidden_states = final_hidden_states.reshape(batch_size, sequence_length, hidden_dim)
        return final_hidden_states, router_logits


class MLP(nn.Module):
    """A Standard Transformer-style dense module to follow attention."""

    def __init__(self, config: PretrainedConfig, input_size: int, widening_factor: int):
        super().__init__()
        self.dense1 = nn.Linear(input_size, widening_factor * input_size)
        self.dense2 = nn.Linear(widening_factor * input_size, input_size)

        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states: torch.Tensor) -> Tuple[torch.Tensor, Any]:
        hidden_states = self.dense1(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        hidden_states = self.dense2(hidden_states)
        return hidden_states, None


class SimpleGMLP(nn.Module):
    """A Simple Gated Multilayer Perceptron (aka. 'gmlp'), without the spatial gating mechanism.
    
    Note that this is not the spatial gating MLP (https://arxiv.org/abs/2105.08050).
    - A simplified MLP w/ gating mechanism adapted from Mixtral Expert, as when
    the number of experts and top_k are both set to 1.)
    - Added a dropout layer. 
    - This was also used in T5 v1.1.
    """

    def __init__(self, config: PretrainedConfig, input_size: int, widening_factor: int):
        super().__init__()
        self.hidden_dim = input_size
        self.ffn_dim = int(input_size * widening_factor)

        self.w1 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.w2 = nn.Linear(self.ffn_dim, self.hidden_dim, bias=False)
        self.gate = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]
        self.dropout1 = nn.Dropout(config.dropout_rate)
        self.dropout2 = nn.Dropout(config.dropout_rate)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        current_hidden_states = self.act_fn(self.w1(hidden_states)) * self.gate(hidden_states)
        current_hidden_states = self.dropout1(current_hidden_states)
        current_hidden_states = self.w2(current_hidden_states)
        current_hidden_states = self.dropout2(
            current_hidden_states)  # Residual connection is applied outside of this module.
        return current_hidden_states, None


def get_ff_layer(config: PretrainedConfig, input_size: int, widening_factor: int):
    if config.ff_layer_type == 'moe':
        assert hasattr(config, 'moe_num_experts') and hasattr(config, 'moe_topk') and hasattr(config, 'hidden_act')
        return MixtralSparseMoeBlock(config, input_size, widening_factor)
    elif config.ff_layer_type == 'mlp':
        assert hasattr(config, 'hidden_act')
        return MLP(config, input_size, widening_factor)
    elif config.ff_layer_type == 'gmlp':
        assert hasattr(config, 'hidden_act')
        return SimpleGMLP(config, input_size, widening_factor)
    else:
        raise ValueError(
            f"Unsupported ff_layer_type: {config.ff_layer_type}. Supported types are 'moe', 'mlp' and 'gmlp'.")


def test_get_ff_layer():
    from model.ff_layer import get_ff_layer
    from model.perceiver_helper import PerceiverTFConfig
    input_size = 32
    widening_factor = 1

    # Test for MoE
    config = PerceiverTFConfig()  # or any type of PretrainedConfig()
    config.ff_layer_type = 'moe'
    config.moe_num_experts = 4
    config.moe_topk = 2
    config.hidden_act = 'silu'

    ff_layer = get_ff_layer(config, input_size, widening_factor)
    x = torch.rand(2, 8, input_size)
    hidden_states, router_logits = ff_layer(x)
    print(hidden_states.shape, router_logits.shape)  # (2, 8, 32), (2*8, 4)

    # Test for MLP
    config.ff_layer_type = 'mlp'
    config.hidden_act = 'gelu'

    ff_layer = get_ff_layer(config, input_size, widening_factor)
    hidden_states, _ = ff_layer(x)
    print(hidden_states.shape)  # (2, 8, 32)

    # Test for (simple)gMLP
    config.ff_layer_type = 'gmlp'
    config.hidden_act = 'silu'
    ff_layer = get_ff_layer(config, input_size, widening_factor)
    hidden_states, _ = ff_layer(x)
    print(hidden_states.shape)  # (2, 8, 32)
