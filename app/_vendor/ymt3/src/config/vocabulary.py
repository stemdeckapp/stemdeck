# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""vocabulary.py 

Vocabulary for instrument classes. Vocabulary can be used as train_vocab
or test_vocab in data_presets.py or train.py arguments.

- When it is used as train_vocab, it maps the instrument classes to the first
  program number of the class. For example, if you use 'GM_INSTR_CLASS' as
  train_vocab, then the program number of 'Piano' is [0,1,2,3,4,5,6,7]. These
  program numbers are trained as program [0] in the model.

  - When it is used as eval_vocab, any program number in the instrument class 
  is considered as correct.

  
MUSICNET_INSTR_CLASS: 3 classes used for MusicNet benchmark
GM_INSTR_CLASS: equivalent to 'MIDI Class' defined by MT3. 
GM_INSTR_CLASS_PLUS: GM_INSTR_CLASS + singing voice
GM_INSTR_FULL: 128 GM instruments, which is extended from 'MT3_FULL'
MT3_FULL: this matches the class names in Table 3 of MT3 paper
ENST_DRUM_NOTES: 20 drum notes used in ENST dataset
GM_DRUM_NOTES: 45 GM drum notes with percussions

Program 128 is reserved for 'drum' internally.
Program 129 is reserved for 'unannotated', internally.
Program 100 is reserved for 'singing voice (melody)' in GM_INSTR_CLASS_PLUS.
Program 101 is reserved for 'singing voice (chorus)' in GM_INSTR_CLASS_PLUS.


"""
# yapf: disable
import numpy as np

PIANO_SOLO_CLASS = {
    "Piano": np.arange(0, 8),
}

GUITAR_SOLO_CLASS = {
    "Guitar": np.arange(24, 32),
}

SINGING_SOLO_CLASS = {
    "Singing Voice": [100, 101],
}

SINGING_CHORUS_SEP_CLASS = {
    "Singing Voice": [100],
    "Singing Voice (chorus)": [101],
}

BASS_SOLO_CLASS = {
    "Bass": np.arange(32, 40),
}

MUSICNET_INSTR_CLASS = {
    "Piano": np.arange(0, 8),
    "Strings": np.arange(40, 52),  # Solo strings + ensemble strings
    "Winds": np.arange(64, 80),  # Reed + Pipe
}

GM_INSTR_CLASS = {
    "Piano": np.arange(0, 8),
    "Chromatic Percussion": np.arange(8, 16),
    "Organ": np.arange(16, 24),
    "Guitar": np.arange(24, 32),
    "Bass": np.arange(32, 40),
    "Strings": np.arange(40, 56),  # Strings + Ensemble
    # "Strings": np.arange(40, 48),
    # "Ensemble": np.arange(48, 56),
    "Brass": np.arange(56, 64),
    "Reed": np.arange(64, 72),
    "Pipe": np.arange(72, 80),
    "Synth Lead": np.arange(80, 88),
    "Synth Pad": np.arange(88, 96),
}

GM_INSTR_CLASS_PLUS = GM_INSTR_CLASS.copy()
GM_INSTR_CLASS_PLUS["Singing Voice"] = [100, 101]

GM_INSTR_EXT_CLASS = { # Best for enjoyable MIDI file generation
    "Acoustic Piano": [0, 1, 3, 6, 7],
    "Electric Piano": [2, 4, 5],
    "Chromatic Percussion": np.arange(8, 16),
    "Organ": np.arange(16, 24),
    "Guitar (clean)": np.arange(24, 28),
    "Guitar (distortion)": [30, 28, 29, 31], # np.arange(28, 32),
    "Bass": [33, 32, 34, 35, 36, 37, 38, 39], # np.arange(32, 40),
    "Strings": [48, 40, 41, 42, 43, 44, 45, 46, 47, 49, 50, 51, 52, 53, 54, 55], # np.arange(40, 56),
    "Brass": np.arange(56, 64),
    "Reed": np.arange(64, 72),
    "Pipe": np.arange(72, 80),
    "Synth Lead": np.arange(80, 88),
    "Synth Pad": np.arange(88, 96),
}
GM_INSTR_EXT_CLASS_PLUS = GM_INSTR_EXT_CLASS.copy()
GM_INSTR_EXT_CLASS_PLUS["Singing Voice"] = [100]
GM_INSTR_EXT_CLASS_PLUS["Singing Voice (chorus)"] = [101]

GM_INSTR_FULL = {
    "Acoustic Grand Piano": [0],
    "Bright Acoustic Piano": [1],
    "Electric Grand Piano": [2],
    "Honky-tonk Piano": [3],
    "Electric Piano 1": [4],
    "Electric Piano 2": [5],
    "Harpsichord": [6],
    "Clavinet": [7],
    "Celesta": [8],
    "Glockenspiel": [9],
    "Music Box": [10],
    "Vibraphone": [11],
    "Marimba": [12],
    "Xylophone": [13],
    "Tubular Bells": [14],
    "Dulcimer": [15],
    "Drawbar Organ": [16],
    "Percussive Organ": [17],
    "Rock Organ": [18],
    "Church Organ": [19],
    "Reed Organ": [20],
    "Accordion": [21],
    "Harmonica": [22],
    "Tango Accordion": [23],
    "Acoustic Guitar (nylon)": [24],
    "Acoustic Guitar (steel)": [25],
    "Electric Guitar (jazz)": [26],
    "Electric Guitar (clean)": [27],
    "Electric Guitar (muted)": [28],
    "Overdriven Guitar": [29],
    "Distortion Guitar": [30],
    "Guitar Harmonics": [31],
    "Acoustic Bass": [32],
    "Electric Bass (finger)": [33],
    "Electric Bass (pick)": [34],
    "Fretless Bass": [35],
    "Slap Bass 1": [36],
    "Slap Bass 2": [37],
    "Synth Bass 1": [38],
    "Synth Bass 2": [39],
    "Violin": [40],
    "Viola": [41],
    "Cello": [42],
    "Contrabass": [43],
    "Tremolo Strings": [44],
    "Pizzicato Strings": [45],
    "Orchestral Harp": [46],
    "Timpani": [47],
    "String Ensemble 1": [48],
    "String Ensemble 2": [49],
    "Synth Strings 1": [50],
    "Synth Strings 2": [51],
    "Choir Aahs": [52],
    "Voice Oohs": [53],
    "Synth Choir": [54],
    "Orchestra Hit": [55],
    "Trumpet": [56],
    "Trombone": [57],
    "Tuba": [58],
    "Muted Trumpet": [59],
    "French Horn": [60],
    "Brass Section": [61],
    "Synth Brass 1": [62],
    "Synth Brass 2": [63],
    "Soprano Sax": [64],
    "Alto Sax": [65],
    "Tenor Sax": [66],
    "Baritone Sax": [67],
    "Oboe": [68],
    "English Horn": [69],
    "Bassoon": [70],
    "Clarinet": [71],
    "Piccolo": [72],
    "Flute": [73],
    "Recorder": [74],
    "Pan Flute": [75],
    "Bottle Blow": [76],
    "Shakuhachi": [77],
    "Whistle": [78],
    "Ocarina": [79],
    "Lead 1 (square)": [80],
    "Lead 2 (sawtooth)": [81],
    "Lead 3 (calliope)": [82],
    "Lead 4 (chiff)": [83],
    "Lead 5 (charang)": [84],
    "Lead 6 (voice)": [85],
    "Lead 7 (fifths)": [86],
    "Lead 8 (bass + lead)": [87],
    "Pad 1 (new age)": [88],
    "Pad 2 (warm)": [89],
    "Pad 3 (polysynth)": [90],
    "Pad 4 (choir)": [91],
    "Pad 5 (bowed)": [92],
    "Pad 6 (metallic)": [93],
    "Pad 7 (halo)": [94],
    "Pad 8 (sweep)": [95],
    # "FX 1 (rain)": [96],
    # "FX 2 (soundtrack)": [97],
    # "FX 3 (crystal)": [98],
    # "FX 4 (atmosphere)": [99],
    # "FX 5 (brightness)": [100],
    # "FX 6 (goblins)": [101],
    # "FX 7 (echoes)": [102],
    # "FX 8 (sci-fi)": [103],
    # "Sitar": [104],
    # "Banjo": [105],
    # "Shamisen": [106],
    # "Koto": [107],
    # "Kalimba": [108],
    # "Bagpipe": [109],
    # "Fiddle": [110],
    # "Shanai": [111],
    # "Tinkle Bell": [112],
    # "Agogo": [113],
    # "Steel Drums": [114],
    # "Woodblock": [115],
    # "Taiko Drum": [116],
    # "Melodic Tom": [117],
    # "Synth Drum": [118],
    # "Reverse Cymbal": [119],
    # "Guitar Fret Noise": [120],
    # "Breath Noise": [121],
    # "Seashore": [122],
    # "Bird Tweet": [123],
    # "Telephone Ring": [124],
    # "Helicopter": [125],
    # "Applause": [126],
    # "Gunshot": [127]
}

MT3_FULL = { # this matches the class names in Table 3 of MT3 paper 
    "Acoustic Piano": [0, 1, 3, 6, 7],
    "Electric Piano": [2, 4, 5],
    "Chromatic Percussion": np.arange(8, 16),
    "Organ": np.arange(16, 24),
    "Acoustic Guitar": np.arange(24, 26),
    "Clean Electric Guitar": np.arange(26, 29),
    "Distorted Electric Guitar": np.arange(29, 32),
    "Acoustic Bass": [32, 35],
    "Electric Bass": [33, 34, 36, 37, 38, 39],
    "Violin": [40],
    "Viola": [41],
    "Cello": [42],
    "Contrabass": [43],
    "Orchestral Harp": [46],
    "Timpani": [47],
    "String Ensemble": [48, 49, 44, 45],
    "Synth Strings": [50, 51],
    "Choir and Voice": [52, 53, 54],
    "Orchestra Hit": [55],
    "Trumpet": [56, 59],
    "Trombone": [57],
    "Tuba": [58],
    "French Horn": [60],
    "Brass Section": [61, 62, 63],
    "Soprano/Alto Sax": [64, 65],
    "Tenor Sax": [66],
    "Baritone Sax": [67],
    "Oboe": [68],
    "English Horn": [69],
    "Bassoon": [70],
    "Clarinet": [71],
    "Pipe": [73, 72, 74, 75, 76, 77, 78, 79],
    "Synth Lead": np.arange(80, 88),
    "Synth Pad": np.arange(88, 96),
}

MT3_FULL_PLUS = MT3_FULL.copy()
MT3_FULL_PLUS["Singing Voice"] = [100]
MT3_FULL_PLUS["Singing Voice (chorus)"] = [101]

ENST_DRUM_NOTES = {
    "bd": [36],  # Kick Drum
    "sd": [38],  # Snare Drum
    "sweep": [0],  # Brush sweep
    "sticks": [1],  # Sticks
    "rs": [2],  # Rim shot
    "cs": [37],  # X-stick
    "chh": [42],  # Closed Hi-Hat
    "ohh": [46],  # Open Hi-Hat
    "cb": [56],  # Cowbell
    "c": [3],  # Other Cymbals
    "lmt": [47],  # Low Mid Tom
    "mt": [48],  # Mid Tom
    "mtr": [58],  # Mid Tom Rim
    "lt": [45],  # Low Tom
    "ltr": [50],  # Low Tom Rim
    "lft": [41],  # Low Floor Tom
    "rc": [51],  # Ride Cymbal
    "ch": [52],  # Chinese Cymbal
    "cr": [49],  # Crash Cymbal
    "spl": [55],  # Splash Cymbal
}

EGMD_DRUM_NOTES = {
    "Kick Drum": [36],  # Listed by order of most common annotation
    "Snare X-stick": [37],  # Snare X-Stick, https://youtu.be/a2KFrrKaoYU?t=80
    "Snare Drum": [38],  # Snare (head) and Electric Snare
    "Closed Hi-Hat": [42, 44, 22],  # 44 is pedal hi-hat
    "Open Hi-Hat": [46, 26],
    "Cowbell": [56],
    "High Floor Tom": [43],
    "Low Floor Tom": [41],  # Lowest Tom
    "Low Tom": [45],
    "Low-Mid Tom": [47],
    "Mid Tom": [48],
    "Low Tom (Rim)": [50],  # TD-17: 47, 50, 58  
    "Mid Tom (Rim)": [58],
    # "Ride Cymbal": [51, 53, 59],
    "Ride": [51],
    "Ride (Bell)": [53],  # https://youtu.be/b94hZoM5s3k?t=323
    "Ride (Edge)": [59],
    "Chinese Cymbal": [52],
    "Crash Cymbal": [49, 57],
    "Splash Cymbal": [55],
}

# Inspired by Roland TD-17 MIDI note map, https://rolandus.zendesk.com/hc/en-us/articles/360005173411-TD-17-Default-Factory-MIDI-Note-Map
GM_DRUM_NOTES = {
    "Kick Drum": [36, 35],  # Listed by order of most common annotation
    "Snare X-stick": [37, 2],  # Snare X-Stick, https://youtu.be/a2KFrrKaoYU?t=80
    "Snare Drum": [38, 40],  # Snare (head) and Electric Snare
    "Closed Hi-Hat": [42, 44, 22],  # 44 is pedal hi-hat
    "Open Hi-Hat": [46, 26],
    "Cowbell": [56],
    "High Floor Tom": [43],
    "Low Floor Tom": [41],  # Lowest Tom
    "Low Tom": [45],
    "Low-Mid Tom": [47],
    "Mid Tom": [48],
    "Low Tom (Rim)": [50],  # TD-17: 47, 50, 58  
    "Mid Tom (Rim)": [58],
    # "Ride Cymbal": [51, 53, 59],
    "Ride": [51],
    "Ride (Bell)": [53],  # https://youtu.be/b94hZoM5s3k?t=323
    "Ride (Edge)": [59],
    "Chinese Cymbal": [52],
    "Crash Cymbal": [49, 57],
    "Splash Cymbal": [55],
}

KICK_SNARE_HIHAT = {
    "Kick Drum": [36, 35],
    "Snare Drum": [38, 40],
    # "Snare Drum + X-Stick": [38, 40, 37, 2],
    # "Snare X-stick": [37, 2],  # Snare X-Stick, https://youtu.be/a2KFrrKaoYU?t=80
    "Hi-Hat": [42, 44, 46, 22, 26],
    # "Ride Cymbal": [51, 53, 59],
    # "Hi-Hat + Ride": [42, 44, 46, 22, 26, 51, 53, 59],
    # "HiHat + all Cymbals": [42, 44, 46, 22, 26, 51, 53, 59, 52, 49, 57, 55],
    # "Kick Drum + Low Tom": [36, 35, 45],
    # "All Cymbal": [51, 53, 59, 52, 49, 57, 55]
    # "all": np.arange(30, 60)
}

drum_vocab_presets = {
    "gm": GM_DRUM_NOTES,
    "egmd": EGMD_DRUM_NOTES,
    "enst": ENST_DRUM_NOTES,
    "ksh": KICK_SNARE_HIHAT,
    "kshr": {
        "Kick Drum": [36, 35],
        "Snare Drum": [38, 40],
        "Hi-Hat": [42, 44, 46, 22, 26, 51, 53, 59],
    }
}

program_vocab_presets = {
    "gm_full": GM_INSTR_FULL,  # 96 classes (except drums)
    "mt3_full": MT3_FULL,  # 34 classes (except drums) as in MT3 paper
    "mt3_midi": GM_INSTR_CLASS,  # 11 classes (except drums) as in MT3 paper
    "mt3_midi_plus": GM_INSTR_CLASS_PLUS,  # 11 classes + singing (except drums)
    "mt3_full_plus": MT3_FULL_PLUS,  # 34 classes (except drums) mt3_full + singing (except drums)
    "gm": GM_INSTR_CLASS,  # 11 classes (except drums)
    "gm_plus": GM_INSTR_CLASS_PLUS,  # 11 classes + singing (except drums)
    "gm_ext_plus": GM_INSTR_EXT_CLASS_PLUS,  # 13 classes + singing + chorus (except drums)
}
