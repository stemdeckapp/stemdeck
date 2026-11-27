"""These classes simply hold MIDI data in a convenient form.

"""
from __future__ import print_function

from .utilities import key_number_to_key_name


class Note(object):
    """A note event.

    Parameters
    ----------
    velocity : int
        Note velocity.
    pitch : int
        Note pitch, as a MIDI note number.
    start : float
        Note on time, absolute, in seconds.
    end : float
        Note off time, absolute, in seconds.

    """

    def __init__(self, velocity, pitch, start, end):
        if end < start:
            raise ValueError("Note end time must be greater than start time")
        
        self.velocity = velocity
        self.pitch = pitch
        self.start = start
        self.end = end

    def get_duration(self):
        """Get the duration of the note in seconds."""
        return self.end - self.start

    @property
    def duration(self):
        return self.get_duration()

    def __repr__(self):
        return 'Note(start={:f}, end={:f}, pitch={}, velocity={})'.format(
            self.start, self.end, self.pitch, self.velocity)


class PitchBend(object):
    """A pitch bend event.

    Parameters
    ----------
    pitch : int
        MIDI pitch bend amount, in the range ``[-8192, 8191]``.
    time : float
        Time where the pitch bend occurs.

    """

    def __init__(self, pitch, time):
        self.pitch = pitch
        self.time = time

    def __repr__(self):
        return 'PitchBend(pitch={:d}, time={:f})'.format(self.pitch, self.time)


class ControlChange(object):
    """A control change event.

    Parameters
    ----------
    number : int
        The control change number, in ``[0, 127]``.
    value : int
        The value of the control change, in ``[0, 127]``.
    time : float
        Time where the control change occurs.

    """

    def __init__(self, number, value, time):
        self.number = number
        self.value = value
        self.time = time

    def __repr__(self):
        return ('ControlChange(number={:d}, value={:d}, '
                'time={:f})'.format(self.number, self.value, self.time))


class TimeSignature(object):
    """Container for a Time Signature event, which contains the time signature
    numerator, denominator and the event time in seconds.

    Attributes
    ----------
    numerator : int
        Numerator of time signature.
    denominator : int
        Denominator of time signature.
    time : float
        Time of event in seconds.

    Examples
    --------
    Instantiate a TimeSignature object with 6/8 time signature at 3.14 seconds:

    >>> ts = TimeSignature(6, 8, 3.14)
    >>> print(ts)
    6/8 at 3.14 seconds

    """

    def __init__(self, numerator, denominator, time):
        if not (isinstance(numerator, int) and numerator > 0):
            raise ValueError(
                '{} is not a valid `numerator` type or value'.format(
                    numerator))
        if not (isinstance(denominator, int) and denominator > 0):
            raise ValueError(
                '{} is not a valid `denominator` type or value'.format(
                    denominator))
        if not (isinstance(time, (int, float)) and time >= 0):
            raise ValueError(
                '{} is not a valid `time` type or value'.format(time))

        self.numerator = numerator
        self.denominator = denominator
        self.time = time

    def __repr__(self):
        return "TimeSignature(numerator={}, denominator={}, time={})".format(
            self.numerator, self.denominator, self.time)

    def __str__(self):
        return '{}/{} at {:.2f} seconds'.format(
            self.numerator, self.denominator, self.time)


class KeySignature(object):
    """Contains the key signature and the event time in seconds.
    Only supports major and minor keys.

    Attributes
    ----------
    key_number : int
        Key number according to ``[0, 11]`` Major, ``[12, 23]`` minor.
        For example, 0 is C Major, 12 is C minor.
    time : float
        Time of event in seconds.

    Examples
    --------
    Instantiate a C# minor KeySignature object at 3.14 seconds:

    >>> ks = KeySignature(13, 3.14)
    >>> print(ks)
    C# minor at 3.14 seconds
    """

    def __init__(self, key_number, time):
        if not all((isinstance(key_number, int),
                    key_number >= 0,
                    key_number < 24)):
            raise ValueError(
                '{} is not a valid `key_number` type or value'.format(
                    key_number))
        if not (isinstance(time, (int, float)) and time >= 0):
            raise ValueError(
                '{} is not a valid `time` type or value'.format(time))

        self.key_number = key_number
        self.time = time

    def __repr__(self):
        return "KeySignature(key_number={}, time={})".format(
            self.key_number, self.time)

    def __str__(self):
        return '{} at {:.2f} seconds'.format(
            key_number_to_key_name(self.key_number), self.time)


class Lyric(object):
    """Timestamped lyric text.

    Attributes
    ----------
    text : str
        The text of the lyric.
    time : float
        The time in seconds of the lyric.
    """

    def __init__(self, text, time):
        self.text = text
        self.time = time

    def __repr__(self):
        return 'Lyric(text="{}", time={})'.format(
            self.text.replace('"', r'\"'), self.time)

    def __str__(self):
        return '"{}" at {:.2f} seconds'.format(self.text, self.time)

class Text(object):
    """Timestamped text event.

    Attributes
    ----------
    text : str
        The text.
    time : float
        The time it occurs in seconds.
    """

    def __init__(self, text, time):
        self.text = text
        self.time = time

    def __repr__(self):
        return 'Text(text="{}", time={})'.format(
            self.text.replace('"', r'\"'), self.time)

    def __str__(self):
        return '"{}" at {:.2f} seconds'.format(self.text, self.time)
