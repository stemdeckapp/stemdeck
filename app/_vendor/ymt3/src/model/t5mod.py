# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
# ==============================================================================
# Copyright 2018 Mesh TensorFlow authors, T5 Authors and HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
from typing import Optional, Tuple, Union, Dict
from einops import rearrange
from model.ops import count_parameters

import torch
from torch import nn
from torch.utils.checkpoint import checkpoint
from transformers.utils import logging
from transformers.utils.model_parallel_utils import assert_device_map, get_device_map
from transformers.models.t5.modeling_t5 import (T5LayerNorm, T5LayerSelfAttention, T5LayerCrossAttention, T5LayerFF)
from transformers.modeling_outputs import (BaseModelOutput, BaseModelOutputWithPastAndCrossAttentions)
from transformers import T5Config, T5PreTrainedModel
from model.positional_encoding import FixedSinusoidalPositionalEmbedding
from model.ff_layer import get_ff_layer

logger = logging.get_logger(__name__)


class T5BlockYMT3(nn.Module):
    """T5 Block, modified to allow using different types of FF layers."""

    def __init__(self, config, has_relative_attention_bias=False):
        super().__init__()
        self.is_decoder = config.is_decoder
        self.layer = nn.ModuleList()
        self.layer.append(T5LayerSelfAttention(config, has_relative_attention_bias=has_relative_attention_bias))
        if self.is_decoder:
            self.layer.append(T5LayerCrossAttention(config))

        # FF layer
        if config.ff_layer_type == 't5_gmlp':
            self.layer.append(T5LayerFF(config))
        elif config.ff_layer_type == 'moe':
            config.moe_num_experts = 8
            config.moe_topk = 2
            config.hidden_act = 'silu'
            moe = get_ff_layer(config, input_size=config.d_model, widening_factor=config.ff_widening_factor)
            self.layer.append(moe)
        else:
            raise ValueError(f"Unknown FF layer type: {config.ff_layer_type}.")
        self.ff_layer_type = config.ff_layer_type

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        position_bias=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        encoder_decoder_position_bias=None,
        layer_head_mask=None,
        cross_attn_layer_head_mask=None,
        past_key_value=None,
        use_cache=False,
        output_attentions=False,
        return_dict=True,
    ):
        if past_key_value is not None:
            if not self.is_decoder:
                logger.warning("`past_key_values` is passed to the encoder. Please make sure this is intended.")
            expected_num_past_key_values = 2 if encoder_hidden_states is None else 4

            if len(past_key_value) != expected_num_past_key_values:
                raise ValueError(
                    f"There should be {expected_num_past_key_values} past states. "
                    f"{'2 (past / key) for cross attention. ' if expected_num_past_key_values == 4 else ''}"
                    f"Got {len(past_key_value)} past key / value states")

            self_attn_past_key_value = past_key_value[:2]
            cross_attn_past_key_value = past_key_value[2:]
        else:
            self_attn_past_key_value, cross_attn_past_key_value = None, None

        self_attention_outputs = self.layer[0](
            hidden_states,
            attention_mask=attention_mask,
            position_bias=position_bias,
            layer_head_mask=layer_head_mask,
            past_key_value=self_attn_past_key_value,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        hidden_states, present_key_value_state = self_attention_outputs[:2]
        attention_outputs = self_attention_outputs[2:]  # Keep self-attention outputs and relative position weights

        # clamp inf values to enable fp16 training
        if hidden_states.dtype == torch.float16:
            clamp_value = torch.where(
                torch.isinf(hidden_states).any(),
                torch.finfo(hidden_states.dtype).max - 1000,
                torch.finfo(hidden_states.dtype).max,
            )
            hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

        do_cross_attention = self.is_decoder and encoder_hidden_states is not None
        if do_cross_attention:
            # the actual query length is unknown for cross attention
            # if using past key value states. Need to inject it here
            if present_key_value_state is not None:
                query_length = present_key_value_state[0].shape[2]
            else:
                query_length = None

            cross_attention_outputs = self.layer[1](
                hidden_states,
                key_value_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                position_bias=encoder_decoder_position_bias,
                layer_head_mask=cross_attn_layer_head_mask,
                past_key_value=cross_attn_past_key_value,
                query_length=query_length,
                use_cache=use_cache,
                output_attentions=output_attentions,
            )
            hidden_states = cross_attention_outputs[0]

            # clamp inf values to enable fp16 training
            if hidden_states.dtype == torch.float16:
                clamp_value = torch.where(
                    torch.isinf(hidden_states).any(),
                    torch.finfo(hidden_states.dtype).max - 1000,
                    torch.finfo(hidden_states.dtype).max,
                )
                hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

            # Combine self attn and cross attn key value states
            if present_key_value_state is not None:
                present_key_value_state = present_key_value_state + cross_attention_outputs[1]

            # Keep cross-attention outputs and relative position weights
            attention_outputs = attention_outputs + cross_attention_outputs[2:]

        # Apply Feed Forward layer - Modified for MoE
        if self.ff_layer_type == 't5_gmlp':
            hidden_states = self.layer[-1](hidden_states)
        elif self.ff_layer_type == 'moe':
            hidden_states = hidden_states + self.layer[-1](hidden_states)[0]  # residual connection outside the MoE
        else:
            raise ValueError(f"Unknown FF layer type: {self.ff_layer_type}.")

        # clamp inf values to enable fp16 training
        if hidden_states.dtype == torch.float16:
            clamp_value = torch.where(
                torch.isinf(hidden_states).any(),
                torch.finfo(hidden_states.dtype).max - 1000,
                torch.finfo(hidden_states.dtype).max,
            )
            hidden_states = torch.clamp(hidden_states, min=-clamp_value, max=clamp_value)

        outputs = (hidden_states,)

        if use_cache:
            outputs = outputs + (present_key_value_state,) + attention_outputs
        else:
            outputs = outputs + attention_outputs

        return outputs  # hidden-states, present_key_value_states, (self-attention position bias), (self-attention weights), (cross-attention position bias), (cross-attention weights)


class T5StackYMT3(T5PreTrainedModel):
    """
    T5Stack, modified for YMT3 with:
    - absolute sinusoidal absolute positional encoding
    """

    def __init__(
        self,
        config,
    ):
        super().__init__(config)
        self.is_decoder = config.is_decoder

        # Positional encoding (modified)
        self.use_t5_trainable_pe = False
        self.additive_pe = None

        pos_enc_type = getattr(config, 'position_encoding_type', 'sinusoidal')
        if pos_enc_type in ['sinusoidal']:
            self.additive_pe = FixedSinusoidalPositionalEmbedding(config.num_max_positions,
                                                                  embedding_dim=config.d_model)
            self.block = nn.ModuleList(
                [T5BlockYMT3(config, has_relative_attention_bias=False) for i in range(config.num_layers)])
        elif pos_enc_type == 'trainable':
            self.use_t5_trainable_pe = True
            # Stack blocks
            self.block = nn.ModuleList(
                [T5BlockYMT3(config, has_relative_attention_bias=bool(i == 0)) for i in range(config.num_layers)])

        self.final_layer_norm = T5LayerNorm(config.d_model, eps=config.layer_norm_epsilon)
        self.dropout = nn.Dropout(config.dropout_rate)

        # Initialize weights and apply final processing
        self.post_init()
        # Model parallel
        self.gradient_checkpointing = False

    def forward(
        self,
        # input_ids=None,
        inputs_embeds=None,
        attention_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        head_mask=None,
        cross_attn_head_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (output_hidden_states
                                if output_hidden_states is not None else self.config.output_hidden_states)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            err_msg_prefix = "decoder_" if self.is_decoder else ""
            raise ValueError(f"You have to specify {err_msg_prefix}inputs_embeds")

        batch_size, seq_length = input_shape

        # required mask seq length can be calculated via length of past
        mask_seq_length = past_key_values[0][0].shape[2] + seq_length if past_key_values is not None else seq_length

        # mod: required for additive PE
        past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0

        if use_cache is True:
            assert self.is_decoder, f"`use_cache` can only be set to `True` if {self} is used as a decoder"

        if attention_mask is None:
            attention_mask = torch.ones(batch_size, mask_seq_length, device=inputs_embeds.device)
        if self.is_decoder and encoder_attention_mask is None and encoder_hidden_states is not None:
            encoder_seq_length = encoder_hidden_states.shape[1]
            encoder_attention_mask = torch.ones(batch_size,
                                                encoder_seq_length,
                                                device=inputs_embeds.device,
                                                dtype=torch.long)

        # initialize past_key_values with `None` if past does not exist
        if past_key_values is None:
            past_key_values = [None] * len(self.block)

        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape)

        # If a 2D or 3D attention mask is provided for the cross-attention
        # we need to make broadcastable to [batch_size, num_heads, seq_length, seq_length]
        if self.is_decoder and encoder_hidden_states is not None:
            encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()
            encoder_hidden_shape = (encoder_batch_size, encoder_sequence_length)
            if encoder_attention_mask is None:
                encoder_attention_mask = torch.ones(encoder_hidden_shape, device=inputs_embeds.device)
            encoder_extended_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_extended_attention_mask = None

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`...")
                use_cache = False

        # Prepare head mask if needed
        head_mask = self.get_head_mask(head_mask, self.config.num_layers)
        cross_attn_head_mask = self.get_head_mask(cross_attn_head_mask, self.config.num_layers)
        present_key_value_states = () if use_cache else None
        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        all_cross_attentions = () if (output_attentions and self.is_decoder) else None
        position_bias = None
        encoder_decoder_position_bias = None

        # mod: additive absolute PE (sinusoidal)
        if self.additive_pe is not None:
            inputs_embeds = inputs_embeds + self.additive_pe(inputs_embeds.shape[1], past_key_values_length)
        else:
            pass  # trinable PE is implemented in T5Block

        hidden_states = self.dropout(inputs_embeds)

        for i, (layer_module, past_key_value) in enumerate(zip(self.block, past_key_values)):
            layer_head_mask = head_mask[i]
            cross_attn_layer_head_mask = cross_attn_head_mask[i]

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):

                    def custom_forward(*inputs):
                        return tuple(module(*inputs, use_cache, output_attentions))

                    return custom_forward

                layer_outputs = checkpoint(
                    create_custom_forward(layer_module),
                    hidden_states,
                    extended_attention_mask,
                    position_bias,
                    encoder_hidden_states,
                    encoder_extended_attention_mask,
                    encoder_decoder_position_bias,
                    layer_head_mask,
                    cross_attn_layer_head_mask,
                    None,  # past_key_value is always None with gradient checkpointing
                )
            else:
                layer_outputs = layer_module(
                    hidden_states,
                    attention_mask=extended_attention_mask,
                    position_bias=position_bias,
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_extended_attention_mask,
                    encoder_decoder_position_bias=encoder_decoder_position_bias,
                    layer_head_mask=layer_head_mask,
                    cross_attn_layer_head_mask=cross_attn_layer_head_mask,
                    past_key_value=past_key_value,
                    use_cache=use_cache,
                    output_attentions=output_attentions,
                )

            # layer_outputs is a tuple with:
            # hidden-states, key-value-states, (self-attention position bias), (self-attention weights), (cross-attention position bias), (cross-attention weights)
            if use_cache is False:
                layer_outputs = layer_outputs[:1] + (None,) + layer_outputs[1:]

            hidden_states, present_key_value_state = layer_outputs[:2]

            # We share the position biases between the layers - the first layer store them
            # layer_outputs = hidden-states, key-value-states (self-attention position bias), (self-attention weights),
            # (cross-attention position bias), (cross-attention weights)
            position_bias = layer_outputs[2]
            if self.is_decoder and encoder_hidden_states is not None:
                encoder_decoder_position_bias = layer_outputs[4 if output_attentions else 3]
            # append next layer key value states
            if use_cache:
                present_key_value_states = present_key_value_states + (present_key_value_state,)

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[3],)
                if self.is_decoder:
                    all_cross_attentions = all_cross_attentions + (layer_outputs[5],)

        hidden_states = self.final_layer_norm(hidden_states)
        hidden_states = self.dropout(hidden_states)

        # Add last layer
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [
                hidden_states,
                present_key_value_states,
                all_hidden_states,
                all_attentions,
                all_cross_attentions,
            ] if v is not None)
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=present_key_value_states,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
            cross_attentions=all_cross_attentions,
        )


class T5EncoderYMT3(T5PreTrainedModel):
    # _keys_to_ignore_on_load_missing = [r"encoder.embed_tokens.weight"]

    def __init__(self, encoder_config: Optional[Dict] = None, config: Optional[T5Config] = None):
        if config is None:
            config = T5Config()
        if encoder_config is not None:
            config = copy.deepcopy(config)
            config.update(encoder_config)

        if hasattr(config, "ff_widening_factor"):
            config.d_ff = int(config.d_model) * int(config.ff_widening_factor)

        config.is_decoder = False
        config.use_cache = False
        config.is_encoder_decoder = False

        super().__init__(config)
        self.model_dim = config.d_model

        self.encoder = T5StackYMT3(config)

        # Initialize weights and apply final processing
        self.post_init()

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
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[torch.FloatTensor], BaseModelOutput]:
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # Encode
        encoder_outputs = self.encoder(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if not return_dict:
            return encoder_outputs
        else:
            return BaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )


class T5DecoderYMT3(T5PreTrainedModel):

    def __init__(self, decoder_config: Optional[Dict] = None, config: Optional[T5Config] = None):
        if config is None:
            config = T5Config()
        if decoder_config is not None:
            config = copy.deepcopy(config)
            config.update(decoder_config)

        if hasattr(config, "ff_widening_factor"):
            config.d_ff = int(config.d_model) * int(config.ff_widening_factor)

        config.is_decoder = True
        config.is_encoder_decoder = False

        super().__init__(config)
        self.model_dim = config.d_model

        self.decoder = T5StackYMT3(config)

        # Initialize weights and apply final processing
        self.post_init()

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
        # input_ids: torch.LongTensor, # removed since embed_tokens is outside the decoder
        inputs_embeds: Optional[torch.FloatTensor] = None,
        attention_mask: Optional[torch.FloatTensor] = None,  # decoder_attention_mask
        encoder_attention_mask: Optional[torch.FloatTensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        encoder_hidden_states: Optional[torch.FloatTensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[torch.FloatTensor], BaseModelOutputWithPastAndCrossAttentions]:
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if isinstance(encoder_hidden_states, BaseModelOutput):
            encoder_hidden_states = encoder_hidden_states.last_hidden_state

        # Decode
        decoder_outputs = self.decoder(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            head_mask=head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if not return_dict:
            return decoder_outputs
        else:
            return BaseModelOutputWithPastAndCrossAttentions(
                last_hidden_state=decoder_outputs[0],
                past_key_values=decoder_outputs[1],
                hidden_states=decoder_outputs[2] if len(decoder_outputs) > 2 else None,
                attentions=decoder_outputs[3] if len(decoder_outputs) > 3 else None,
                cross_attentions=decoder_outputs[4] if len(decoder_outputs) > 4 else None,
            )


class MultiChannelT5Decoder(T5PreTrainedModel):

    def __init__(self, decoder_config: Optional[Dict] = None, config: Optional[T5Config] = None):
        if config is None:
            config = T5Config()
        if decoder_config is not None:
            config = copy.deepcopy(config)
            config.update(decoder_config)

        if hasattr(config, "ff_widening_factor"):
            config.d_ff = int(config.d_model) * int(config.ff_widening_factor)

        config.is_decoder = True
        config.is_encoder_decoder = False

        super().__init__(config)
        self.model_dim = config.d_model
        self.decoder = T5StackYMT3(config)

        # Multi-channel parameters
        self.num_channels = config.num_channels

        # Initialize weights and apply final processing
        self.post_init()

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
        # input_ids: torch.LongTensor, # removed since embed_tokens is outside the decoder
        inputs_embeds: Optional[torch.FloatTensor] = None,
        attention_mask: Optional[torch.FloatTensor] = None,  # decoder_attention_mask
        encoder_attention_mask: Optional[torch.FloatTensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        encoder_hidden_states: Optional[torch.FloatTensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[torch.FloatTensor], BaseModelOutputWithPastAndCrossAttentions]:
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        """
        Args:
            inputs_embeds: torch.FloatTensor (B, K, T, D), where K is the number of channels
            encoder_hidden_states: torch.FloatTensor (B, K, T, D), where K is the number of channels
        
        Returns:
            decoder_outputs: BaseModelOutputWithPastAndCrossAttentions
                last_hidden_state: torch.FloatTensor (B, K, T, D), where K is the number of channels
                past_key_values: Tuple[Tuple[torch.Tensor]]
                hidden_states: Tuple[torch.FloatTensor]
                attentions: Tuple[torch.FloatTensor]
                cross_attentions: Tuple[torch.FloatTensor]

        """
        if isinstance(encoder_hidden_states, BaseModelOutput):
            encoder_hidden_states = encoder_hidden_states.last_hidden_state

        # Reshape input_embeds and encoder_hidden_states
        b, k, t, d = inputs_embeds.size()
        inputs_embeds = rearrange(inputs_embeds, 'b k t d -> (b k) t d')
        encoder_hidden_states = rearrange(encoder_hidden_states, 'b k t d -> (b k) t d')

        # K-channel Decoding
        decoder_outputs = self.decoder(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            head_mask=head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True,
        )

        # Reshape decoder_outputs
        decoder_outputs['last_hidden_state'] = rearrange(decoder_outputs['last_hidden_state'],
                                                         '(b k) t d -> b k t d',
                                                         b=b,
                                                         k=k)

        if not return_dict:
            # Collecting values from decoder_outputs in a specific order
            outputs = (
                decoder_outputs['last_hidden_state'],
                decoder_outputs.get('past_key_values', None),
                decoder_outputs.get('hidden_states', None),
                decoder_outputs.get('attentions', None),
                decoder_outputs.get('cross_attentions', None),
            )
            return tuple(v for v in outputs if v is not None)
        else:
            return decoder_outputs  # ['last_hidden_state']: (B, K, T, D)


def test_multi_channel_t5_decoder():
    # Test multi-channel decoder
    config = T5Config()
    config.num_channels = 4
    config.d_model = 32
    config.num_layers = 2
    config.num_heads = 2
    config.num_max_positions = 64  # for positional encoding

    decoder = MultiChannelT5Decoder(decoder_config=None, config=config)
    decoder.eval()

    input_emb = torch.rand(2, 4, 64, 32)  # (B, K, T, D)
    enc_hs = torch.rand(2, 4, 64, 32)  # (B, K, T, D)
    out = decoder(inputs_embeds=input_emb, encoder_hidden_states=enc_hs, return_dict=True)
    # out['last_hidden_state']: (B, K, T, D)
    # out['past_key_values']: Tuple[Tuple[torch.Tensor]]
