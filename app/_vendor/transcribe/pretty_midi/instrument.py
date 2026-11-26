"""The Instrument class holds all events for a single instrument and contains
functions for extracting information from the events it contains.
"""
from warnings import warn
import numpy as np

from .containers import PitchBend
from .utilities import pitch_bend_to_semitones, note_number_to_hz
from .fluidsynth import get_fluidsynth_instance


class Instrument(object):
    """Object to hold event information for a single instrument.

    Parameters
    ----------
    program : int
        MIDI program number (instrument index), in ``[0, 127]``.
    is_drum : bool
        Is the instrument a drum instrument (channel 9)?
    name : str
        Name of the instrument.

    Attributes
    ----------
    program : int
        The program number of this instrument.
    is_drum : bool
        Is the instrument a drum instrument (channel 9)?
    name : str
        Name of the instrument.
    notes : list
        List of :class:`pretty_midi.Note` objects.
    pitch_bends : list
        List of of :class:`pretty_midi.PitchBend` objects.
    control_changes : list
        List of :class:`pretty_midi.ControlChange` objects.

    """

    def __init__(self, program, is_drum=False, name=''):
        """Create the Instrument.

        """
        self.program = program
        self.is_drum = is_drum
        self.name = name
        self.notes = []
        self.pitch_bends = []
        self.control_changes = []

    def get_onsets(self):
        """Get all onsets of all notes played by this instrument.
        May contain duplicates.

        Returns
        -------
        onsets : np.ndarray
                List of all note onsets.

        """
        onsets = []
        # Get the note-on time of each note played by this instrument
        for note in self.notes:
            onsets.append(note.start)
        # Return them sorted (because why not?)
        return np.sort(onsets)

    def get_piano_roll(self, fs=100, times=None,
                       pedal_threshold=64):
        """Compute a piano roll matrix of this instrument.

        Parameters
        ----------
        fs : int
            Sampling frequency of the columns, i.e. each column is spaced apart
            by ``1./fs`` seconds.
        times : np.ndarray
            Times of the start of each column in the piano roll.
            Default ``None`` which is ``np.arange(0, get_end_time(), 1./fs)``.
        pedal_threshold : int
            Value of control change 64 (sustain pedal) message that is less
            than this value is reflected as pedal-off.  Pedals will be
            reflected as elongation of notes in the piano roll.
            If None, then CC64 message is ignored.
            Default is 64.

        Returns
        -------
        piano_roll : np.ndarray, shape=(128,times.shape[0])
            Piano roll of this instrument.

        """
        # If there are no notes, return an empty matrix
        if self.notes == []:
            return np.array([[]]*128)
        # Get the end time of the last event
        end_time = self.get_end_time()
        # Extend end time if one was provided
        if times is not None and times[-1] > end_time:
            end_time = times[-1]
        # Allocate a matrix of zeros - we will add in as we go
        piano_roll = np.zeros((128, int(fs*end_time)))
        # Drum tracks don't have pitch, so return a matrix of zeros
        if self.is_drum:
            if times is None:
                return piano_roll
            else:
                return np.zeros((128, times.shape[0]))
        # Add up piano roll matrix, note-by-note
        for note in self.notes:
            # Should interpolate
            piano_roll[note.pitch,
                       int(note.start*fs):int(note.end*fs)] += note.velocity

        # Process sustain pedals
        if pedal_threshold is not None:
            CC_SUSTAIN_PEDAL = 64
            time_pedal_on = 0
            is_pedal_on = False
            for cc in [_e for _e in self.control_changes
                       if _e.number == CC_SUSTAIN_PEDAL]:
                time_now = int(cc.time*fs)
                is_current_pedal_on = (cc.value >= pedal_threshold)
                if not is_pedal_on and is_current_pedal_on:
                    time_pedal_on = time_now
                    is_pedal_on = True
                elif is_pedal_on and not is_current_pedal_on:
                    # For each pitch, a sustain pedal "retains"
                    # the maximum velocity up to now due to
                    # logarithmic nature of human loudness perception
                    subpr = piano_roll[:, time_pedal_on:time_now]

                    # Take the running maximum
                    pedaled = np.maximum.accumulate(subpr, axis=1)
                    piano_roll[:, time_pedal_on:time_now] = pedaled
                    is_pedal_on = False

        # Process pitch changes
        # Need to sort the pitch bend list for the following to work
        ordered_bends = sorted(self.pitch_bends, key=lambda bend: bend.time)
        # Add in a bend of 0 at the end of time
        end_bend = PitchBend(0, end_time)
        for start_bend, end_bend in zip(ordered_bends,
                                        ordered_bends[1:] + [end_bend]):
            # Piano roll is already generated with everything bend = 0
            if np.abs(start_bend.pitch) < 1:
                continue
            # Get integer and decimal part of bend amount
            start_pitch = pitch_bend_to_semitones(start_bend.pitch)
            bend_int = int(np.sign(start_pitch)*np.floor(np.abs(start_pitch)))
            bend_decimal = np.abs(start_pitch - bend_int)
            # Column indices effected by the bend
            bend_range = np.r_[int(start_bend.time*fs):int(end_bend.time*fs)]
            # Construct the bent part of the piano roll
            bent_roll = np.zeros(piano_roll[:, bend_range].shape)
            # Easiest to process differently depending on bend sign
            if start_bend.pitch >= 0:
                # First, pitch shift by the int amount
                if bend_int != 0:
                    bent_roll[bend_int:] = piano_roll[:-bend_int, bend_range]
                else:
                    bent_roll = piano_roll[:, bend_range]
                # Now, linear interpolate by the decimal place
                bent_roll[1:] = ((1 - bend_decimal)*bent_roll[1:] +
                                 bend_decimal*bent_roll[:-1])
            else:
                # Same procedure as for positive bends
                if bend_int != 0:
                    bent_roll[:bend_int] = piano_roll[-bend_int:, bend_range]
                else:
                    bent_roll = piano_roll[:, bend_range]
                bent_roll[:-1] = ((1 - bend_decimal)*bent_roll[:-1] +
                                  bend_decimal*bent_roll[1:])
            # Store bent portion back in piano roll
            piano_roll[:, bend_range] = bent_roll

        if times is None:
            return piano_roll
        piano_roll_integrated = np.zeros((128, times.shape[0]))
        # Convert to column indices
        times = np.array(np.round(times*fs), dtype=int)
        for n, (start, end) in enumerate(zip(times[:-1], times[1:])):
            if start < piano_roll.shape[1]:  # if start is >=, leave zeros
                if start == end:
                    end = start + 1
                # Each column is the mean of the columns in piano_roll
                piano_roll_integrated[:, n] = np.mean(piano_roll[:, start:end],
                                                      axis=1)
        return piano_roll_integrated

    def get_chroma(self, fs=100, times=None, pedal_threshold=64):
        """Get a sequence of chroma vectors from this instrument.

        Parameters
        ----------
        fs : int
            Sampling frequency of the columns, i.e. each column is spaced apart
            by ``1./fs`` seconds.
        times : np.ndarray
            Times of the start of each column in the piano roll.
            Default ``None`` which is ``np.arange(0, get_end_time(), 1./fs)``.
        pedal_threshold : int
            Value of control change 64 (sustain pedal) message that is less
            than this value is reflected as pedal-off.  Pedals will be
            reflected as elongation of notes in the piano roll.
            If None, then CC64 message is ignored.
            Default is 64.

        Returns
        -------
        piano_roll : np.ndarray, shape=(12,times.shape[0])
            Chromagram of this instrument.

        """
        # First, get the piano roll
        piano_roll = self.get_piano_roll(fs=fs, times=times,
                                         pedal_threshold=pedal_threshold)
        # Fold into one octave
        chroma_matrix = np.zeros((12, piano_roll.shape[1]))
        for note in range(12):
            chroma_matrix[note, :] = np.sum(piano_roll[note::12], axis=0)
        return chroma_matrix

    def get_end_time(self):
        """Returns the time of the end of the events in this instrument.

        Returns
        -------
        end_time : float
            Time, in seconds, of the last event.

        """
        # Cycle through all note ends and all pitch bends and find the largest
        events = ([n.end for n in self.notes] +
                  [b.time for b in self.pitch_bends] +
                  [c.time for c in self.control_changes])
        # If there are no events, just return 0
        if len(events) == 0:
            return 0.
        else:
            return max(events)

    def get_pitch_class_histogram(self, use_duration=False, use_velocity=False,
                                  normalize=False):
        """Computes the frequency of pitch classes of this instrument,
        optionally weighted by their durations or velocities.

        Parameters
        ----------
        use_duration : bool
            Weight frequency by note duration.
        use_velocity : bool
            Weight frequency by note velocity.
        normalize : bool
            Normalizes the histogram such that the sum of bin values is 1.

        Returns
        -------
        histogram : np.ndarray, shape=(12,)
            Histogram of pitch classes given current instrument, optionally
            weighted by their durations or velocities.
        """

        # Return all zeros if track is drum
        if self.is_drum:
            return np.zeros(12)

        weights = np.ones(len(self.notes))

        # Assumes that duration and velocity have equal weight
        if use_duration:
            weights *= [note.end - note.start for note in self.notes]
        if use_velocity:
            weights *= [note.velocity for note in self.notes]

        histogram, _ = np.histogram([n.pitch % 12 for n in self.notes],
                                    bins=np.arange(13),
                                    weights=weights,
                                    density=normalize)

        return histogram

    def get_pitch_class_transition_matrix(self, normalize=False,
                                          time_thresh=0.05):
        """Computes the pitch class transition matrix of this instrument.
        Transitions are added whenever the end of a note is within
        ``time_tresh`` from the start of any other note.

        Parameters
        ----------
        normalize : bool
            Normalize transition matrix such that matrix sum equals to 1.
        time_thresh : float
            Maximum temporal threshold, in seconds, between the start of a note
            and end time of any other note for a transition to be added.

        Returns
        -------
        transition_matrix : np.ndarray, shape=(12,12)
            Pitch class transition matrix.
        """

        # instrument is drum or less than one note, return all zeros
        if self.is_drum or len(self.notes) <= 1:
            return np.zeros((12, 12))

        # retrieve note starts, ends and pitch classes(nodes) from self.notes
        starts, ends, nodes = np.array(
            [[x.start, x.end, x.pitch % 12] for x in self.notes]).T

        # compute distance matrix for all start and end time pairs
        dist_mat = np.subtract.outer(ends, starts)

        # find indices of pairs of notes where the end time of one note is
        # within time_thresh of the start time of the other
        sources, targets = np.where(abs(dist_mat) < time_thresh)

        transition_matrix, _, _ = np.histogram2d(nodes[sources],
                                                 nodes[targets],
                                                 bins=np.arange(13),
                                                 density=normalize)
        return transition_matrix

    def remove_invalid_notes(self):
        """Removes any notes whose end time is before or at their start time.

        """
        # Crete a list of all invalid notes
        notes_to_delete = []
        for note in self.notes:
            if note.end <= note.start:
                notes_to_delete.append(note)
        # Remove the notes found
        for note in notes_to_delete:
            self.notes.remove(note)

    def synthesize(self, fs=44100, wave=np.sin):
        """Synthesize the instrument's notes using some waveshape.
        For drum instruments, returns zeros.

        Parameters
        ----------
        fs : int
            Sampling rate of the synthesized audio signal.
        wave : function
            Function which returns a periodic waveform,
            e.g. ``np.sin``, ``scipy.signal.square``, etc.

        Returns
        -------
        synthesized : np.ndarray
            Waveform of the instrument's notes, synthesized at ``fs``.

        """
        # Pre-allocate output waveform
        synthesized = np.zeros(int(fs*(self.get_end_time() + 1)))

        # If we're a percussion channel, just return the zeros
        if self.is_drum:
            return synthesized
        # If the above if statement failed, we need to revert back to default
        if not hasattr(wave, '__call__'):
            raise ValueError('wave should be a callable Python function')
        # This is a simple way to make the end of the notes fade-out without
        # clicks
        fade_out = np.linspace(1, 0, int(.1*fs))
        # Create a frequency multiplier array for pitch bend
        bend_multiplier = np.ones(synthesized.shape)
        # Need to sort the pitch bend list for the loop below to work
        ordered_bends = sorted(self.pitch_bends, key=lambda bend: bend.time)
        # Add in a bend of 0 at the end of time
        end_bend = PitchBend(0, self.get_end_time())
        for start_bend, end_bend in zip(ordered_bends,
                                        ordered_bends[1:] + [end_bend]):
            # Bend start and end time in samples
            start = int(start_bend.time*fs)
            end = int(end_bend.time*fs)
            # The multiplier will be (twelfth root of 2)^(bend semitones)
            bend_semitones = pitch_bend_to_semitones(start_bend.pitch)
            bend_amount = (2**(1/12.))**bend_semitones
            # Sample indices effected by the bend
            bend_multiplier[start:end] = bend_amount
        # Add in waveform for each note
        for note in self.notes:
            # Indices in samples of this note
            start = int(fs*note.start)
            end = int(fs*note.end)
            # Get frequency of note from MIDI note number
            frequency = note_number_to_hz(note.pitch)
            # When a pitch bend gets applied, there will be a sample
            # discontinuity. So, we also need an array of offsets which get
            # applied to compensate.
            offsets = np.zeros(end - start)
            for bend in ordered_bends:
                bend_sample = int(bend.time*fs)
                # Does this pitch bend fall within this note?
                if bend_sample > start and bend_sample < end:
                    # Compute the average bend so far
                    bend_so_far = bend_multiplier[start:bend_sample].mean()
                    bend_amount = bend_multiplier[bend_sample]
                    # Compute the offset correction
                    offset = (bend_so_far - bend_amount)*(bend_sample - start)
                    # Store this offset for samples effected
                    offsets[bend_sample - start:] = offset
            # Compute the angular frequencies, bent, over this interval
            frequencies = 2*np.pi*frequency*(bend_multiplier[start:end])/fs
            # Synthesize using wave function at this frequency
            note_waveform = wave(frequencies*np.arange(end - start) +
                                 2*np.pi*frequency*offsets/fs)
            # Apply an exponential envelope
            envelope = np.exp(-np.arange(end - start)/(1.0*fs))
            # Make the end of the envelope be a fadeout
            if envelope.shape[0] > fade_out.shape[0]:
                envelope[-fade_out.shape[0]:] *= fade_out
            else:
                envelope *= np.linspace(1, 0, envelope.shape[0])
            # Multiply by velocity (don't think it's linearly scaled but
            # whatever)
            envelope *= note.velocity
            # Add in envelope'd waveform to the synthesized signal
            synthesized[start:end] += envelope*note_waveform

        return synthesized

    def fluidsynth(self, fs=None, synthesizer=None, sfid=0, sf2_path=None):
        """Synthesize using fluidsynth.

        Parameters
        ----------
        fs : int
            Sampling rate to synthesize at.
            Default ``None``, which takes the sampling rate from ``synthesizer``, or
            uses ``pretty_midi.fluidsynth.DEFAULT_SAMPLE_RATE`` = 44100 if a synthesizer
            needs to be created.
        synthesizer : fluidsynth.Synth or str
            fluidsynth.Synth instance to use or a string with the path to a .sf2 file.
            Default ``None``, which creates a new instance using the TimGM6mb.sf2 file
            included with ``pretty_midi``.
        sfid : int
            Soundfont ID to use if an instance of fluidsynth.Synth is provided.
            Default ``0``, which uses the first soundfont.
        sf2_path : str
            Path to a .sf2 file.
            Default ``None``, which uses the TimGM6mb.sf2 file included with
            ``pretty_midi``.
            .. deprecated:: 0.2.11
                Use :param:`synthesizer` instead.

        Returns
        -------
        synthesized : np.ndarray
            Waveform of the MIDI data, synthesized at ``fs``.

        """
        if sf2_path is not None:
            warn("The parameter 'sf2_path' is deprecated, please use 'synthesizer' instead.",
                 DeprecationWarning, 2)
            if synthesizer is not None:
                raise ValueError("sf2_path and synthesizer cannot both be supplied.")
            else:
                synthesizer = sf2_path

        # If the instrument has no notes, return an empty array
        if len(self.notes) == 0:
            return np.array([])

        # Create a fluidsynth instance if one wasn't provided
        # Raises ValueError if sample rates do not match
        synthesizer, sfid, delete_synthesizer = get_fluidsynth_instance(synthesizer, sfid, fs)
        # Sample rate required below to compute note positions
        # Take from synthesizer in case fs was not passed explicitly
        fs = synthesizer.get_setting('synth.sample-rate')

        # If this is a drum instrument, use channel 9 and bank 128
        if self.is_drum:
            channel = 9
            # Try to use the supplied program number
            res = synthesizer.program_select(channel, sfid, 128, self.program)
            # If the result is -1, there's no preset with this program number
            if res == -1:
                # So use preset 0
                synthesizer.program_select(channel, sfid, 128, 0)
        # Otherwise just use channel 0
        else:
            channel = 0
            synthesizer.program_select(channel, sfid, 0, self.program)
        # Collect all notes in one list
        event_list = []
        for note in self.notes:
            event_list += [[note.start, 'note on', note.pitch, note.velocity]]
            event_list += [[note.end, 'note off', note.pitch]]
        for bend in self.pitch_bends:
            event_list += [[bend.time, 'pitch bend', bend.pitch]]
        for control_change in self.control_changes:
            event_list += [[control_change.time, 'control change',
                            control_change.number, control_change.value]]
        # Sort the event list by time, and secondarily by whether the event
        # is a note off
        event_list.sort(key=lambda x: (x[0], x[1] != 'note off'))
        # Add some silence at the beginning according to the time of the first
        # event
        current_time = event_list[0][0]
        # Convert absolute seconds to relative samples
        next_event_times = [e[0] for e in event_list[1:]]
        for event, end in zip(event_list[:-1], next_event_times):
            event[0] = end - event[0]
        # Include 1 second of silence at the end
        event_list[-1][0] = 1.
        # Pre-allocate output array
        total_time = current_time + np.sum([e[0] for e in event_list])
        synthesized = np.zeros(int(np.ceil(fs*total_time)))
        # Iterate over all events
        for event in event_list:
            # Process events based on type
            if event[1] == 'note on':
                synthesizer.noteon(channel, event[2], event[3])
            elif event[1] == 'note off':
                synthesizer.noteoff(channel, event[2])
            elif event[1] == 'pitch bend':
                synthesizer.pitch_bend(channel, event[2])
            elif event[1] == 'control change':
                synthesizer.cc(channel, event[2], event[3])
            # Add in these samples
            current_sample = int(fs*current_time)
            end = int(fs*(current_time + event[0]))
            samples = synthesizer.get_samples(end - current_sample)[::2]
            synthesized[current_sample:end] += samples
            # Increment the current sample
            current_time += event[0]
        # Close fluidsynth if it was a local instance created in the function
        if delete_synthesizer:
            synthesizer.delete()

        return synthesized

    def __repr__(self):
        return 'Instrument(program={}, is_drum={}, name="{}")'.format(
            self.program, self.is_drum, self.name.replace('"', r'\"'))
