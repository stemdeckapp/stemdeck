"""
``pretty_midi`` contains utility function/classes for handling MIDI data,
so that it's in a format from which it is easy to modify and extract
information.
If you end up using ``pretty_midi`` in a published research project, please
cite the following report:

Colin Raffel and Daniel P. W. Ellis.
`Intuitive Analysis, Creation and Manipulation of MIDI Data with pretty_midi
<http://colinraffel.com/publications/ismir2014intuitive.pdf>`_.
In 15th International Conference on Music Information Retrieval Late Breaking
and Demo Papers, 2014.

Example usage for analyzing, manipulating and synthesizing a MIDI file:

.. code-block:: python

    import pretty_midi
    # Load MIDI file into PrettyMIDI object
    midi_data = pretty_midi.PrettyMIDI('example.mid')
    # Print an empirical estimate of its global tempo
    print midi_data.estimate_tempo()
    # Compute the relative amount of each semitone across the entire song,
    # a proxy for key
    total_velocity = sum(sum(midi_data.get_chroma()))
    print [sum(semitone)/total_velocity for semitone in midi_data.get_chroma()]
    # Shift all notes up by 5 semitones
    for instrument in midi_data.instruments:
        # Don't want to shift drum notes
        if not instrument.is_drum:
            for note in instrument.notes:
                note.pitch += 5
    # Synthesize the resulting MIDI data using sine waves
    audio_data = midi_data.synthesize()

Example usage for creating a simple MIDI file:

.. code-block:: python

    import pretty_midi
    # Create a PrettyMIDI object
    cello_c_chord = pretty_midi.PrettyMIDI()
    # Create an Instrument instance for a cello instrument
    cello_program = pretty_midi.instrument_name_to_program('Cello')
    cello = pretty_midi.Instrument(program=cello_program)
    # Iterate over note names, which will be converted to note number later
    for note_name in ['C5', 'E5', 'G5']:
        # Retrieve the MIDI note number for this note name
        note_number = pretty_midi.note_name_to_number(note_name)
        # Create a Note instance, starting at 0s and ending at .5s
        note = pretty_midi.Note(
            velocity=100, pitch=note_number, start=0, end=.5)
        # Add it to our cello instrument
        cello.notes.append(note)
    # Add the cello instrument to the PrettyMIDI object
    cello_c_chord.instruments.append(cello)
    # Write out the MIDI data
    cello_c_chord.write('cello-C-chord.mid')

Further examples can be found in the source tree's `examples directory
<https://github.com/craffel/pretty-midi/tree/master/examples>`_.

``pretty_midi.PrettyMIDI``
==========================

.. autoclass:: PrettyMIDI
   :members:
   :undoc-members:

``pretty_midi.Instrument``
==========================

.. autoclass:: Instrument
   :members:
   :undoc-members:

``pretty_midi.Note``
====================

.. autoclass:: Note
   :members:
   :undoc-members:

``pretty_midi.PitchBend``
=========================

.. autoclass:: PitchBend
   :members:
   :undoc-members:

``pretty_midi.ControlChange``
=============================

.. autoclass:: ControlChange
   :members:
   :undoc-members:

``pretty_midi.TimeSignature``
=============================

.. autoclass:: TimeSignature
   :members:
   :undoc-members:

``pretty_midi.KeySignature``
============================

.. autoclass:: KeySignature
   :members:
   :undoc-members:

``pretty_midi.Lyric``
=====================

.. autoclass:: Lyric
   :members:
   :undoc-members:

``pretty_midi.Text``
=====================

.. autoclass:: Text
   :members:
   :undoc-members:

Utility functions
=================
.. autofunction:: key_number_to_key_name
.. autofunction:: key_name_to_key_number
.. autofunction:: mode_accidentals_to_key_number
.. autofunction:: key_number_to_mode_accidentals
.. autofunction:: qpm_to_bpm
.. autofunction:: note_number_to_hz
.. autofunction:: hz_to_note_number
.. autofunction:: note_name_to_number
.. autofunction:: note_number_to_name
.. autofunction:: note_number_to_drum_name
.. autofunction:: drum_name_to_note_number
.. autofunction:: program_to_instrument_name
.. autofunction:: instrument_name_to_program
.. autofunction:: program_to_instrument_class
.. autofunction:: pitch_bend_to_semitones
.. autofunction:: semitones_to_pitch_bend
"""
from .pretty_midi import *
from .instrument import *
from .containers import *
from .utilities import *
from .constants import *

__version__ = '0.2.11.post0'
