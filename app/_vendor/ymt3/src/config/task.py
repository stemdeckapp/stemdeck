# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""task.py"""
from config.vocabulary import *
from utils.note_event_dataclasses import Event

task_cfg = {
    "mt3_midi": { # 11 classes + drum class
        "name": "mt3_midi",
        "train_program_vocab": program_vocab_presets["mt3_midi"],
        "train_drum_vocab": drum_vocab_presets["gm"],
    },
    "mt3_midi_plus": { # 11 classes + singing + drum class
        "name": "mt3_midi_plus",
        "train_program_vocab": program_vocab_presets["mt3_midi_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
    },
    "mt3_full": { # 34 classes (except drums) as in MT3 paper
        "name": "mt3_full",
        "train_program_vocab": program_vocab_presets["mt3_full"],
        "train_drum_vocab": drum_vocab_presets["gm"],
    },
    "mt3_full_plus": { # 34 classes (except drums) as in MT3 paper + singing + drum class
        "name": "mt3_full_plus",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
    },
    "gm_ext_plus": { # 13 classes + singing + chorus (except drums) 
        "name": "gm_ext_plus",
        "train_program_vocab": program_vocab_presets["gm_ext_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
    },
    "singing_v1": {
        "name": "singing",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "subtask_tokens": ["task", "transcribe_singing", "transcribe_all"],
        "ignore_decoding_tokens": ["task", "transcribe_singing", "transcribe_all"],
        "max_task_token_length": 2,
        "eval_subtask_prefix": {
            "default": [Event("transcribe_all", 0), Event("task", 0)],
            "singing-only": [Event("transcribe_singing", 0),
                             Event("task", 0)],
        }
    },
    "singing_drum_v1": {
        "name": "singing_drum",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "subtask_tokens": ["task", "transcribe_singing", "transcribe_drum", "transcribe_all"],
        "ignore_decoding_tokens": [
            "task", "transcribe_singing", "transcribe_drum", "transcribe_all"
        ],
        "max_task_token_length": 2,
        "eval_subtask_prefix": {
            "default": [Event("transcribe_all", 0), Event("task", 0)],
            "singing-only": [Event("transcribe_singing", 0),
                             Event("task", 0)],
            "drum-only": [Event("transcribe_drum", 0),
                          Event("task", 0)],
        }
    },
    "mc13": { # multi-channel decoding task of {11 classes + drums + singing}
        "name": "mc13",
        "train_program_vocab": program_vocab_presets["gm_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "num_decoding_channels": len(program_vocab_presets["gm_plus"]) + 1, # 13
        "max_note_token_length_per_ch": 512, # multi-channel decoding exclusive parameter
        "mask_loss_strategy": None, # multi-channel decoding exclusive parameter
    },
    "mc13_256": { # multi-channel decoding task of {11 classes + drums + singing}
        "name": "mc13_256",
        "train_program_vocab": program_vocab_presets["gm_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "num_decoding_channels": len(program_vocab_presets["gm_plus"]) + 1, # 13
        "max_note_token_length_per_ch": 256, # multi-channel decoding exclusive parameter
        "mask_loss_strategy": None, # multi-channel decoding exclusive parameter
    },
    "mc13_full_plus": { # multi-channel decoding task of {34 classes + drums + singing & chorus} mapped to 13 channels
        "name": "mc13_full_plus",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "program2channel_vocab_source": program_vocab_presets["gm_plus"],
        "num_decoding_channels": 13,
        "max_note_token_length_per_ch": 512, # multi-channel decoding exclusive parameter
        "mask_loss_strategy": None, # multi-channel decoding exclusive parameter
    },
    "mc13_full_plus_256": { # multi-channel decoding task of {34 classes + drums + singing & chorus} mapped to 13 channels
        "name": "mc13_full_plus_256",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "program2channel_vocab_source": program_vocab_presets["gm_plus"],
        "num_decoding_channels": 13,
        "max_note_token_length_per_ch": 256, # multi-channel decoding exclusive parameter
        "mask_loss_strategy": None, # multi-channel decoding exclusive parameter
    },
    "exc_v1": {
        "name": "exclusive",
        "train_program_vocab": program_vocab_presets["mt3_full_plus"],
        "train_drum_vocab": drum_vocab_presets["gm"],
        "subtask_tokens": ["transcribe", "all", ":"],
        # "ignore_decoding_tokens": [
        #     "task", "transcribe_singing", "transcribe_drum", "transcribe_all"
        # ],
        # "max_task_token_length": 2,
        "ignore_decoding_tokens_from_and_to": ["transcribe", ":"],
        "eval_subtask_prefix": { # this is the main task that transcribe all instruments
            "default": [Event("transcribe", 0), Event("all", 0), Event(":", 0)],
        },
        "shuffle_subtasks": True,
    },
}
