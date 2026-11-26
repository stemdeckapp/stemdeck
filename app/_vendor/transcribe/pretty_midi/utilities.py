"""Utilty functions for converting between MIDI data and human-readable/usable
values

"""

import numpy as np
import re

from .constants import DRUM_MAP, INSTRUMENT_MAP, INSTRUMENT_CLASSES


def key_number_to_key_name(key_number):
    """Convert a key number to a key string.

    Parameters
    ----------
    key_number : int
        Uses pitch classes to represent major and minor keys.
        For minor keys, adds a 12 offset.
        For example, C major is 0 and C minor is 12.

    Returns
    -------
    key_name : str
        Key name in the format ``'(root) (mode)'``, e.g. ``'Gb minor'``.
        Gives preference for keys with flats, with the exception of F#, G# and
        C# minor.
    """

    if not isinstance(key_number, int):
        raise ValueError('`key_number` is not int!')
    if not ((key_number >= 0) and (key_number < 24)):
        raise ValueError('`key_number` is larger than 24')

    # preference to keys with flats
    keys = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb',
            'G', 'Ab', 'A', 'Bb', 'B']

    # circle around 12 pitch classes
    key_idx = key_number % 12
    mode = key_number // 12

    # check if mode is major or minor
    if mode == 0:
        return keys[key_idx] + ' Major'
    elif mode == 1:
        # preference to C#, F# and G# minor
        if key_idx in [1, 6, 8]:
            return keys[key_idx-1] + '# minor'
        else:
            return keys[key_idx] + ' minor'


def key_name_to_key_number(key_string):
    """Convert a key name string to key number.

    Parameters
    ----------
    key_string : str
        Format is ``'(root) (mode)'``, where:
          * ``(root)`` is one of ABCDEFG or abcdefg.  A lowercase root
            indicates a minor key when no mode string is specified.  Optionally
            a # for sharp or b for flat can be specified.

          * ``(mode)`` is optionally specified either as one of 'M', 'Maj',
            'Major', 'maj', or 'major' for major or 'm', 'Min', 'Minor', 'min',
            'minor' for minor.  If no mode is specified and the root is
            uppercase, the mode is assumed to be major; if the root is
            lowercase, the mode is assumed to be minor.

    Returns
    -------
    key_number : int
        Integer representing the key and its mode.  Integers from 0 to 11
        represent major keys from C to B; 12 to 23 represent minor keys from C
        to B.
    """
    # Create lists of possible mode names (major or minor)
    major_strs = ['M', 'Maj', 'Major', 'maj', 'major']
    minor_strs = ['m', 'Min', 'Minor', 'min', 'minor']
    # Construct regular expression for matching key
    pattern = re.compile(
        # Start with any of A-G, a-g
        '^(?P<key>[ABCDEFGabcdefg])'
        # Next, look for #, b, or nothing
        '(?P<flatsharp>[#b]?)'
        # Allow for a space between key and mode
        ' ?'
        # Next, look for any of the mode strings
        '(?P<mode>(?:(?:' +
        # Next, look for any of the major or minor mode strings
        ')|(?:'.join(major_strs + minor_strs) + '))?)$')
    # Match provided key string
    result = re.match(pattern, key_string)
    if result is None:
        raise ValueError('Supplied key {} is not valid.'.format(key_string))
    # Convert result to dictionary
    result = result.groupdict()

    # Map from key string to pitch class number
    key_number = {'c': 0, 'd': 2, 'e': 4, 'f': 5,
                  'g': 7, 'a': 9, 'b': 11}[result['key'].lower()]
    # Increment or decrement pitch class if a flat or sharp was specified
    if result['flatsharp']:
        if result['flatsharp'] == '#':
            key_number += 1
        elif result['flatsharp'] == 'b':
            key_number -= 1
    # Circle around 12 pitch classes
    key_number = key_number % 12
    # Offset if mode is minor, or the key name is lowercase
    if result['mode'] in minor_strs or (result['key'].islower() and
                                        result['mode'] not in major_strs):
        key_number += 12

    return key_number


def mode_accidentals_to_key_number(mode, num_accidentals):
    """Convert a given number of accidentals and mode to a key number.

    Parameters
    ----------
    mode : int
        0 is major, 1 is minor.
    num_accidentals : int
        Positive number is used for sharps, negative number is used for flats.

    Returns
    -------
    key_number : int
        Integer representing the key and its mode.
    """

    if not (isinstance(num_accidentals, int) and
            num_accidentals > -8 and
            num_accidentals < 8):
        raise ValueError('Number of accidentals {} is not valid'.format(
            num_accidentals))
    if mode not in (0, 1):
        raise ValueError('Mode {} is not recognizable, must be 0 or 1'.format(
            mode))

    sharp_keys = 'CGDAEBF'
    flat_keys = 'FBEADGC'

    # check if key signature has sharps or flats
    if num_accidentals >= 0:
        num_sharps = num_accidentals // 6
        key = sharp_keys[num_accidentals % 7] + '#' * int(num_sharps)
    else:
        if num_accidentals == -1:
            key = 'F'
        else:
            key = flat_keys[(-1 * num_accidentals - 1) % 7] + 'b'

    # find major key number
    key += ' Major'

    # use routine to convert from string notation to number notation
    key_number = key_name_to_key_number(key)

    # if minor, offset
    if mode == 1:
        key_number = 12 + ((key_number - 3) % 12)

    return key_number


def key_number_to_mode_accidentals(key_number):
    """Converts a key number to number of accidentals and mode.

    Parameters
    ----------
    key_number : int
        Key number as used in ``pretty_midi``.

    Returns
    -------
    mode : int
        0 for major, 1 for minor.
    num_accidentals : int
        Number of accidentals.
        Positive is for sharps and negative is for flats.
    """

    if not ((isinstance(key_number, int) and
             key_number >= 0 and
             key_number < 24)):
        raise ValueError('Key number {} is not a must be an int between 0 and '
                         '24'.format(key_number))

    pc_to_num_accidentals_major = {0: 0, 1: -5, 2: 2, 3: -3, 4: 4, 5: -1, 6: 6,
                                   7: 1, 8: -4, 9: 3, 10: -2, 11: 5}
    mode = key_number // 12

    if mode == 0:
        num_accidentals = pc_to_num_accidentals_major[key_number]
        return mode, num_accidentals
    elif mode == 1:
        key_number = (key_number + 3) % 12
        num_accidentals = pc_to_num_accidentals_major[key_number]
        return mode, num_accidentals
    else:
        return None


def qpm_to_bpm(quarter_note_tempo, numerator, denominator):
    """Converts from quarter notes per minute to beats per minute.

    Parameters
    ----------
    quarter_note_tempo : float
        Quarter note tempo.
    numerator : int
        Numerator of time signature.
    denominator : int
        Denominator of time signature.

    Returns
    -------
    bpm : float
        Tempo in beats per minute.
    """

    if not (isinstance(quarter_note_tempo, (int, float)) and
            quarter_note_tempo > 0):
        raise ValueError(
            'Quarter notes per minute must be an int or float '
            'greater than 0, but {} was supplied'.format(quarter_note_tempo))
    if not (isinstance(numerator, int) and numerator > 0):
        raise ValueError(
            'Time signature numerator must be an int greater than 0, but {} '
            'was supplied.'.format(numerator))
    if not (isinstance(denominator, int) and denominator > 0):
        raise ValueError(
            'Time signature denominator must be an int greater than 0, but {} '
            'was supplied.'.format(denominator))

    # denominator is whole, half, quarter, eighth, sixteenth or 32nd note
    if denominator in [1, 2, 4, 8, 16, 32]:
        # simple triple
        if numerator == 3:
            return quarter_note_tempo * denominator / 4.0
        # compound meter 6/8*n, 9/8*n, 12/8*n...
        elif numerator % 3 == 0:
            return quarter_note_tempo / 3.0 * denominator / 4.0
        # strongly assume two eighths equal a beat
        else:
            return quarter_note_tempo * denominator / 4.0
    else:
        return quarter_note_tempo


def note_number_to_hz(note_number):
    """Convert a (fractional) MIDI note number to its frequency in Hz.

    Parameters
    ----------
    note_number : float
        MIDI note number, can be fractional.

    Returns
    -------
    note_frequency : float
        Frequency of the note in Hz.

    """
    # MIDI note numbers are defined as the number of semitones relative to C0
    # in a 440 Hz tuning
    return 440.0*(2.0**((note_number - 69)/12.0))


def hz_to_note_number(frequency):
    """Convert a frequency in Hz to a (fractional) note number.

    Parameters
    ----------
    frequency : float
        Frequency of the note in Hz.

    Returns
    -------
    note_number : float
        MIDI note number, can be fractional.

    """
    # MIDI note numbers are defined as the number of semitones relative to C0
    # in a 440 Hz tuning
    return 12*(np.log2(frequency) - np.log2(440.0)) + 69


def note_name_to_number(note_name):
    """Converts a note name in the format
    ``'(note)(accidental)(octave number)'`` (e.g. ``'C#4'``) to MIDI note
    number.

    ``'(note)'`` is required, and is case-insensitive.

    ``'(accidental)'`` should be ``''`` for natural, ``'#'`` for sharp and
    ``'!'`` or ``'b'`` for flat.

    If ``'(octave)'`` is ``''``, octave 0 is assumed.

    Parameters
    ----------
    note_name : str
        A note name, as described above.

    Returns
    -------
    note_number : int
        MIDI note number corresponding to the provided note name.

    Notes
    -----
        Thanks to Brian McFee.

    """

    # Map note name to the semitone
    pitch_map = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
    # Relative change in semitone denoted by each accidental
    acc_map = {'#': 1, '': 0, 'b': -1, '!': -1}

    # Reg exp will raise an error when the note name is not valid
    try:
        # Extract pitch, octave, and accidental from the supplied note name
        match = re.match(r'^(?P<n>[A-Ga-g])(?P<off>[#b!]?)(?P<oct>[+-]?\d+)$',
                         note_name)

        pitch = match.group('n').upper()
        offset = acc_map[match.group('off')]
        octave = int(match.group('oct'))
    except:
        raise ValueError('Improper note format: {}'.format(note_name))

    # Convert from the extrated ints to a full note number
    return 12*(octave + 1) + pitch_map[pitch] + offset


def note_number_to_name(note_number):
    """Convert a MIDI note number to its name, in the format
    ``'(note)(accidental)(octave number)'`` (e.g. ``'C#4'``).

    Parameters
    ----------
    note_number : int
        MIDI note number.  If not an int, it will be rounded.

    Returns
    -------
    note_name : str
        Name of the supplied MIDI note number.

    Notes
    -----
        Thanks to Brian McFee.

    """

    # Note names within one octave
    semis = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

    # Ensure the note is an int
    note_number = int(np.round(note_number))

    # Get the semitone and the octave, and concatenate to create the name
    return semis[note_number % 12] + str(note_number//12 - 1)


def note_number_to_drum_name(note_number):
    """Converts a MIDI note number in a percussion instrument to the
    corresponding drum name, according to the General MIDI standard.

    Any MIDI note number outside of the valid range (note 35-81, zero-indexed)
    will result in an empty string.

    Parameters
    ----------
    note_number : int
        MIDI note number.  If not an int, it will be rounded.

    Returns
    -------
    drum_name : str
        Name of the drum for this note for a percussion instrument.

    Notes
    -----
        See http://www.midi.org/techspecs/gm1sound.php

    """

    # Ensure note is an int
    note_number = int(np.round(note_number))
    # General MIDI only defines drum names for notes 35-81
    if note_number < 35 or note_number > 81:
        return ''
    else:
        # Our DRUM_MAP starts from index 0; drum names start from 35
        return DRUM_MAP[note_number - 35]


def __normalize_str(name):
    """Removes all non-alphanumeric characters from a string and converts
    it to lowercase.

    """
    return ''.join(ch for ch in name if ch.isalnum()).lower()


def drum_name_to_note_number(drum_name):
    """Converts a drum name to the corresponding MIDI note number for a
    percussion instrument.  Conversion is case, whitespace, and
    non-alphanumeric character insensitive.

    Parameters
    ----------
    drum_name : str
        Name of a drum which exists in the general MIDI standard.
        If the drum is not found, a ValueError is raised.

    Returns
    -------
    note_number : int
        The MIDI note number corresponding to this drum.

    Notes
    -----
        See http://www.midi.org/techspecs/gm1sound.php

    """

    normalized_drum_name = __normalize_str(drum_name)
    # Create a list of the entries DRUM_MAP, normalized, to search over
    normalized_drum_names = [__normalize_str(name) for name in DRUM_MAP]

    # If the normalized drum name is not found, complain
    try:
        note_index = normalized_drum_names.index(normalized_drum_name)
    except:
        raise ValueError('{} is not a valid General MIDI drum '
                         'name.'.format(drum_name))

    # If an index was found, it will be 0-based; add 35 to get the note number
    return note_index + 35


def program_to_instrument_name(program_number):
    """Converts a MIDI program number to the corresponding General MIDI
    instrument name.

    Parameters
    ----------
    program_number : int
        MIDI program number, between 0 and 127.

    Returns
    -------
    instrument_name : str
        Name of the instrument corresponding to this program number.

    Notes
    -----
        See http://www.midi.org/techspecs/gm1sound.php

    """

    # Check that the supplied program is in the valid range
    if program_number < 0 or program_number > 127:
        raise ValueError('Invalid program number {}, should be between 0 and'
                         ' 127'.format(program_number))
    # Just grab the name from the instrument mapping list
    return INSTRUMENT_MAP[program_number]


def instrument_name_to_program(instrument_name):
    """Converts an instrument name to the corresponding General MIDI program
    number.  Conversion is case, whitespace, and non-alphanumeric character
    insensitive.

    Parameters
    ----------
    instrument_name : str
        Name of an instrument which exists in the general MIDI standard.
        If the instrument is not found, a ValueError is raised.

    Returns
    -------
    program_number : int
        The MIDI program number corresponding to this instrument.

    Notes
    -----
        See http://www.midi.org/techspecs/gm1sound.php

    """

    normalized_inst_name = __normalize_str(instrument_name)
    # Create a list of the entries INSTRUMENT_MAP, normalized, to search over
    normalized_inst_names = [__normalize_str(name) for name in
                             INSTRUMENT_MAP]

    # If the normalized drum name is not found, complain
    try:
        program_number = normalized_inst_names.index(normalized_inst_name)
    except:
        raise ValueError('{} is not a valid General MIDI instrument '
                         'name.'.format(instrument_name))

    # Return the index (program number) if a match was found
    return program_number


def program_to_instrument_class(program_number):
    """Converts a MIDI program number to the corresponding General MIDI
    instrument class.

    Parameters
    ----------
    program_number : int
        MIDI program number, between 0 and 127.

    Returns
    -------
    instrument_class : str
        Name of the instrument class corresponding to this program number.

    Notes
    -----
        See http://www.midi.org/techspecs/gm1sound.php

    """

    # Check that the supplied program is in the valid range
    if program_number < 0 or program_number > 127:
        raise ValueError('Invalid program number {}, should be between 0 and'
                         ' 127'.format(program_number))
    # Just grab the name from the instrument mapping list
    return INSTRUMENT_CLASSES[int(program_number)//8]


def pitch_bend_to_semitones(pitch_bend, semitone_range=2.):
    """Convert a MIDI pitch bend value (in the range ``[-8192, 8191]``) to the
    bend amount in semitones.

    Parameters
    ----------
    pitch_bend : int
        MIDI pitch bend amount, in ``[-8192, 8191]``.
    semitone_range : float
        Convert to +/- this semitone range.  Default is 2., which is the
        General MIDI standard +/-2 semitone range.

    Returns
    -------
    semitones : float
        Number of semitones corresponding to this pitch bend amount.

    """

    return semitone_range*pitch_bend/8192.0


def semitones_to_pitch_bend(semitones, semitone_range=2.):
    """Convert a semitone value to the corresponding MIDI pitch bend integer.

    Parameters
    ----------
    semitones : float
        Number of semitones for the pitch bend.
    semitone_range : float
        Convert to +/- this semitone range.  Default is 2., which is the
        General MIDI standard +/-2 semitone range.

    Returns
    -------
    pitch_bend : int
        MIDI pitch bend amount, in ``[-8192, 8191]``.

    """
    return int(8192*(semitones/semitone_range))
