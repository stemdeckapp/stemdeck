# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# Written for StemDeck: a small rewrite, no code of the original.
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

import copy


class PretrainedConfig:
    model_type = ""

    def __init__(self, **kwargs):
        self.return_dict = kwargs.pop("return_dict", True)
        self.output_hidden_states = kwargs.pop("output_hidden_states", False)
        self.output_attentions = kwargs.pop("output_attentions", False)
        self.torchscript = kwargs.pop("torchscript", False)
        self.pruned_heads = kwargs.pop("pruned_heads", {})
        self.tie_word_embeddings = kwargs.pop("tie_word_embeddings", True)
        self.chunk_size_feed_forward = kwargs.pop("chunk_size_feed_forward", 0)
        self.is_encoder_decoder = kwargs.pop("is_encoder_decoder", False)
        self.is_decoder = kwargs.pop("is_decoder", False)
        self.add_cross_attention = kwargs.pop("add_cross_attention", False)
        self.use_cache = kwargs.pop("use_cache", getattr(self, "use_cache", True))
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def use_return_dict(self):
        return self.return_dict and not self.torchscript

    def update(self, d):
        for k, v in d.items():
            setattr(self, k, v)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)
