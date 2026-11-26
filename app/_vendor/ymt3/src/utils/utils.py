# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
from typing import Dict


def create_inverse_vocab(vocab: Dict) -> Dict:
    inverse_vocab = {}
    for k, vnp in vocab.items():
        for v in vnp:
            inverse_vocab[v] = (vnp[0], k)  # (program, str_instrument_name)
    return inverse_vocab


def create_program2channel_vocab(program_vocab: Dict, drum_program: int = 128, force_assign_13_ch: bool = False):
    """
    Create a direct map for programs to indices, instrument groups, and primary programs.
    
    Args:
        program_vocab (dict): A dictionary of program vocabularies.
        drum_program (int): The program number for drums. Default: 128.
    
    Returns:
        program2channel_vocab (dict): A dictionary of program to indices, instrument groups, and primary programs.
            e.g. {
                0: {'channel': 0, 'instrument_group': 'Piano', 'primary_program': 0},
                1: {'channel': 1, 'instrument_group': 'Chromatic Percussion', 'primary_program': 8},
                ...
                100: {'channel': 11, 'instrument_group': 'Singing Voice', 'primary_program': 100},
                128: {'channel': 12, 'instrument_group': 'Drums', 'primary_program': 128}
                }
            "primary_program" is not used now.
        
        num_channels (int): The number of channels. Typically length of program vocab + 1 (for drums)
    
    """
    num_channels = len(program_vocab) + 1
    program2channel_vocab = {}
    for idx, (instrument_group, programs) in enumerate(program_vocab.items()):
        if idx > num_channels:
            raise ValueError(
                f"📕 The number of channels ({num_channels}) is less than the number of instrument groups ({idx})")
        for program in programs:
            if program in program2channel_vocab:
                raise ValueError(f"📕 program {program} is duplicated in program_vocab")
            else:
                program2channel_vocab[program] = {
                    "channel": int(idx),
                    "instrument_group": str(instrument_group),
                    "primary_program": int(programs[0]),
                }

    # Add drums
    if drum_program in program2channel_vocab.keys():
        raise ValueError(
            f"📕 drum_program {drum_program} is duplicated in program_vocab. program_vocab should not include drum or program 128"
        )
    else:
        program2channel_vocab[drum_program] = {
            "channel": idx + 1,
            "instrument_group": "Drums",
            "primary_program": drum_program,
        }
    return program2channel_vocab, num_channels
