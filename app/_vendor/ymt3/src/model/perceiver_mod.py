# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""perceiver_mod.py

    Implementation of the PerceiverTF encoder with:
    - AliBi positional bias
    - Mixtral of Experts (MoE) feedforward layer

"""
import math
from einops import rearrange
from typing import Optional, Tuple, Union, List, Dict, Literal

import torch
from torch import nn
from transformers.models.perceiver.modeling_perceiver import PerceiverSelfOutput
from transformers.pytorch_utils import (apply_chunking_to_forward, find_pruneable_heads_and_indices, prune_linear_layer)
from model.perceiver_helper import MoEModelOutputWithCrossAttentions
from model.perceiver_helper import PerceiverTFPreTrainedModel, PerceiverTFConfig
from model.positional_encoding import AlibiPositionalBias, get_rotary_emb
from model.ops import get_layer_norm
from model.ff_layer import get_ff_layer


class PerceiverEmbeddings(nn.Module):
    """Construct the latent embeddings sharable with token embeddings in the decoder."""

    def __init__(self, config, shared_emb: Optional[nn.Parameter] = None):
        super().__init__()
        if shared_emb is not None:
            self.latents = shared_emb
            assert self.latents.shape == (config.num_latents, config.d_latents)
        else:
            self.latents = nn.Parameter(torch.randn(config.num_latents, config.d_latents))

    def forward(self, batch_size: int):
        return self.latents.expand(batch_size, -1, -1)


class PerceiverTFTrainablePE(nn.Module):
    """Construct the trainable absolute positional embeddings."""

    def __init__(self, position_encoding_type: Literal['trainable', 'tkd', 'td', 'tk', 'kdt'], max_t: int, k: int,
                 d: int) -> None:
        super().__init__()
        self.position_encoding_type = position_encoding_type
        self.max_t = max_t
        self.k = k
        self.d = d

        if position_encoding_type in ['trainable', 'tkd']:
            self._pos_emb = nn.Parameter(torch.randn(max_t, k, d))
        elif position_encoding_type == 'td':
            self._pos_emb = nn.Parameter(torch.randn(max_t, d))
        elif position_encoding_type == 'tk':
            self._pos_emb = nn.Parameter(torch.randn(max_t, k))
        elif position_encoding_type == 'kdt':
            self._pos_emb = nn.Parameter(torch.randn(k, d))
            self._pos_emb_temporal = nn.Parameter(torch.randn(max_t, d))
        else:
            raise ValueError(f'unknown position encoding type {position_encoding_type}')

    def forward(self):
        pos_emb_temporal = None

        if self.position_encoding_type in ['trainable', 'tkd']:
            pos_emb = self._pos_emb
        elif self.position_encoding_type == 'td':
            pos_emb = self._pos_emb.unsqueeze(1).expand(-1, self.k, -1)
        elif self.position_encoding_type == 'tk':
            pos_emb = self._pos_emb.unsqueeze(-1).expand(-1, -1, self.d)
        elif self.position_encoding_type == 'kdt':
            pos_emb = self._pos_emb.unsqueeze(0).expand(self.max_t, -1, -1)
            pos_emb_temporal = self._pos_emb_temporal

        return pos_emb, pos_emb_temporal


class PerceiverAlibiSelfAttention(nn.Module):
    """
    Multi-headed {cross, self}-attention + Alibi/Rotary positional bias/emb:
    - Can be used both in the encoder as well as in the decoder.
    - Modified from PerceiverSelfAttention in modeling_perceiver.py to support Alibi positional bias
    
    """

    def __init__(
        self,
        config,
        is_cross_attention=False,
        qk_channels=None,
        v_channels=None,
        num_heads=1,
        q_dim=None,
        kv_dim=None,
        rotary_emb=None,
    ):
        super().__init__()
        self.num_heads = num_heads
        # Q and K must have the same number of channels.
        # Default to preserving Q's input's shape.
        if qk_channels is None:
            qk_channels = q_dim
        # V's num_channels determines the shape of the output of QKV-attention.
        # Default to the same number of channels used in the key-query operation.
        if v_channels is None:
            v_channels = qk_channels
        if qk_channels % num_heads != 0:
            raise ValueError(f"qk_channels ({qk_channels}) must be divisible by num_heads ({num_heads}).")
        if v_channels % num_heads != 0:
            raise ValueError(f"v_channels ({v_channels}) must be divisible by num_heads ({num_heads}).")

        self.qk_channels = qk_channels
        self.v_channels = v_channels
        self.qk_channels_per_head = self.qk_channels // num_heads
        self.v_channels_per_head = self.v_channels // num_heads

        # Layer normalization
        self.layernorm1 = get_layer_norm(q_dim, config.layer_norm_type, config.layer_norm_eps)
        if is_cross_attention:
            self.layernorm2 = get_layer_norm(kv_dim, config.layer_norm_type, config.layer_norm_eps)
        else:
            self.layernorm2 = nn.Identity()
        # self.layernorm1 = nn.LayerNorm(q_dim)
        # self.layernorm2 = nn.LayerNorm(kv_dim) if is_cross_attention else nn.Identity()

        # Projection matrices
        self.query = nn.Linear(q_dim, qk_channels)
        self.key = nn.Linear(kv_dim, qk_channels)
        self.value = nn.Linear(kv_dim, v_channels)

        self.dropout = nn.Dropout(config.dropout_rate)

        # (Modified) Alibi positional bias
        if config.position_encoding_type == 'alibi':
            self.alibi_bias = AlibiPositionalBias(heads=num_heads, total_heads=num_heads, trainable_slope=False)
        elif config.position_encoding_type == 'alibit':
            self.alibi_bias = AlibiPositionalBias(heads=num_heads, total_heads=num_heads, trainable_slope=True)
        else:
            self.alibi_bias = None
        # (Modified) RoPE
        if config.position_encoding_type == 'rope':
            assert rotary_emb is not None, "rotary_emb must be provided for RoPE."
            self.rotary_emb = rotary_emb
        else:
            self.rotary_emb = None
        self.rope_apply_to_keys = config.rope_apply_to_keys  # False by default

    def transpose_for_scores(self, x, channels_per_head):
        new_x_shape = x.size()[:-1] + (self.num_heads, channels_per_head)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs: Optional[torch.FloatTensor] = None,
        inputs_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[torch.Tensor]:
        hidden_states = self.layernorm1(hidden_states)
        inputs = self.layernorm2(inputs)

        # Project queries, keys and values to a common feature dimension. If this is instantiated as a cross-attention module,
        # the keys and values come from the inputs; the attention mask needs to be such that the inputs's non-relevant tokens are not attended to.
        is_cross_attention = inputs is not None
        queries = self.query(hidden_states)

        if is_cross_attention:
            keys = self.key(inputs)
            values = self.value(inputs)
            attention_mask = inputs_mask
        else:
            keys = self.key(hidden_states)
            values = self.value(hidden_states)

        # Reshape channels for multi-head attention.
        # We reshape from (batch_size, time, channels) to (batch_size, num_heads, time, channels per head)
        queries = self.transpose_for_scores(queries, self.qk_channels_per_head)
        keys = self.transpose_for_scores(keys, self.qk_channels_per_head)
        values = self.transpose_for_scores(values, self.v_channels_per_head)

        # (Modified) RoPE
        if self.rotary_emb is not None:
            queries = self.rotary_emb.apply_rotary_custom(queries)
            if self.rope_apply_to_keys is True:
                keys = self.rotary_emb.apply_rotary_custom(keys)

        # Take the dot product between the queries and keys to get the raw attention scores.
        attention_scores = torch.matmul(queries, keys.transpose(-1, -2))

        # (Modified) Alibi positional bias
        if self.alibi_bias is not None:
            batch_size, num_heads, q_seq_len, k_seq_len = attention_scores.shape
            attention_scores += self.alibi_bias(q_seq_len,
                                                k_seq_len)  # auto-broadcasting to (b, num_heads, q_seq_len, k_seq_len)

        _, _, _, q_head_dim = queries.shape
        _, _, _, v_head_dim = values.shape
        hiddens = self.num_heads * v_head_dim

        attention_scores = attention_scores / math.sqrt(q_head_dim)

        if attention_mask is not None:
            # Apply the attention mask (precomputed for all layers in PerceiverModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, values)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (hiddens,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        return outputs


class PerceiverAlibiAttention(nn.Module):
    """
    Attention module, including a dense block + Alibi
    : modified from PerceiverAttention in modeling_perceiver.py to support Alibi positional bias
    """

    def __init__(
        self,
        config,
        is_cross_attention=False,
        qk_channels=None,
        v_channels=None,
        num_heads=1,
        q_dim=None,
        kv_dim=None,
        use_query_residual=True,
        rotary_emb=None,
    ):
        super().__init__()
        # MultiHead attention
        if is_cross_attention and qk_channels is None:
            if config.cross_attention_shape_for_attention == "q":
                qk_channels = q_dim
            elif config.cross_attention_shape_for_attention == "kv":
                qk_channels = kv_dim
            else:
                raise ValueError(f"Unknown value {config.cross_attention_shape_for_attention} for "
                                 "cross_attention_shape_for_attention.")
        else:
            if qk_channels is None:
                qk_channels = q_dim
            if v_channels is None:
                v_channels = qk_channels
        self.self = PerceiverAlibiSelfAttention(config,
                                                is_cross_attention=is_cross_attention,
                                                qk_channels=qk_channels,
                                                v_channels=v_channels,
                                                num_heads=num_heads,
                                                q_dim=q_dim,
                                                kv_dim=kv_dim,
                                                rotary_emb=rotary_emb)
        # dense block
        output_channels = None
        if is_cross_attention:
            output_channels = q_dim
        else:
            if output_channels is None:
                output_channels = v_channels
        self.output = PerceiverSelfOutput(config, input_channels=self.self.v_channels, output_channels=output_channels)
        self.use_query_residual = use_query_residual
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(heads, self.self.num_attention_heads,
                                                        self.self.attention_head_size, self.pruned_heads)

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs: Optional[torch.FloatTensor] = None,
        inputs_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[torch.Tensor]:
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            inputs,
            inputs_mask,
            output_attentions,
        )

        # Output projection
        attention_output = self.output(self_outputs[0])

        # Optionally include a residual to the original queries.
        # Consider omitting the residual if the semantics of query and output
        # are different, e.g. if queries are positions and outputs are pixels.
        if self.use_query_residual:
            attention_output = attention_output + hidden_states

        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


class PerceiverAlibiLayer(nn.Module):
    """Construct a single PerceiverTF layer with:
        - Alibi positional bias
        - RoPE
        - Mixtral of Experts (MoE) feedforward layer

    """

    def __init__(
        self,
        config,
        is_cross_attention=False,
        qk_channels=None,
        v_channels=None,
        num_heads=1,
        q_dim=None,
        kv_dim=None,
        widening_factor=1,
        use_query_residual=True,
        rotary_emb=None,
    ):
        super().__init__()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = PerceiverAlibiAttention(config,
                                                 is_cross_attention=is_cross_attention,
                                                 qk_channels=qk_channels,
                                                 v_channels=v_channels,
                                                 num_heads=num_heads,
                                                 q_dim=q_dim,
                                                 kv_dim=kv_dim,
                                                 use_query_residual=use_query_residual,
                                                 rotary_emb=rotary_emb)
        self.layernorm = get_layer_norm(q_dim, config.layer_norm_type, config.layer_norm_eps)
        # self.layernorm = nn.LayerNorm(q_dim)
        self.mlp = get_ff_layer(config, input_size=q_dim, widening_factor=widening_factor)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs: Optional[torch.FloatTensor] = None,
        inputs_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[torch.Tensor]:
        attention_outputs = self.attention(
            hidden_states,
            attention_mask,
            head_mask,
            inputs,
            inputs_mask,
            output_attentions,
        )
        attention_output = attention_outputs[0]

        outputs = attention_outputs[1:]  # add attentions if we output attention weights
        """apply_chunking_to_forward: 
        This function chunks the input_tensors into smaller input tensor parts of size
        chunk_size over the dimension chunk_dim. It then applies a layer forward_fn to 
        each chunk independently to save memory.If the forward_fn is independent across
        the chunk_dim this function will yield the same result as not applying it.
        """
        layer_output, router_logits = apply_chunking_to_forward(self.feed_forward_chunk, self.chunk_size_feed_forward,
                                                                self.seq_len_dim, attention_output)

        layer_output = layer_output + attention_output  # residual connection
        outputs = (layer_output,) + outputs + (router_logits,)  # add router_logits to outputs
        return outputs

    def feed_forward_chunk(self, attention_output):
        layer_output = self.layernorm(attention_output)
        layer_output, router_logits = self.mlp(layer_output)  # router_logits is returned only when using MoE.
        return layer_output, router_logits


class PerceiverTFEncoderBlock(nn.Module):
    """Construct a single block of PerceiverTF encoder:
        - Spectral Cross Attention (SCA)
        - Local latent transformer layers
        - Temporal transformer layers
        - added Alibi positional bias, RoPE, gMLP and MoE feedforward layer
    """

    def __init__(self,
                 config: PerceiverTFConfig,
                 kv_dim: Optional[int] = None,
                 sca_use_query_residual: bool = True,
                 rotary_emb_sca: Optional[nn.Module] = None,
                 rotary_emb_latent: Optional[nn.Module] = None,
                 rotary_emb_temporal: Optional[nn.Module] = None):
        super().__init__()
        self.config = config

        # Check that we can use multihead-attention with these shapes.
        if config.d_latents % config.num_self_attention_heads != 0:
            raise ValueError(f"num_z_channels ({config.d_latents}) must be divisible by"
                             f" num_self_attend_heads ({config.num_self_attention_heads}).")
        if config.d_latents % config.num_cross_attention_heads != 0:
            raise ValueError(f"num_z_channels ({config.d_latents}) must be divisible by"
                             f" num_cross_attend_heads ({config.num_cross_attention_heads}).")

        if kv_dim is None:
            kv_dim = config.kv_dim
        if sca_use_query_residual is None:
            sca_use_query_residual = config.sca_use_query_residual

        # Spectral Cross Attention (SCA) layer.
        self.sca_attention_to_channel = config.attention_to_channel
        self.spectral_cross_attention = PerceiverAlibiAttention(config,
                                                                is_cross_attention=True,
                                                                qk_channels=config.qk_channels,
                                                                v_channels=config.v_channels,
                                                                num_heads=config.num_cross_attention_heads,
                                                                q_dim=config.d_latents,
                                                                kv_dim=kv_dim,
                                                                use_query_residual=sca_use_query_residual,
                                                                rotary_emb=rotary_emb_sca)  # (Modified) RoPE

        # Local latent trasformer layers.
        local_transformer_layers = []
        for _ in range(config.num_local_transformers_per_block):
            layer = PerceiverAlibiLayer(
                config,
                is_cross_attention=False,
                qk_channels=config.qk_channels,  # projection dim for q and k. 
                v_channels=config.v_channels,  # projection dim for v.
                num_heads=config.num_self_attention_heads,
                q_dim=config.d_model,
                kv_dim=config.d_model,
                widening_factor=config.ff_widening_factor,
                use_query_residual=config.use_query_residual,
                rotary_emb=rotary_emb_latent  # (Modified) RoPE
            )
            local_transformer_layers.append(layer)
        self.local_transformer = nn.ModuleList(local_transformer_layers)

        # Temporal transformer layers.
        temporal_transformer_layers = []
        for _ in range(config.num_temporal_transformers_per_block):
            layer = PerceiverAlibiLayer(
                config,
                is_cross_attention=False,
                qk_channels=config.qk_channels,  # projection dim for q and k. 
                v_channels=config.v_channels,  # projection dim for v.
                num_heads=config.num_self_attention_heads,
                q_dim=config.d_model,
                kv_dim=config.d_model,
                widening_factor=config.ff_widening_factor,
                use_query_residual=config.use_query_residual,
                rotary_emb=rotary_emb_temporal  # (Modified) RoPE
            )
            temporal_transformer_layers.append(layer)
        self.temporal_transformer = nn.ModuleList(temporal_transformer_layers)

    def forward(
        self,
        hidden_states: torch.Tensor,
        inputs: Optional[torch.FloatTensor] = None,
        inputs_mask: Optional[torch.FloatTensor] = None,
        local_attention_mask: Optional[torch.FloatTensor] = None,
        temporal_attention_mask: Optional[torch.FloatTensor] = None,
        local_head_mask: Optional[torch.FloatTensor] = None,
        temporal_head_mask: Optional[torch.FloatTensor] = None,
        pos_emb_temporal: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        output_hidden_states: Optional[bool] = False,
        output_router_logits: Optional[bool] = False,  # Only used for MoE.
        return_dict: Optional[bool] = True,
    ) -> Union[Tuple, MoEModelOutputWithCrossAttentions]:
        """
        Inputs:
            hidden_states: (B, T, K, D)
            inputs: (B, T, F, C)
        Returns:
            hidden_states: (B, T, K, D)

        Args:
            hidden_states:
                latent_array (B, T, num_latents, d_latents) for SCA. The latent array 
                with shape (B, K, D) is expanded by t, and positional embeddings are 
                added to it.
            inputs: torch.FloatTensor
                The input sequence of shape (B, T, F, C).
            inputs_mask: torch.FloatTensor
                Only used for SCA. By default, None.
            local_attention_mask:
                Used for local self-attention. By default, None.
            temporal_attention_mask:
                Used for temporal self-attention. By default, None.
            local_head_mask:
                By default, None.
            temporal_head_mask:
                By default, None.
            pos_emb_temporal:
                Optioanl. Used for temporal self-attention. By default, None. (max_t, num_latents, d_latents)
            output_attentions: bool
                Whether to return attentions weights.
            output_hidden_states: bool
                Whether to return all hidden states. If False, only last hidden 
                state is returned.
            output_router_logits: bool
                Whether to return router logits for MoE. If False, only last hidden 
                state is returned.
            return_dict: bool
                Whether to return a MoEModelOutputWithCrossAttentions instead of a tuple.
        """

        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None
        all_cross_attentions = () if output_attentions else None
        all_router_logits = () if output_router_logits else None

        # Collect dimension info
        batch_size, t, num_latents, d_latents = hidden_states.size()  # (B, T, K, D)

        # if self.sca_attention_to_channel:
        #     _, _, _, f = inputs.size()  # (B, T, C, F)
        #     assert d_latents == f, "d_latents must be equal to kv_dim, which is input frequency dim."
        # else:
        #     _, _, _, c = inputs.size()  # (B, T, F, C)
        #     assert d_latents == c, "d_latents must be equal to kv_dim, which is input channels."

        # Reshape (B, T, _, _) to (B*T, _, _) for SCA and local transformer.
        hidden_states = rearrange(hidden_states, "b t k d -> (b t) k d")
        inputs = rearrange(inputs, "b t f c -> (b t) f c")

        # Apply the SCA between the latents (hidden_states) and inputs:
        layer_outputs = self.spectral_cross_attention(
            hidden_states,
            attention_mask=None,  # Input_mask is used instead for cross-attention
            inputs=inputs,
            inputs_mask=inputs_mask,
            output_attentions=output_attentions,
        )
        hidden_states = layer_outputs[0]  # (B*T, K, D)

        if output_attentions:
            all_cross_attentions = all_cross_attentions + (layer_outputs[1],)

        # Apply the block of local latent transformer layers.
        for i, layer_module in enumerate(self.local_transformer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_head_mask = local_head_mask[i] if local_head_mask is not None else None
            layer_outputs = layer_module(
                hidden_states,
                attention_mask=local_attention_mask,
                head_mask=layer_head_mask,
                output_attentions=output_attentions,
            )
            hidden_states = layer_outputs[0]  # (B*T, K, D)
            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_outputs[1],)
            if output_router_logits:
                all_router_logits = all_router_logits + (layer_outputs[2],)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        # Reshape (B*T, K, D) to (B*K, T, D) for the temporal transformer.
        hidden_states = rearrange(hidden_states, "(b t) k d -> (b k) t d", b=batch_size)

        # Apply the block of temporal transformer layers.
        for i, layer_module in enumerate(self.temporal_transformer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_head_mask = temporal_head_mask[i] if temporal_head_mask is not None else None

            if i == 0 and pos_emb_temporal is not None:
                # Add temporal positional embeddings to the hidden_states.
                hidden_states = hidden_states + pos_emb_temporal[:t]  # pos_emb_temporal: (T, D)

            layer_outputs = layer_module(
                hidden_states,
                attention_mask=temporal_attention_mask,
                head_mask=layer_head_mask,
                output_attentions=output_attentions,
            )
            hidden_states = layer_outputs[0]
            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_outputs[1],)
            if output_router_logits:
                all_router_logits = all_router_logits + (layer_outputs[2],)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        last_hideen_state = hidden_states
        # Reshape (B*K, T, D) to (B, T, K, D) for the next block.
        last_hideen_state = rearrange(last_hideen_state, "(b k) t d -> b t k d", b=batch_size)

        # Prepare the outputs.
        if not return_dict:
            return tuple(
                v for v in
                [last_hideen_state, all_hidden_states, all_self_attentions, all_cross_attentions, all_router_logits]
                if v is not None)
        return MoEModelOutputWithCrossAttentions(
            last_hidden_state=last_hideen_state,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
            router_logits=all_router_logits,
        )


class PerceiverTFEncoder(PerceiverTFPreTrainedModel):
    """PerceiverTFEncoder is an encoder model based on the Perceiver and Spectral Cross Attention (SCA).
    
    position_encoding_type: str
        The type of positional encoding to use. One of the following:
        - 'trainable': trainable positional embeddings
        - 'alibi': AlibiNet positional embeddings
        - 'alibit': AlibiNet positional embeddings with trainable slopes for each head
        - 'rope': RoPE (Rotary Positional Encoding)
        (experimental w/ 'trainable')
        - 'tkd': trainable PE (T,K,D) on latent (default for 'trainable')
        - 'td': trainable PE (T,D) on latent
        - 'tk': trainable PE (T,K) on latent
        - 'kdt': trainable PE (K,D) on latent, and (T,) on temporal transformer 
    
    """

    def __init__(self,
                 config: PerceiverTFConfig,
                 sca_use_query_residual: Optional[bool] = None,
                 shared_emb: Optional[nn.Embedding] = None):
        super().__init__(config)
        self.config = config

        if sca_use_query_residual is None:
            self.sca_use_query_residual = config.sca_use_query_residual  # True by default
        self.position_encoding_type = config.position_encoding_type
        self.sca_attention_to_channel = config.attention_to_channel

        # Construct a latent array.
        self.latent_array = PerceiverEmbeddings(config)  # (num_latents, d_latents)

        # Positional embeddings for the latent array.
        if self.position_encoding_type == 'rope':
            # (Modified) RoPE
            self.rotary_emb_sca = get_rotary_emb(config.num_cross_attention_heads, config.rope_type_sca,
                                                 config.rope_partial_pe, config.rope_trainable)
            self.rotary_emb_latent = get_rotary_emb(config.num_cross_attention_heads, config.rope_type_latent,
                                                    config.rope_partial_pe, config.rope_trainable)
            self.rotary_emb_temporal = get_rotary_emb(config.num_cross_attention_heads, config.rope_type_temporal,
                                                      config.rope_partial_pe, config.rope_trainable)
        else:
            self.rotary_emb_sca = None
            self.rotary_emb_latent = None
            self.rotary_emb_temporal = None

        if self.position_encoding_type in ['alibi', 'alibit', 'rope', None]:
            # alibi is imeplemented within PerceiverAlibiSelfAttention, and activated by config.
            # RoPE is implemented without using self.pos_emb.
            self.pos_emb = None
        else:
            k, d = self.latent_array.latents.size()
            max_t = int(config.num_max_positions) + 10  # 10 is headroom for future task tokens...
            self.pos_emb = PerceiverTFTrainablePE(self.position_encoding_type, max_t, k, d)
            """
            self.pos_emb() returns:
                pos_emb: (max_t, K, D)
                pos_emb_temporal: (max_t, K, D)
            """

        # Construct the encoder blocks.
        blocks = []
        for _ in range(config.num_blocks):
            block = PerceiverTFEncoderBlock(
                config,
                kv_dim=config.kv_dim,
                sca_use_query_residual=sca_use_query_residual,
                rotary_emb_sca=self.rotary_emb_sca,  # (Modified) RoPE
                rotary_emb_latent=self.rotary_emb_latent,
                rotary_emb_temporal=self.rotary_emb_temporal)
            blocks.append(block)
        self.blocks = nn.ModuleList(blocks)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.latent_array.latents

    def set_input_embeddings(self, value):
        self.latent_array.latents = value

    """temporary fix for torch.compile issue"""

    def forward(self, **kwargs):
        if self.training is True:
            return self._forward_compile(**kwargs)
        else:
            return self._forward_no_compile(**kwargs)

    def _forward_no_compile(self, **kwargs):
        return self._forward(**kwargs)

    @torch.compile
    def _forward_compile(self, **kwargs):
        return self._forward(**kwargs)

    def _forward(
        self,
        inputs: Optional[torch.FloatTensor] = None,  # (B, T, F, kv_dim)
        inputs_embeds: Optional[torch.FloatTensor] = None,  # (B, T, F, kv_dim) 
        inputs_mask: Optional[torch.FloatTensor] = None,  # (B, F) Mask freq. of inputs in SCA.
        local_attention_mask: Optional[torch.FloatTensor] = None,  # (B, K)
        temporal_attention_mask: Optional[torch.FloatTensor] = None,  # (B, T)
        local_head_mask: Optional[torch.FloatTensor] = None,
        temporal_head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MoEModelOutputWithCrossAttentions]:
        # Inputs and inputs_embeds are tied, and actually the same. (following T5 convention)
        # Inputs are from convoulutional features from audio.
        # Don't be confused with latent embeddings, which is `self.latent_array.latents`, and
        # used as hidden_state of block.
        if inputs is None and inputs_embeds is not None:
            inputs = inputs_embeds
        elif inputs is None and inputs_embeds is None:
            raise ValueError("You must provide 'inputs' or 'inputs_embeds' argument.")

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (output_hidden_states
                                if output_hidden_states is not None else self.config.output_hidden_states)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        batch_size, t, _f, _c = inputs.size()
        device = inputs.device

        # SCA attention to channels of inputs, instead of frequency bins.
        if self.sca_attention_to_channel is True:
            inputs = rearrange(inputs, "b t f c -> b t c f")

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_blocks x num_heads]
        # and head_mask is converted to shape [num_blocks x batch x num_heads x N x N]
        local_head_mask = self.get_head_mask(local_head_mask,
                                             self.config.num_blocks * self.config.num_local_transformers_per_block)
        temporal_head_mask = self.get_head_mask(
            temporal_head_mask, self.config.num_blocks * self.config.num_temporal_transformers_per_block)

        # Prepare attention mask: not implemented

        # Expand the latent embeddings by t: (B, K, D) --> (B, T, K, D)
        latent_embeddings = self.latent_array(batch_size=batch_size)  # (B, num_latents, d_latents)
        expanded_latent_embeddings = latent_embeddings.unsqueeze(1).expand(-1, t, -1, -1)

        # Add positional embeddings to the expanded latent embeddings: (B, T, K, D)
        if self.pos_emb is not None:
            pos_emb_latent, pos_emb_temporal = self.pos_emb.forward()
            expanded_latent_embeddings = expanded_latent_embeddings + pos_emb_latent[:t]
            # (max_t, K, D) -> (T, K, D) -> (B, T, K, D) auto-broadcasting
        else:
            pos_emb_temporal = None

        # Lists to store intermediate outputs if required
        all_hidden_states = []
        all_attentions = []
        all_cross_attentions = []
        all_router_logits = []

        hidden_states = expanded_latent_embeddings

        # Forward-pass
        for i, block in enumerate(self.blocks):
            block_output = block(hidden_states=hidden_states,
                                 inputs=inputs,
                                 inputs_mask=inputs_mask,
                                 local_attention_mask=local_attention_mask,
                                 temporal_attention_mask=temporal_attention_mask,
                                 local_head_mask=local_head_mask,
                                 temporal_head_mask=temporal_head_mask,
                                 pos_emb_temporal=pos_emb_temporal if i == 0 else None,
                                 output_attentions=output_attentions,
                                 output_hidden_states=output_hidden_states,
                                 output_router_logits=output_router_logits,
                                 return_dict=True)

            # Update the hidden_states for the next block
            hidden_states = block_output.last_hidden_state

            # Append to lists if required
            if output_hidden_states:
                all_hidden_states.append(hidden_states)
            if output_attentions:
                all_attentions.append(block_output.attentions)
                all_cross_attentions.append(block_output.cross_attentions)
            if output_router_logits:
                all_router_logits.append(block_output.router_logits)
        last_hidden_states = hidden_states

        # Prepare outputs
        if not return_dict:
            # Convert lists to tuples
            return (last_hidden_states, tuple(all_hidden_states) if all_hidden_states else None,
                    tuple(all_attentions) if all_attentions else None,
                    tuple(all_cross_attentions) if all_cross_attentions else None,
                    tuple(all_router_logits) if all_router_logits else None)

        return MoEModelOutputWithCrossAttentions(
            last_hidden_state=last_hidden_states,
            hidden_states=tuple(all_hidden_states) if all_hidden_states else None,
            attentions=tuple(all_attentions) if all_attentions else None,
            cross_attentions=tuple(all_cross_attentions) if all_cross_attentions else None,
            router_logits=tuple(all_router_logits) if all_router_logits else None)


def test():
    # In HuggingFace's Perceiver implementation:
    # `q_dim` is the latent array dimension d_latents of ((B), num_latents, d_latents).
    # `kv_dim`os the actual input dimension D of (B, T, D)
    # `qk_channels`, `v_channels`: are projection dimensions for attention, (B, T, C)
    #                              (B, T, D) --> projection --> (B, T, C)
    # However, PerceiverTF does not require projection:
    # It takes as input a latent tensor (B, num_latents, d_latents) and a conv_feat tensor (T, B, F, C)
    # The `spectral-cross-attention` and `local-self-attention-transformer` takes as input (B*T, F, C),
    # and C=D=d_latents.
    from model.ops import count_parameters

    # Test input
    b = 2  # batch
    t = 10  # time steps (330 for 6s in paper)
    f = 128  # freq of conv_feat
    c = 128  # channels of conv_feat
    k = 24  # num_latents
    d = 128  # d_latents
    conv_feat = torch.randn(b, t, f, c)

    # construct PerceiverTFEncoder
    config = PerceiverTFConfig()
    pe_types = ['alibi', 'alibit', 'trainable', 'tkd', 'td', 'tk', 'kdt', None]
    config.ff_layer_type = 'moe'
    config.moe_num_experts = 4
    config.moe_topk = 2

    for pe_type in pe_types:
        config.position_encoding_type = pe_type  # 'alibi', 'alibit', 'trainable', 'tkd', 'td', 'tk', 'kdt', None
        config.num_latents = k
        config.d_latents = d
        config.kv_dim = c
        config.qk_channels = d
        config.v_channels = d
        encoder = PerceiverTFEncoder(config)
        encoder.eval()
        assert encoder.latent_array.latents.size() == (k, d)
        # forward
        enc_hidden_state = encoder.forward(inputs_embeds=conv_feat).last_hidden_state
        # print(enc_hidden_state.shape)  # [2, 10, 24, 128] = [B, T, K, D]
        n_param = count_parameters(encoder)[1] // 1000
        print(config.position_encoding_type, f'num_param: {n_param}K')
    """
    PE type | num. param.
    None | 1397K
    alibi | 1397K
    alibit (train slope) | 1397K
    tkd | 2442K
    td | 1441K
    tk | 1405K
    kdt | 1444K 

    MLP | 2637K
    MoE (4 experts) | 4411K
    MoE (6 experts) | 5594K
    """
