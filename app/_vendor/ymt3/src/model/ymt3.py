# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""ymt3.py"""
from typing import Union, Optional, Tuple, Dict, List, Any

import torch
import torch.nn as nn
import pytorch_lightning as pl

from transformers import T5Config
from model.t5mod import T5EncoderYMT3, T5DecoderYMT3, MultiChannelT5Decoder
from model.t5mod_helper import task_cond_dec_generate
from model.perceiver_mod import PerceiverTFEncoder
from model.perceiver_helper import PerceiverTFConfig
from model.lm_head import LMHead
from model.spectrogram import get_spectrogram_layer_from_audio_cfg
from model.conv_block import PreEncoderBlockRes3B
from model.conv_block import PreEncoderBlockHFTT, PreEncoderBlockRes3BHFTT  # added for hFTT-like pre-encoder
from model.projection_layer import get_projection_layer, get_multi_channel_projection_layer

from utils.utils import create_inverse_vocab
from utils.task_manager import TaskManager

from config.config import audio_cfg as default_audio_cfg
from config.config import model_cfg as default_model_cfg
from config.config import shared_cfg as default_shared_cfg
from config.config import T5_BASE_CFG


class YourMT3(pl.LightningModule):
    """YourMT3:
    
    Lightning wrapper for multi-task music transcription Transformer.
    
    """

    def __init__(
            self,
            audio_cfg: Optional[Dict] = None,
            model_cfg: Optional[Dict] = None,
            shared_cfg: Optional[Dict] = None,
            pretrained: bool = False,
            optimizer_name: str = 'adamwscale',
            scheduler_name: str = 'cosine',
            base_lr: float = None,  # None: 'auto' for AdaFactor, 1e-3 for constant, 1e-2 for cosine
            max_steps: Optional[int] = None,
            weight_decay: float = 0.0,
            init_factor: Optional[Union[str, float]] = None,
            task_manager: TaskManager = TaskManager(),
            eval_subtask_key: Optional[str] = "default",
            eval_vocab: Optional[Dict] = None,
            eval_drum_vocab: Optional[Dict] = None,
            write_output_dir: Optional[str] = None,
            write_output_vocab: Optional[Dict] = None,
            onset_tolerance: float = 0.05,
            add_pitch_class_metric: Optional[List[str]] = None,
            add_melody_metric_to_singing: bool = True,
            test_optimal_octave_shift: bool = False,
            test_pitch_shift_layer: Optional[str] = None,
            **kwargs: Any) -> None:
        super().__init__()
        if pretrained is True:
            raise NotImplementedError("Pretrained model is not supported in this version.")
        self.test_pitch_shift_layer = test_pitch_shift_layer  # debug only

        # Config
        if model_cfg is None:
            model_cfg = default_model_cfg  # default config, not overwritten by args of trainer
        if audio_cfg is None:
            audio_cfg = default_audio_cfg  # default config, not overwritten by args of trainer
        if shared_cfg is None:
            shared_cfg = default_shared_cfg  # default config, not overwritten by args of trainer

        # Spec Layer (need to define here to infer max token length)
        self.spectrogram, spec_output_shape = get_spectrogram_layer_from_audio_cfg(
            audio_cfg)  # can be spec or melspec; output_shape is (T, F)
        model_cfg["feat_length"] = spec_output_shape[0]  # T of (T, F)

        # Task manger and Tokens
        self.task_manager = task_manager
        self.max_total_token_length = self.task_manager.max_total_token_length

        # Task Conditioning
        self.use_task_cond_encoder = bool(model_cfg["use_task_conditional_encoder"])
        self.use_task_cond_decoder = bool(model_cfg["use_task_conditional_decoder"])

        # Select Encoder type, Model-specific Config
        assert model_cfg["encoder_type"] in ["t5", "perceiver-tf"]
        assert model_cfg["decoder_type"] in ["t5", "multi-t5"]
        self.encoder_type = model_cfg["encoder_type"]  # {"t5", "perceiver-tf", "conformer"}
        self.decoder_type = model_cfg["decoder_type"]  # {"t5", "multi-t5"}
        encoder_config = model_cfg["encoder"][self.encoder_type]  # mutable
        decoder_config = model_cfg["decoder"][self.decoder_type]  # mutable

        # Positional Encoding
        if isinstance(model_cfg["num_max_positions"], str) and model_cfg["num_max_positions"] == 'auto':
            encoder_config["num_max_positions"] = int(model_cfg["feat_length"] +
                                                      self.task_manager.max_task_token_length + 10)
            decoder_config["num_max_positions"] = int(self.max_total_token_length + 10)
        else:
            assert isinstance(model_cfg["num_max_positions"], int)
            encoder_config["num_max_positions"] = model_cfg["num_max_positions"]
            decoder_config["num_max_positions"] = model_cfg["num_max_positions"]

        # Select Pre-Encoder and Pre-Decoder type
        if model_cfg["pre_encoder_type"] == "default":
            model_cfg["pre_encoder_type"] = model_cfg["pre_encoder_type_default"].get(model_cfg["encoder_type"], None)
        elif model_cfg["pre_encoder_type"] in [None, "none", "None", "0"]:
            model_cfg["pre_encoder_type"] = None
        if model_cfg["pre_decoder_type"] == "default":
            model_cfg["pre_decoder_type"] = model_cfg["pre_decoder_type_default"].get(model_cfg["encoder_type"]).get(
                model_cfg["decoder_type"], None)
        elif model_cfg["pre_decoder_type"] in [None, "none", "None", "0"]:
            model_cfg["pre_decoder_type"] = None
        self.pre_encoder_type = model_cfg["pre_encoder_type"]
        self.pre_decoder_type = model_cfg["pre_decoder_type"]

        # Pre-encoder
        self.pre_encoder = nn.Sequential()
        if self.pre_encoder_type in ["conv", "conv1d_t", "conv1d_f"]:
            kernel_size = (3, 3)
            avp_kernel_size = (1, 2)
            if self.pre_encoder_type == "conv1d_t":
                kernel_size = (3, 1)
            elif self.pre_encoder_type == "conv1d_f":
                kernel_size = (1, 3)
            self.pre_encoder.append(
                PreEncoderBlockRes3B(1,
                                     model_cfg["conv_out_channels"],
                                     kernel_size=kernel_size,
                                     avp_kernerl_size=avp_kernel_size,
                                     activation="relu"))
            pre_enc_output_shape = (spec_output_shape[0], spec_output_shape[1] // 2**3, model_cfg["conv_out_channels"]
                                   )  # (T, F, C) excluding batch dim
        elif self.pre_encoder_type == "hftt":
            self.pre_encoder.append(PreEncoderBlockHFTT())
            pre_enc_output_shape = (spec_output_shape[0], spec_output_shape[1], 128)  # (T, F, C) excluding batch dim
        elif self.pre_encoder_type == "res3b_hftt":
            self.pre_encoder.append(PreEncoderBlockRes3BHFTT())
            pre_enc_output_shape = (spec_output_shape[0], spec_output_shape[1] // 2**3, 128)
        else:
            pre_enc_output_shape = spec_output_shape  # (T, F) excluding batch dim

        # Auto-infer `d_feat` and `d_model`, `vocab_size`, and `num_max_positions`
        if isinstance(model_cfg["d_feat"], str) and model_cfg["d_feat"] == 'auto':
            if self.encoder_type == "perceiver-tf" and encoder_config["attention_to_channel"] is True:
                model_cfg["d_feat"] = pre_enc_output_shape[-2]  # TODO: better readablity
            else:
                model_cfg["d_feat"] = pre_enc_output_shape[-1]  # C of (T, F, C) or F or (T, F)

        if self.encoder_type == "perceiver-tf" and isinstance(encoder_config["d_model"], str):
            if encoder_config["d_model"] == 'q':
                encoder_config["d_model"] = encoder_config["d_latent"]
            elif encoder_config["d_model"] == 'kv':
                encoder_config["d_model"] = model_cfg["d_feat"]
            else:
                raise ValueError(f"Unknown d_model: {encoder_config['d_model']}")

        # # required for PerceiverTF with attention_to_channel option
        # if self.encoder_type == "perceiver-tf":
        #     if encoder_config["attention_to_channel"] is True:
        #         encoder_config["kv_dim"] = model_cfg["d_feat"]  # TODO: better readablity
        #     else:
        #         encoder_config["kv_dim"] = model_cfg["conv_out_channels"]

        if isinstance(model_cfg["vocab_size"], str) and model_cfg["vocab_size"] == 'auto':
            model_cfg["vocab_size"] = task_manager.num_tokens

        if isinstance(model_cfg["num_max_positions"], str) and model_cfg["num_max_positions"] == 'auto':
            model_cfg["num_max_positions"] = int(
                max(model_cfg["feat_length"], model_cfg["event_length"]) + self.task_manager.max_task_token_length + 10)

        # Pre-decoder
        self.pre_decoder = nn.Sequential()
        if self.encoder_type == "perceiver-tf" and self.decoder_type == "t5":
            t, f, c = pre_enc_output_shape  # perceiver-tf: (110, 128, 128) for 2s
            encoder_output_shape = (t, encoder_config["num_latents"], encoder_config["d_latent"])  # (T, K, D_source)
            decoder_input_shape = (t, decoder_config["d_model"])  # (T, D_target)
            proj_layer = get_projection_layer(input_shape=encoder_output_shape,
                                              output_shape=decoder_input_shape,
                                              proj_type=self.pre_decoder_type)
            self.pre_encoder_output_shape = pre_enc_output_shape
            self.encoder_output_shape = encoder_output_shape
            self.decoder_input_shape = decoder_input_shape
            self.pre_decoder.append(proj_layer)
        elif self.encoder_type in ["t5", "conformer"] and self.decoder_type == "t5":
            pass
        elif self.encoder_type == "perceiver-tf" and self.decoder_type == "multi-t5":
            # NOTE: this is experiemental, only for multi-channel decoding with 13 classes
            assert encoder_config["num_latents"] % decoder_config["num_channels"] == 0
            encoder_output_shape = (encoder_config["num_latents"], encoder_config["d_model"])
            decoder_input_shape = (decoder_config["num_channels"], decoder_config["d_model"])
            proj_layer = get_multi_channel_projection_layer(input_shape=encoder_output_shape,
                                                            output_shape=decoder_input_shape,
                                                            proj_type=self.pre_decoder_type)
            self.pre_decoder.append(proj_layer)
        else:
            raise NotImplementedError(
                f"Encoder type {self.encoder_type} and decoder type {self.decoder_type} is not implemented yet.")

        # Positional Encoding, Vocab, etc.
        if self.encoder_type in ["t5", "conformer"]:
            encoder_config["num_max_positions"] = decoder_config["num_max_positions"] = model_cfg["num_max_positions"]
        else:  # perceiver-tf uses separate positional encoding
            encoder_config["num_max_positions"] = model_cfg["feat_length"]
            decoder_config["num_max_positions"] = model_cfg["num_max_positions"]
        encoder_config["vocab_size"] = decoder_config["vocab_size"] = model_cfg["vocab_size"]

        # Print and save updated configs
        self.audio_cfg = audio_cfg
        self.model_cfg = model_cfg
        self.shared_cfg = shared_cfg
        self.save_hyperparameters()
        if self.global_rank == 0:
            print(self.hparams)

        # Encoder and Decoder and LM-head
        self.encoder = None
        self.decoder = None
        self.lm_head = LMHead(decoder_config, 1.0, model_cfg["tie_word_embeddings"])
        self.embed_tokens = nn.Embedding(decoder_config["vocab_size"], decoder_config["d_model"])
        self.embed_tokens.weight.data.normal_(mean=0.0, std=1.0)
        self.shift_right_fn = None
        self.set_encoder_decoder()  # shift_right_fn is also set here

        # Model as ModuleDict
        # self.model = nn.ModuleDict({
        #     "pitchshift": self.pitchshift,   # no grad; created in setup() only for training,
        #                                        and called by training_step()
        #     "spectrogram": self.spectrogram,  # no grad
        #     "pre_encoder": self.pre_encoder,
        #     "encoder": self.encoder,
        #     "pre_decoder": self.pre_decoder,
        #     "decoder": self.decoder,
        #     "embed_tokens": self.embed_tokens,
        #     "lm_head": self.lm_head,
        # })

        # Output MIDI
        if write_output_dir is not None:
            if write_output_vocab is None:
                from config.vocabulary import program_vocab_presets
                self.midi_output_vocab = program_vocab_presets["gm_ext_plus"]
            else:
                self.midi_output_vocab = write_output_vocab
            self.midi_output_inverse_vocab = create_inverse_vocab(self.midi_output_vocab)

    def set_encoder_decoder(self) -> None:
        """Set encoder, decoder, lm_head and emb_tokens from self.model_cfg"""

        # Generate and update T5Config
        t5_basename = self.model_cfg["t5_basename"]
        if t5_basename not in T5_BASE_CFG.keys():
            raise ValueError(f"unknown T5 base config: {t5_basename}")
        # Load from pre-defined config in config.py
        t5_config = T5Config(**T5_BASE_CFG[t5_basename])

        # Create encoder, decoder, lm_head and embed_tokens
        if self.encoder_type == "t5":
            self.encoder = T5EncoderYMT3(self.model_cfg["encoder"]["t5"], t5_config)
        elif self.encoder_type == "perceiver-tf":
            perceivertf_config = PerceiverTFConfig()
            perceivertf_config.update(self.model_cfg["encoder"]["perceiver-tf"])
            self.encoder = PerceiverTFEncoder(perceivertf_config)

        if self.decoder_type == "t5":
            self.decoder = T5DecoderYMT3(self.model_cfg["decoder"]["t5"], t5_config)
        elif self.decoder_type == "multi-t5":
            self.decoder = MultiChannelT5Decoder(self.model_cfg["decoder"]["multi-t5"], t5_config)

        # `shift_right` function for decoding
        self.shift_right_fn = self.decoder._shift_right

    def inference(self,
                  x: torch.FloatTensor,
                  task_tokens: Optional[torch.LongTensor] = None,
                  max_token_length: Optional[int] = None,
                  **kwargs: Any) -> torch.Tensor:
        """ Inference from audio batch by cached autoregressive decoding.
        Args:
            x: (b, 1, t) waveform with t=32767
            task_token: (b, c, task_len) tokenized task. If None, will not append task embeddings (from task_tokens) to input.
            max_length: Maximum length of generated sequence. If None, self.max_total_token_length.
            **kwargs: https://huggingface.co/docs/transformers/v4.27.2/en/main_classes/text_generation#transformers.GenerationMixin.generate
            
        Returns:
            res_tokens: (b, n) resulting tokenized sequence of variable length < max_length
        """
        if self.test_pitch_shift_layer is not None:
            x_ps = self.pitchshift(x, self.test_pitch_shift_semitone)
            x = x_ps

        # From spectrogram to pre-decoder is the same pipeline as in forward()
        x = self.spectrogram(x)  # mel-/spectrogram: (b, 256, 512) or (B, T, F)
        x = self.pre_encoder(x)  # projection to d_model: (B, 256, 512)
        if task_tokens is not None and task_tokens.numel() > 0 and self.use_task_cond_encoder is True:
            # append task embedding to encoder input
            task_embed = self.embed_tokens(task_tokens)  # (B, task_len, 512)
            x = torch.cat([task_embed, x], dim=1)  # (B, task_len + 256, 512)
        enc_hs = self.encoder(inputs_embeds=x)["last_hidden_state"]  # (B, task_len + 256, 512)
        enc_hs = self.pre_decoder(enc_hs)  # (B, task_len + 256, 512)

        # Cached-autoregressive decoding with task token (can be None) as prefix
        if max_token_length is None:
            max_token_length = self.max_total_token_length

        pred_ids = task_cond_dec_generate(decoder=self.decoder,
                                          decoder_type=self.decoder_type,
                                          embed_tokens=self.embed_tokens,
                                          lm_head=self.lm_head,
                                          encoder_hidden_states=enc_hs,
                                          shift_right_fn=self.shift_right_fn,
                                          prefix_ids=task_tokens,
                                          max_length=max_token_length)  # (B, task_len + N) or (B, C, task_len + N)
        if pred_ids.dim() == 2:
            pred_ids = pred_ids.unsqueeze(1)  # (B, 1, task_len + N)

        if self.test_pitch_shift_layer is None:
            return pred_ids
        else:
            return pred_ids, x_ps
