"""config.py"""
import numpy as np
import torch

if torch.cuda.is_available():
    TEST_BSZ = 128
else:
    TEST_BSZ = 16
# yapf: disable
"""
audio_cfg:
- Used by 'ymt3' to create a spectrogram layer.
- Input shape of model is determined by audio_cfg.
- 'train.py' arguments can override these defaults.
"""
audio_cfg = {
    # Overwrittable by args in train.py
    "codec": "melspec",  # {melspec, spec} melspec for MT3, spec for PerceiverTF
    "hop_length": 128,  # {128, 300} 128 for MT3, 300 for PerceiverTF
    # Shared audio parameters
    "audio_backend": "torchaudio",  # {torchaudio, nnAudio}
    "sample_rate": 16000,
    "input_frames": 32767, # number of input frames (~=2.048 s), determining in-/output shape of front layers. 
    "n_fft": 2048,
    "n_mels": 512,  # only for melspec
    "f_min": 50.0,
    "f_max": 8000.0,
} # TODO: currently dataloader is not updated by "input_frames"

"""
model_cfg:
- Encoder type dictates use of T5_CFG or PERCEIVER_TF_CFG.
- 'train.py' arguments can override these defaults.
"""
model_cfg = {
    "encoder_type": "t5",  # {"t5", "perceiver-tf", "conformer"}
    "decoder_type": "t5", # {"t5", "multi-t5"}
    "pre_encoder_type": "default",  # {None, "default", "conv", "conv1d", "conv2d_avpt"} by default, t5:None, perceiver:conv.
    "pre_encoder_type_default": {"t5": None, "perceiver-tf": "conv", "conformer": None},
    "pre_decoder_type": "default", # {None, 'linear', 'conv1', 'mlp', 'group_linear'} see model/projection_layer.py
    "pre_decoder_type_default": { # [enc_type][dec_type]
        "t5": {"t5": None,},
        "perceiver-tf": {"t5": "linear", "multi-t5": "mc_shared_linear"},
        "conformer": {"t5": None,},
    },
    "conv_out_channels": 128, # number of filters for 'conv' pre_encoder. Otherwise ignored.
    "t5_basename": "google/t5-v1_1-small",
    "pretrained": False, # bool, if True, load pretrained weights from t5_basename. Mismatched layers are ignored.
    "use_task_conditional_encoder": True, # True by default, but default task is None. So not activated by default. 
    "use_task_conditional_decoder": True, # True by default, but default task is None. So not activated by default.  
    "d_feat": "auto", # Input audio feature dimension for encoder. Automatically inferred by audio_cfg and existence of pre_encoders.
    "tie_word_embeddings": True, # If True, weights of embed_tokens and lm_head are tied for stabilizing gradients. 
    "vocab_size": "auto", # int or "auto", automatically inferred by task manager.
    "num_max_positions": "auto", # int or "auto". Length of positional encoding. Automatically inferred by "feat_length", "event_length" and task_manager.max_task_token_length.
    # 'vocab_size', 'tie_word_embeddings' and 'num_max_positions' are auto-copied to encoder and decoder configs in the below.
    "encoder": {
        "t5": {
            "d_model": 512, # Hidden size of T5 encoder. 
            "num_heads": 6,
            "num_layers": 8,
            "dropout_rate": 0.05,
            "position_encoding_type": "sinusoidal", # {'sinusoidal', 'trainable'}.
            "ff_widening_factor": 2, # wideening factor for MLP/MoE layers. Default is 2 in T5.
            "ff_layer_type": "t5_gmlp", # {'t5_gmlp', 'moe', 'mlp', 'gmlp'}. 'moe' for mixture of experts, 'mlp' for standard transformer dense layer, 'gmlp' for simple gated MLP.
        },
        "perceiver-tf": {
            "num_latents": 24, # number of latents in Perceiver. 24 in perceiver-tf paper.
            "d_latent": 128, # latent dimension of Perceiver. 128 in perceiver-tf paper.
            "d_model": "q", # int or "q" or "kv". Inner-dim of sca and local/temporal self-att.
                # "q" follows "latent_dim". "kv" follows  "d_feat". Best practice is to inc-/decrease 'd_latent', instead of 'd_model'.
            "num_blocks": 3, # number of Perceiver-TF blocks in encoder. L in the paper.
            "num_local_transformers_per_block": 2, # N in the paper.
            "num_temporal_transformers_per_block": 2,  # M in the paper.
            "sca_use_query_residual": False,
            "dropout_rate": 0.1,
            "position_encoding_type": "trainable", # {'trainable', 'rotary', 'alibi', 'alibit', None, 'tkd','td', 'tk', 'kdt'}. alibit is alibi with trainable slopes.
            "attention_to_channel": True, # Whether to use channel attention in sca.
            "layer_norm_type": "layer_norm", # {'layer_norm', 'rms_norm'}
            "ff_layer_type": "mlp", # {'moe', 'mlp', gmlp}. 'moe' for mixture of experts, 'mlp' for standard transformer dense layer, 'gmlp' for simple gated MLP.
            "ff_widening_factor": 1, # wideening factor for MLP/MoE layers. Default is 1.
            "moe_num_experts": 4, # number of experts in MoE layer. Default is 4. Disabled if ff_layer_type is not 'moe'.
            "moe_topk": 2, # top-k routing in MoE layer. Default is 2. Disabled if ff_layer_type is not 'moe'.
            "hidden_act": 'gelu', # activation function in MLP/MoE layer. Default is 'gelu'. {'gelu', 'silu', 'relu'}
            "rotary_type_sca": "pixel", # {'l'|'lang', 'p'|'pixel'}. Default is 'pixel'.
            "rotary_type_latent": "pixel", # {'l'|'lang', 'p'|'pixel'}. Default is 'pixel'.
            "rotary_type_temporal": "lang", # {'l'|'lang', 'p'|'pixel'}. Default is 'lang'.
            "rotary_apply_to_keys": False, # Whether to apply rotary to keys. Default is False.
            "rotary_partial_pe": False, # Whether to use partial positional encoding. Default is False.
        },
        "conformer": {
            "d_model": 512, # Hidden size of T5 encoder. 
            "intermediate_size": 512, # or 2048. size of the intermediate feed forward layer in each T5Block
            "num_heads": 8,
            "num_layers": 8,
            "dropout_rate": 0.1,
            "layerdrop": 0.1, # see https://arxiv.org/abs/1909.11556
            "position_encoding_type": "rotary", # {'rotary', 'relative'}. 
            "conv_dim": (512, 512, 512, 512, 512, 512, 512),
            "conv_stride": (5, 2, 2, 2, 2, 2, 2),
            "conv_kernel": (10, 3, 3, 3, 3, 3, 3),
            "conv_depthwise_kernel_size": 31,
        },

    },
    "decoder": {
        "t5": {
            "d_model": 512, # Hidden size of T5 encoder. If encoder has lower dim, it is projected to this dim for enc-dec cross att.
            "num_heads": 6,
            "num_layers": 8,
            "dropout_rate": 0.05,
            "position_encoding_type": "sinusoidal", # {'sinusoidal', 'trainable'}.
            "ff_widening_factor": 2, # wideening factor for MLP/MoE layers. Default is 2 in T5.
            "ff_layer_type": "t5_gmlp", # {'t5_gmlp', 'moe', 'mlp', 'gmlp'}. 'moe' for mixture of experts, 'mlp' for standard transformer dense layer, 'gmlp' for simple gated MLP.
        },
        "multi-t5": {
            "d_model": 512, # Hidden size of T5 encoder. Recommended: {256 or 512}
            "num_heads": 6,
            "num_layers": 8,
            "dropout_rate": 0.05,
            "position_encoding_type": "sinusoidal", # {'sinusoidal', 'trainable'}.
            "ff_widening_factor": 2, # wideening factor for MLP/MoE layers. Default is 2 in T5.
            "ff_layer_type": "t5_gmlp", # {'t5_gmlp', 'moe', 'mlp', 'gmlp'}. 'moe' for mixture of experts, 'mlp' for standard transformer dense layer, 'gmlp' for simple gated MLP.
            "num_channels": 13,
        },
    },
    "feat_length": "auto", # Input audio feature length for encoder. Automatically inferred by audio_cfg.
        # mt3: 256 time steps
    "event_length": 1024,  # max length of event tokens excluding task tokens <-- 128 for multi-t5
    "init_factor": 1.0, # initialization factor for embedding layers
}

# yapf: enable
shared_cfg = {
    "PATH": {
        "data_home": "../../data", # path to the data directory. If using relative path, it is relative to /src directory.
    },
    "BSZ": { # global batch size is local_bsz * n_GPUs in DDP mode
        "train_sub": 12, #20, # sub-batch size is per CPU worker
        "train_local": 24, #40, # local batch size is per GPU in DDP mode
        "validation": 64, # validation batch size is per GPU in DDP mode
        "test": TEST_BSZ,
    },
    "AUGMENTATION": {
        "train_random_amp_range": [0.8, 1.1], # min and max amplitude scaling factor
        "train_stem_iaug_prob": 0.7, # probability of stem activation in intra-stem augmentation
        "train_stem_xaug_policy": {
            "max_k": 3,
            "tau": 0.3,
            "alpha": 1.0,
            "max_subunit_stems": 12, # the number of subunit stems to be reduced to this number of stems
            "p_include_singing": None,  # NOT IMPLEMENTED; probability of including singing for cross augmented examples. if None, use base probaility.
            "no_instr_overlap": True,
            "no_drum_overlap": True,
            "uhat_intra_stem_augment": True,
        },
        "train_pitch_shift_range": [-2, 2], # [min, max] in semitones. None or [0, 0] for no pitch shift.
    },
    "DATAIO": { # do not set `shuffle` here. 
        "num_workers": 4, # num_worker is per GPU in DDP mode
        "prefetch_factor": 2, #2,
        "pin_memory": True,
        "persistent_workers": False,
    },
    "CHECKPOINT": {
        "save_top_k": 4, # max top k checkpoints to save
        "monitor": 'validation/macro_onset_f',
        "mode": 'max',
        # "every_n_epochs": 20, # only working when check_val_every_n_epoch is 0
        "save_last": True, # save last model
        "filename": "{epoch}-{step}",
    },
    "TRAINER": { # do not coverwrite args in this section
        "limit_train_batches": 1.0, # How much of training dataset to check (float = fraction, int = num_batches)
        "limit_val_batches": 1.0,
        "limit_test_batches": 1.0,
        "gradient_clip_val": 1.0, # {0 or None} means don't clip.
        "accumulate_grad_batches": 1, #1, # Accumulates grads every k batches. If set to 1, no effect.
        "check_val_every_n_epoch": 1, #5, 1 for very large dataset such as EGMD
        "num_sanity_val_steps": 0,
    },
    "WANDB": {
        # "save_dir": "../logs",
        "save_dir": "amt/logs", # modified for huggingface spaces...
        "cache_dir": "../logs/.wandb_cache",
        "resume": "allow",
        "anonymous": "allow", # {never, allow, must}
        "mode": "online", # {online, offline, disabled}
    },
    "LR_SCHEDULE": {
        # "scheduler_type": "cosine", # {legacy, cosine, constant}
        "warmup_steps": 1000, # only for cosine scheduler, legacy scheduler follows T5's legacy schedule
        "total_steps": 100000, # argparser of train.py can overwrite this
        "final_cosine": 1e-5, # only for cosine scheduler
    },
    "TOKENIZER": {
        "max_shift_steps": "auto", # max number of shift steps in the model. (int) or "auto". If "auto", it is set by audio_cfg["input_frames"] and shift_steps_ms. 206 with default setup.
        "shift_step_ms": 10, # shift step in ms
    },
}

T5_BASE_CFG = {
    "google/t5-v1_1-small": {
        "architectures": ["T5ForConditionalGeneration"],
        "d_ff":
            1024,  # size of the intermediate feed forward layer in each T5Block. Can be overwrten by ff_widening_factor in model_cfg.
        "d_kv": 64,  # d_kv has to be equal to d_model // num_heads.
        # "d_model": 512,  # encoder hiddnen size, defined by model_cfg
        "decoder_start_token_id": 0,
        "dense_act_fn": "gelu_new",
        # "dropout_rate": 0.05,  # can be overwritten by args in ymt3
        "eos_token_id": 1,
        "feed_forward_proj": "gated-gelu",
        "initializer_factor": 1.0,
        "is_encoder_decoder": True,
        "is_gated_act": True,
        "layer_norm_epsilon": 1e-06,
        "model_type": "t5",
        # "num_decoder_layers": 8, # defined by model_cfg
        # "num_heads": 6,  # defined by model_cfg
        # "num_layers": 8,  # defined by model_cfg
        "output_past": True,
        "pad_token_id": 0,
        "relative_attention_num_buckets": 32,
        # "tie_word_embeddings": True,
        "use_cache": True,
        # "vocab_size": 1391 # vocab_size is automatically set by the task manager...
    },
    "google/t5-efficient-small": {
        "architectures": ["T5ForConditionalGeneration"],
        "d_ff": 2048,
        "d_kv": 64,
        "d_model": 512,
        "decoder_start_token_id": 0,
        "dropout_rate": 0.1,
        "eos_token_id": 1,
        "feed_forward_proj": "relu",
        "initializer_factor": 1.0,
        "is_encoder_decoder": True,
        "layer_norm_epsilon": 1e-06,
        "model_type": "t5",
        "num_decoder_layers": 6,
        "num_heads": 8,
        "num_layers": 6,
        "pad_token_id": 0,
        "relative_attention_num_buckets": 32,
        "torch_dtype": "float32",
        "transformers_version": "4.17.0.dev0",
        "use_cache": True,
    },
}

# yapf: enable
DEEPSPEED_CFG = {
    "zero_allow_untested_optimizer": True,
    "optimizer": {
        "type": "adam",
        "params": {
            "lr": 1e-4,
            "betas": [0.998, 0.999],
            "eps": 1e-3,
            "weight_decay": 0.001,
            "adam_w_mode": True,
        }
    },
    "scheduler": {
        "type": "WarmupLR",
        "params": {
            "last_batch_iteration": -1,
            "warmup_min_lr": 0,
            "warmup_max_lr": 3e-5,
            "warmup_num_steps": 100,
        },
    },
    "zero_optimization": {
        "stage": 0,  #0,1,2,3
        # "offload_optimizer":
        #     False,  # Enable Offloading optimizer state/calculation to the host CPU
    },
}
