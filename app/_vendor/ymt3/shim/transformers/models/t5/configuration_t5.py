# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# Written for StemDeck: a small rewrite, no code of the original.
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

from ...configuration_utils import PretrainedConfig


class T5Config(PretrainedConfig):
    model_type = "t5"

    def __init__(self, vocab_size=32128, d_model=512, d_kv=64, d_ff=2048, num_layers=6, num_decoder_layers=None,
                 num_heads=8, relative_attention_num_buckets=32, relative_attention_max_distance=128,
                 dropout_rate=0.1, layer_norm_epsilon=1e-6, initializer_factor=1.0, feed_forward_proj="relu",
                 is_encoder_decoder=True, use_cache=True, pad_token_id=0, eos_token_id=1, classifier_dropout=0.0,
                 **kwargs):
        self.vocab_size = vocab_size
        self.d_model = d_model
        self.d_kv = d_kv
        self.d_ff = d_ff
        self.num_layers = num_layers
        self.num_decoder_layers = num_decoder_layers if num_decoder_layers is not None else num_layers
        self.num_heads = num_heads
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.relative_attention_max_distance = relative_attention_max_distance
        self.dropout_rate = dropout_rate
        self.classifier_dropout = classifier_dropout
        self.layer_norm_epsilon = layer_norm_epsilon
        self.initializer_factor = initializer_factor
        self.feed_forward_proj = feed_forward_proj
        self.use_cache = use_cache
        act_info = self.feed_forward_proj.split("-")
        self.dense_act_fn = act_info[-1]
        self.is_gated_act = act_info[0] == "gated"
        if feed_forward_proj == "gated-gelu":
            self.dense_act_fn = "gelu_new"
        self.pad_token_id = pad_token_id
        self.eos_token_id = eos_token_id
        super().__init__(is_encoder_decoder=is_encoder_decoder, use_cache=use_cache, **kwargs)
