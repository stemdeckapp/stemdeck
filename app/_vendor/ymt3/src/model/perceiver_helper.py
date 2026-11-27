# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
from dataclasses import dataclass
from typing import Optional, Tuple
import torch
from torch import nn
from transformers.utils import ModelOutput
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_utils import PreTrainedModel
# from transformers.models.perceiver.modeling_perceiver import (PerceiverAbstractPositionEncoding,
#                                                               PerceiverTrainablePositionEncoding,
#                                                               PerceiverFourierPositionEncoding)


class PerceiverTFConfig(PretrainedConfig):
    r"""
    This is the configuration class to store the configuration of a [`PerceiverTF`]. It is used to instantiate an
    Perceiver model according to the specified arguments, defining the model architecture. Instantiating a
    configuration with the defaults will yield a similar configuration to that of the Perceiver
    [deepmind/language-perceiver](https://huggingface.co/deepmind/language-perceiver) architecture.

    Configuration objects inherit from [`PretrainedConfig`] and can be used to control the model outputs. Read the
    documentation from [`PretrainedConfig`] for more information.

    Args:
        num_latents (`int`, *optional*, defaults to 256):
            The number of latents.
        d_latents (`int`, *optional*, defaults to 1280):
            Dimension of the latent embeddings.
        d_model (`int`, *optional*, defaults to 768):
            Dimension of the inputs. Should only be provided in case [*PerceiverTextPreprocessor*] is used or no
            preprocessor is provided.
        kv_dim (`int`, *optional*, defaults to 128):
        num_blocks (`int`, *optional*, defaults to 1):
            Number of blocks in the Transformer encoder.
        num_self_attention_heads (`int`, *optional*, defaults to 8):
            Number of attention heads for each self-attention layer in the Transformer encoder.
        num_cross_attention_heads (`int`, *optional*, defaults to 8):
            Number of attention heads for each cross-attention layer in the Transformer encoder.
        num_local_transformers_per_block (`int`, *optional*, defaults to 2):
            Number of local Transformer layers per Transformer block in the Transformer encoder.
        num_temporal_transformers_per_block (`int`, *optional*, defaults to 2):
            Number of temporal Transformer layers per Transformer block in the Transformer encoder.
        shared_parallel_temporal_transformers (`bool`, *optional*, defaults to `False`):
            Whether to share the parameters across the K parallel temporal Transformers in each block.
        qk_channels (`int`, *optional*):
            Dimension to project the queries + keys before applying attention in the cross-attention and self-attention
            layers of the encoder. Will default to preserving the dimension of the queries if not specified.
        v_channels (`int`, *optional*):
            Dimension to project the values before applying attention in the cross-attention and self-attention layers
            of the encoder. Will default to preserving the dimension of the queries if not specified.
        ** DEPRECATED ** cross_attention_shape_for_attention (`str`, *optional*, defaults to `'kv'`):
            Dimension to use when downsampling the queries and keys in the cross-attention layer of the encoder.
        ** DEPRECATED ** self_attention_widening_factor (`int`, *optional*, defaults to 1):
            Dimension of the feed-forward layer in the cross-attention layer of the Transformer encoder.
        cross_attention_widening_factor (`int`, *optional*, defaults to 1):
            Dimension of the feed-forward layer in the self-attention layers of the Transformer encoder.
        hidden_act (`str` or `function`, *optional*, defaults to `"gelu"`):
            The non-linear activation function (function or string) in the encoder and pooler. If string, `"gelu"`,
            `"relu"`, `"selu"` and `"gelu_new"` are supported.
        dropout_rate (`float`, *optional*, defaults to 0.1):
            The dropout ratio for the attention probabilities.
        initializer_range (`float`, *optional*, defaults to 0.02):
            The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
        layer_norm_type (`str`, *optional*, defaults to `'layer_norm'`):
            The type of layer normalization to use. Can be one of {'layer_norm', 'rms_norm'}.
        layer_norm_eps (`float`, *optional*, defaults to 1e-5):
            The epsilon used by the layer normalization layers.
        sca_use_query_residual (`bool`, *optional*, defaults to `True`):
            Whether to add a query residual in the spectral cross attention (SCA) layer of the encoder.
        use_query_residual (`float`, *optional*, defaults to `True`):
            Whether to add a query residual in the cross-attention layer of the encoder.
        position_encoding_type (`str`, *optional*, defaults to `'trainable'`):
            Type of position encoding to use. Can be one of {'trainable', 'alibi', 'alibit', 'rope', None}.
        num_max_positions (`int`, *optional*, defaults to 331):
            Maximum number of positions to use for the position encoding.
        vocab_size (`int`, *optional*, defaults to 262):
            Vocabulary size for the masked language modeling model.
        attention_to_channel (`bool`, defaults to `False`):
            Whether SCA should attend to the channel dimension. If False, attention to frequency bin dimension.
        ff_layer_type (`str`, *optional*, defaults to `'mlp'`):
            Type of feed-forward layer to use. Can be one of {'mlp', 'moe'}.
        ff_widening_factor (`int`, *optional*, defaults to 1):
            Widening factor for the feed-forward layers in the MLP/MoE.
        moe_num_experts (`int`, *optional*, defaults to 4):
            Number of experts to use in the mixture of experts (MoE) feed-forward layer. 
            Only used if `ff_layer_type` is set to `'moe'`.
        moe_topk (`int`, *optional*, defaults to 2):
            Number of top experts to use in the mixture of experts (MoE) feed-forward layer.
            Only used if `ff_layer_type` is set to `'moe'`.
        rope_type_sca (`str`, *optional*, defaults to `pixel`): Can be one of {'l'|lang', 'p'|'pixel', None}. 
            RoPE index type for SCA. Only used if `position_encoding_type` is set to `rope`.
        rope_type_latent (`str`, *optional*, defaults to `pixel`): Can be one of {'l'|'lang', 'p'|'pixel', None}.
            RoPE index type for Latent Transformer. Only used if `position_encoding_type` is set to `'rope'`.
        rope_type_temporal (`str`, *optional*, defaults to `lang`): Can be one of {'l'|'lang', 'p'|'pixel', None}.
            RoPE index type for Temporal Transformer. Only used if `position_encoding_type` is set to `'rope'`.     
        rope_apply_to_keys (`bool`, *optional*, defaults to `False`): Whether to apply RoPE to the keys in the
            self/cross-attention layers. Only used if `position_encoding_type` is set to `'rope'`.
        rope_partial_pe (`bool`, *optional*, defaults to `False`): Whether to use partial RoPE in the self/cross-attention.
            Only used if `position_encoding_type` is set to `'rope'`.
        rope_trainable (`bool`, *optional*, defaults to `False`): Whether to make the RoPE trainable. Only used if
    
    Example:

    ```python
    >>> from model.perceiver_mod import PerceiverTFEncodel, PerceiverTFConfig

    >>> # Initializing a Perceiver deepmind/language-perceiver style configuration
    >>> configuration = PerceiverTFConfig()

    >>> # Initializing a model from the deepmind/language-perceiver style configuration
    >>> model = PerceiverTFEncoder(configuration)

    >>> # Accessing the model configuration
    >>> configuration = model.config
    ```"""
    model_type = "perceivertf"

    def __init__(
        self,
        num_latents=24,
        d_latents=128,
        d_model=128,
        kv_dim=128,
        num_blocks=3,
        num_self_attention_heads=8,
        num_cross_attention_heads=8,
        num_local_transformers_per_block=2,
        num_temporal_transformers_per_block=2,
        qk_channels=128,
        v_channels=128,
        cross_attention_shape_for_attention="q",
        # self_attention_widening_factor=1, ** DEPRECATED **
        # cross_attention_widening_factor=1, ** DEPRECATED **
        hidden_act="gelu",
        dropout_rate=0.1,
        initializer_range=0.02,
        layer_norm_type="layer_norm",
        layer_norm_eps=1e-5,
        sca_use_query_residual=True,
        use_query_residual=True,
        position_encoding_type="trainable",
        num_max_positions=330,
        vocab_size=1391,
        attention_to_channel=False,
        ff_layer_type="mlp",
        ff_widening_factor=1,
        moe_num_experts=4,
        moe_topk=2,
        rope_type_sca="pixel",
        rope_type_latent="pixel",
        rope_type_temporal="lang",
        rope_apply_to_keys=False,
        rope_partial_pe=False,
        rope_trainable=False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.num_latents = num_latents
        self.d_latents = d_latents
        self.d_model = d_model
        self.kv_dim = kv_dim
        self.qk_channels = qk_channels
        self.v_channels = v_channels

        self.num_blocks = num_blocks
        self.num_self_attention_heads = num_self_attention_heads
        self.num_cross_attention_heads = num_cross_attention_heads
        self.num_local_transformers_per_block = num_local_transformers_per_block
        self.num_temporal_transformers_per_block = num_temporal_transformers_per_block
        self.sca_use_query_residual = sca_use_query_residual
        self.use_query_residual = use_query_residual
        self.position_encoding_type = position_encoding_type
        self.num_max_positions = num_max_positions
        # self.self_attention_widening_factor = self_attention_widening_factor
        # self.cross_attention_widening_factor = cross_attention_widening_factor
        self.cross_attention_shape_for_attention = cross_attention_shape_for_attention
        self.attention_to_channel = attention_to_channel
        self.ff_layer_type = ff_layer_type
        self.ff_widening_factor = ff_widening_factor
        self.moe_num_experts = moe_num_experts
        self.moe_topk = moe_topk
        self.rope_type_sca = rope_type_sca
        self.rope_type_latent = rope_type_latent
        self.rope_type_temporal = rope_type_temporal
        self.rope_apply_to_keys = rope_apply_to_keys
        self.rope_partial_pe = rope_partial_pe
        self.rope_trainable = rope_trainable

        self.hidden_act = hidden_act
        self.dropout_rate = dropout_rate
        self.initializer_range = initializer_range
        self.layer_norm_type = layer_norm_type
        self.layer_norm_eps = layer_norm_eps

        # masked language modeling attributes
        self.vocab_size = vocab_size


class PerceiverTFPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = PerceiverTFConfig
    base_model_prefix = "perceivertf"
    main_input_name = "inputs"

    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (nn.Linear, nn.Conv2d)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif hasattr(module, "latents"):
            module.latents.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif hasattr(module, "_pos_emb") and isinstance(module._pos_emb, nn.Parameter):
            # initialize PerceiverTFTrainablePE
            module._pos_emb.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif hasattr(module, "_pos_emb_temporal"):
            # initialize PerceiverTFTrainablePE
            module._pos_emb_temporal.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif hasattr(module, "slopes") and isinstance(module.slopes, nn.Parameter):
            # initialize AlibiPositionalBias
            module.reset_parameters()
        elif isinstance(module, nn.ParameterDict):
            for modality in module.keys():
                module[modality].data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        # elif hasattr(module, "position_embeddings") and isinstance(
        #         module, PerceiverTrainablePositionEncoding):
        #     module.position_embeddings.data.normal_(mean=0.0, std=self.config.initializer_range)


# Replace the 'ModelOutputWithCrossAttentions' with 'MoEModelOutputWithCrossAttentions' for MoE
@dataclass
class MoEModelOutputWithCrossAttentions(ModelOutput):
    """
    Base class for model's outputs, with potential hidden states and attentions.
    Plus, router_probs for Mixture of Experts models.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        cross_attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` and `config.add_cross_attention=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the decoder's cross-attention layer, after the attention softmax, used to compute the
            weighted average in the cross-attention heads.
        router_probs (`tuple(torch.FloatTensor)`, *optional*, returned when `output_router_probs=True` and `config.add_router_probs=True` is passed or when `config.output_router_probs=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, sequence_length, num_experts)`.

            Raw router probabilities that are computed by MoE routers, these terms are used to compute the auxiliary
            loss and the z_loss for Mixture of Experts models.
    """

    last_hidden_state: torch.FloatTensor = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    cross_attentions: Optional[Tuple[torch.FloatTensor]] = None
    router_logits: Optional[Tuple[torch.FloatTensor]] = None
