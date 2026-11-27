"""Utility functions for handling fluidsynth

"""

import os
import importlib_resources

try:
    import fluidsynth
    _HAS_FLUIDSYNTH = True
except ImportError:
    _HAS_FLUIDSYNTH = False

DEFAULT_SF2 = 'TimGM6mb.sf2'
DEFAULT_SAMPLE_RATE = 44100


def get_fluidsynth_instance(synthesizer=None, sfid=0, fs=None):
    """ Check if a valid fluidsynth.Synth instance is provided, and if not,
    create one.

    Parameters
    ----------
    synthesizer : fluidsynth.Synth or str
        fluidsynth.Synth instance to use or a string with the path to a .sf2 file.
        Default ``None``, which creates a new instance using the TimGM6mb.sf2 file
        included with ``pretty_midi``.
    sfid : int
        Soundfont ID to use if an instance of fluidsynth.Synth is provided.
        Default ``0``, which uses the first soundfont.
    fs : int
        Sampling rate to synthesize at.
        Default ``None``, which falls back to ``pretty_midi.fluidsynth.DEFAULT_SAMPLE_RATE``
        = 44100 if a new instance must be created.
        If ``synthesizer`` is an existing instance and ``fs`` is specified, then
        ValueError will be raised if the sample rates are not equal.

    Returns
    -------
    synthesizer : fluidsynth.Synth
        fluidsynth.Synth instance
    sfid : int
        Soundfont ID
    new_instance_created : bool
        Whether a new instance of fluidsynth.Synth was created.

    """
    if not _HAS_FLUIDSYNTH:
        raise ImportError("fluidsynth() was called but pyfluidsynth is not installed.")

    if synthesizer is None:
        synthesizer = os.path.join(str(importlib_resources.files(__name__)), DEFAULT_SF2)

    # Create a fluidsynth instance if one wasn't provided
    if isinstance(synthesizer, str):
        sf2_path = synthesizer
        if not os.path.exists(synthesizer):
            raise ValueError("No soundfont file found at the supplied path {}".format(sf2_path))
        fs = fs or DEFAULT_SAMPLE_RATE
        synthesizer = fluidsynth.Synth(samplerate=fs)
        sfid = synthesizer.sfload(sf2_path)
        new_instance_created = True
    elif isinstance(synthesizer, fluidsynth.Synth):
        synth_fs = synthesizer.get_setting('synth.sample-rate')
        if fs and synth_fs != fs:
            raise ValueError(
                f"synthesizer sample rate of {synth_fs} doesn't match provided fs of {fs}")
        new_instance_created = False
    else:
        raise ValueError("synthesizer must be a str or a fluidsynth.Synth instance")

    return synthesizer, sfid, new_instance_created
