"""Utility functions for handling MIDI data in an easy to read/manipulate
format

"""
from __future__ import print_function
from warnings import warn

import mido
import numpy as np
import math
import warnings
import collections
import copy
import functools
import six
import pathlib
from heapq import merge

from .instrument import Instrument
from .containers import (KeySignature, TimeSignature, Lyric, Note,
                         PitchBend, ControlChange, Text)
from .utilities import (key_name_to_key_number, qpm_to_bpm, note_number_to_hz)
from .fluidsynth import get_fluidsynth_instance

# The largest we'd ever expect a tick to be
MAX_TICK = 1e7


class PrettyMIDI(object):
    """A container for MIDI data in an easily-manipulable format.

    Parameters
    ----------
    midi_file : str or file
        Path or file pointer to a MIDI file.
        Default ``None`` would check if ``mido_object`` is populated instead. If both are ``None``,
        creates an empty class with the supplied values for resolution and initial tempo.
        Additionally, a ValueError is raised if both ``midi_file`` and ``mido_object`` are not ``None``.
    resolution : int
        Resolution of the MIDI data, when no file is provided.
    initial_tempo : float
        Initial tempo for the MIDI data, when no file is provided.
    charset : str
        Charset of the MIDI.
    mido_object : mido.MidiFile
        Pre-loaded `mido.MidiFile` object.
        Default ``None`` would check if ``midi_file`` is populated instead. If both are ``None``,
        creates an empty class with the supplied values for resolution and initial tempo.
        Additionally, a ValueError is raised if both ``mido_object`` and ``midi_file`` are not ``None``.

    Attributes
    ----------
    instruments : list
        List of :class:`pretty_midi.Instrument` objects.
    key_signature_changes : list
        List of :class:`pretty_midi.KeySignature` objects.
    time_signature_changes : list
        List of :class:`pretty_midi.TimeSignature` objects.
    lyrics : list
        List of :class:`pretty_midi.Lyric` objects.
    text_events : list
        List of :class:`pretty_midi.Text` objects.
    """

    def __init__(self, midi_file=None, resolution=220, initial_tempo=120., charset='latin1', mido_object=None):
        """Initialize either by populating it with MIDI data from a mido.MidiFile object, file or
        from scratch with no data.

        """
        if mido_object is not None or midi_file is not None:

            if mido_object is not None and midi_file is not None:
                raise ValueError("Either the midi_file or the mido_object argument must be provided, but not both.")

            if mido_object is not None:
                if isinstance(mido_object, mido.MidiFile):
                    midi_data = mido_object
                else:
                    raise ValueError("Expected mido_object to be of type mido.MidiFile.")

            if midi_file is not None:
                # Load in the MIDI data using the midi module
                if isinstance(midi_file, six.string_types) or isinstance(midi_file, pathlib.PurePath):
                    # If a string or path was given, pass it as the filename
                    midi_data = mido.MidiFile(filename=midi_file, charset=charset)
                else:
                    # Otherwise, try passing it in as a file pointer
                    midi_data = mido.MidiFile(file=midi_file, charset=charset)

            # Convert tick values in midi_data to absolute, a useful thing.
            for track in midi_data.tracks:
                tick = 0
                for event in track:
                    event.time += tick
                    tick = event.time

            # Store the resolution for later use
            self.resolution = midi_data.ticks_per_beat

            # Update the array which maps ticks to time
            max_tick = max([max([e.time for e in t])
                            for t in midi_data.tracks]) + 1
            # If max_tick is huge, the MIDI file is probably corrupt
            # and creating the __tick_to_time array will thrash memory
            if max_tick > MAX_TICK:
                raise ValueError(('MIDI file has a largest tick of {},'
                                  ' it is likely corrupt'.format(max_tick)))

            # Populate the list of tempo changes (tick scales)
            self._load_tempo_changes(midi_data)
          
            # Create list that maps ticks to time in seconds
            self._update_tick_to_time(max_tick)

            # Populate the list of key and time signature changes
            self._load_metadata(midi_data)

            # Check that there are tempo, key and time change events
            # only on track 0
            if any(e.type in ('set_tempo', 'key_signature', 'time_signature')
                   for track in midi_data.tracks[1:] for e in track):
                warnings.warn(
                    "Tempo, Key or Time signature change events found on "
                    "non-zero tracks.  This is not a valid type 0 or type 1 "
                    "MIDI file.  Tempo, Key or Time Signature may be wrong.",
                    RuntimeWarning)

            # Populate the list of instruments
            self._load_instruments(midi_data)
            # MIDI Charset
            self._charset = charset
        else:
            self.resolution = resolution
            # Compute the tick scale for the provided initial tempo
            # and let the tick scale start from 0
            self._tick_scales = [(0, 60.0/(initial_tempo*self.resolution))]
            # Only need to convert one tick to time
            self.__tick_to_time = [0]
            # Empty instruments list
            self.instruments = []
            # Empty key signature changes list
            self.key_signature_changes = []
            # Empty time signatures changes list
            self.time_signature_changes = []
            # Empty lyrics list
            self.lyrics = []
            # Empty text events list
            self.text_events = []
            # MIDI Charset
            self._charset = charset

    def _load_tempo_changes(self, midi_data):
        """Populates ``self._tick_scales`` with tuples of
        ``(tick, tick_scale)`` loaded from ``midi_data``.

        Parameters
        ----------
        midi_data : midi.FileReader
            MIDI object from which data will be read.
        """

        # MIDI data is given in "ticks".
        # We need to convert this to clock seconds.
        # The conversion factor involves the BPM, which may change over time.
        # So, create a list of tuples, (time, tempo)
        # denoting a tempo change at a certain time.
        # By default, set the tempo to 120 bpm, starting at time 0
        self._tick_scales = [(0, 60.0/(120.0*self.resolution))]
        # For SMF file type 0, all events are on track 0.
        # For type 1, all tempo events should be on track 1.
        # Everyone ignores type 2.
        # So, just look at events on track 0
        for event in midi_data.tracks[0]:
            if event.type == 'set_tempo':
                # Only allow one tempo change event at the beginning
                if event.time == 0:
                    bpm = 6e7/event.tempo
                    self._tick_scales = [(0, 60.0/(bpm*self.resolution))]
                else:
                    # Get time and BPM up to this point
                    _, last_tick_scale = self._tick_scales[-1]
                    tick_scale = 60.0/((6e7/event.tempo)*self.resolution)
                    # Ignore repetition of BPM, which happens often
                    if tick_scale != last_tick_scale:
                        self._tick_scales.append((event.time, tick_scale))

    def _load_metadata(self, midi_data):
        """Populates ``self.time_signature_changes`` with ``TimeSignature``
        objects, ``self.key_signature_changes`` with ``KeySignature`` objects,
        ``self.lyrics`` with ``Lyric`` objects and ``self.text_events`` with
        ``Text`` objects.

        Parameters
        ----------
        midi_data : midi.FileReader
            MIDI object from which data will be read.
        """

        # Initialize empty lists for storing key signature changes, time
        # signature changes, and lyrics
        self.key_signature_changes = []
        self.time_signature_changes = []
        self.lyrics = []
        self.text_events = []

        for event in midi_data.tracks[0]:
            if event.type == 'key_signature':
                key_obj = KeySignature(
                    key_name_to_key_number(event.key),
                    self.__tick_to_time[event.time])
                self.key_signature_changes.append(key_obj)

            elif event.type == 'time_signature':
                ts_obj = TimeSignature(event.numerator,
                                       event.denominator,
                                       self.__tick_to_time[event.time])
                self.time_signature_changes.append(ts_obj)

        # We search for lyrics and text events on all tracks
        # Lists of lyrics and text events lists, for every track
        tracks_with_lyrics = []
        tracks_with_text_events = []
        for track in midi_data.tracks:
            # Track specific lists that get appended if not empty
            lyrics = []
            text_events = []
            for event in track:
                if event.type == 'lyrics':
                    lyrics.append(Lyric(
                        event.text, self.__tick_to_time[event.time]))
                elif event.type == 'text':
                    text_events.append(Text(
                        event.text, self.__tick_to_time[event.time]))

            if lyrics:
                tracks_with_lyrics.append(lyrics)
            if text_events:
                tracks_with_text_events.append(text_events)

        # We merge the already sorted lists for every track, based on time
        self.lyrics = list(merge(*tracks_with_lyrics, key=lambda x: x.time))
        self.text_events = list(merge(*tracks_with_text_events, key=lambda x: x.time))


    def _update_tick_to_time(self, max_tick):
        """Creates ``self.__tick_to_time``, a class member array which maps
        ticks to time starting from tick 0 and ending at ``max_tick``.

        Parameters
        ----------
        max_tick : int
            Last tick to compute time for.  If ``self._tick_scales`` contains a
            tick which is larger than this value, it will be used instead.

        """
        # If max_tick is smaller than the largest tick in self._tick_scales,
        # use this largest tick instead
        max_scale_tick = max(ts[0] for ts in self._tick_scales)
        max_tick = max_tick if max_tick > max_scale_tick else max_scale_tick
        # Allocate tick to time array - indexed by tick from 0 to max_tick
        self.__tick_to_time = np.zeros(max_tick + 1)
        # Keep track of the end time of the last tick in the previous interval
        last_end_time = 0
        # Cycle through intervals of different tempi
        for (start_tick, tick_scale), (end_tick, _) in \
                zip(self._tick_scales[:-1], self._tick_scales[1:]):
            # Convert ticks in this interval to times
            ticks = np.arange(end_tick - start_tick + 1)
            self.__tick_to_time[start_tick:end_tick + 1] = (last_end_time +
                                                            tick_scale*ticks)
            # Update the time of the last tick in this interval
            last_end_time = self.__tick_to_time[end_tick]
        # For the final interval, use the final tempo setting
        # and ticks from the final tempo setting until max_tick
        start_tick, tick_scale = self._tick_scales[-1]
        ticks = np.arange(max_tick + 1 - start_tick)
        self.__tick_to_time[start_tick:] = (last_end_time +
                                            tick_scale*ticks)

    def _load_instruments(self, midi_data):
        """Populates ``self.instruments`` using ``midi_data``.

        Parameters
        ----------
        midi_data : midi.FileReader
            MIDI object from which data will be read.
        """
        # MIDI files can contain a collection of tracks; each track can have
        # events occuring on one of sixteen channels, and events can correspond
        # to different instruments according to the most recently occurring
        # program number.  So, we need a way to keep track of which instrument
        # is playing on each track on each channel.  This dict will map from
        # program number, drum/not drum, channel, and track index to instrument
        # indices, which we will retrieve/populate using the __get_instrument
        # function below.
        instrument_map = collections.OrderedDict()
        # Store a similar mapping to instruments storing "straggler events",
        # e.g. events which appear before we want to initialize an Instrument
        stragglers = {}
        # This dict will map track indices to any track names encountered
        track_name_map = collections.defaultdict(str)

        def __get_instrument(program, channel, track, create_new):
            """Gets the Instrument corresponding to the given program number,
            drum/non-drum type, channel, and track index.  If no such
            instrument exists, one is created.

            """
            # If we have already created an instrument for this program
            # number/track/channel, return it
            if (program, channel, track) in instrument_map:
                return instrument_map[(program, channel, track)]
            # If there's a straggler instrument for this instrument and we
            # aren't being requested to create a new instrument
            if not create_new and (channel, track) in stragglers:
                return stragglers[(channel, track)]
            # If we are told to, create a new instrument and store it
            if create_new:
                is_drum = (channel == 9)
                instrument = Instrument(
                    program, is_drum, track_name_map[track_idx])
                # If any events appeared for this instrument before now,
                # include them in the new instrument
                if (channel, track) in stragglers:
                    straggler = stragglers[(channel, track)]
                    instrument.control_changes = straggler.control_changes
                    instrument.pitch_bends = straggler.pitch_bends
                # Add the instrument to the instrument map
                instrument_map[(program, channel, track)] = instrument
            # Otherwise, create a "straggler" instrument which holds events
            # which appear before we actually want to create a proper new
            # instrument
            else:
                # Create a "straggler" instrument
                instrument = Instrument(program, track_name_map[track_idx])
                # Note that stragglers ignores program number, because we want
                # to store all events on a track which appear before the first
                # note-on, regardless of program
                stragglers[(channel, track)] = instrument
            return instrument

        for track_idx, track in enumerate(midi_data.tracks):
            # Keep track of last note on location:
            # key = (instrument, note),
            # value = (note-on tick, velocity)
            last_note_on = collections.defaultdict(list)
            # Keep track of which instrument is playing in each channel
            # initialize to program 0 for all channels
            current_instrument = np.zeros(16, dtype=int)
            for event in track:
                # Look for track name events
                if event.type == 'track_name':
                    # Set the track name for the current track
                    track_name_map[track_idx] = event.name
                # Look for program change events
                if event.type == 'program_change':
                    # Update the instrument for this channel
                    current_instrument[event.channel] = event.program
                # Note ons are note on events with velocity > 0
                elif event.type == 'note_on' and event.velocity > 0:
                    # Store this as the last note-on location
                    note_on_index = (event.channel, event.note)
                    last_note_on[note_on_index].append((
                        event.time, event.velocity))
                # Note offs can also be note on events with 0 velocity
                elif event.type == 'note_off' or (event.type == 'note_on' and
                                                  event.velocity == 0):
                    # Check that a note-on exists (ignore spurious note-offs)
                    key = (event.channel, event.note)
                    if key in last_note_on:
                        # Get the start/stop times and velocity of every note
                        # which was turned on with this instrument/drum/pitch.
                        # One note-off may close multiple note-on events from
                        # previous ticks. In case there's a note-off and then
                        # note-on at the same tick we keep the open note from
                        # this tick.
                        end_tick = event.time
                        open_notes = last_note_on[key]

                        notes_to_close = [
                            (start_tick, velocity)
                            for start_tick, velocity in open_notes
                            if start_tick != end_tick]
                        notes_to_keep = [
                            (start_tick, velocity)
                            for start_tick, velocity in open_notes
                            if start_tick == end_tick]

                        for start_tick, velocity in notes_to_close:
                            start_time = self.__tick_to_time[start_tick]
                            end_time = self.__tick_to_time[end_tick]
                            # Create the note event
                            note = Note(velocity, event.note, start_time,
                                        end_time)
                            # Get the program and drum type for the current
                            # instrument
                            program = current_instrument[event.channel]
                            # Retrieve the Instrument instance for the current
                            # instrument
                            # Create a new instrument if none exists
                            instrument = __get_instrument(
                                program, event.channel, track_idx, 1)
                            # Add the note event
                            instrument.notes.append(note)

                        if len(notes_to_close) > 0 and len(notes_to_keep) > 0:
                            # Note-on on the same tick but we already closed
                            # some previous notes -> it will continue, keep it.
                            last_note_on[key] = notes_to_keep
                        else:
                            # Remove the last note on for this instrument
                            del last_note_on[key]
                # Store pitch bends
                elif event.type == 'pitchwheel':
                    # Create pitch bend class instance
                    bend = PitchBend(event.pitch,
                                     self.__tick_to_time[event.time])
                    # Get the program for the current inst
                    program = current_instrument[event.channel]
                    # Retrieve the Instrument instance for the current inst
                    # Don't create a new instrument if none exists
                    instrument = __get_instrument(
                        program, event.channel, track_idx, 0)
                    # Add the pitch bend event
                    instrument.pitch_bends.append(bend)
                # Store control changes
                elif event.type == 'control_change':
                    control_change = ControlChange(
                        event.control, event.value,
                        self.__tick_to_time[event.time])
                    # Get the program for the current inst
                    program = current_instrument[event.channel]
                    # Retrieve the Instrument instance for the current inst
                    # Don't create a new instrument if none exists
                    instrument = __get_instrument(
                        program, event.channel, track_idx, 0)
                    # Add the control change event
                    instrument.control_changes.append(control_change)
        # Initialize list of instruments from instrument_map
        self.instruments = [i for i in instrument_map.values()]

    def get_tempo_changes(self):
        """Return arrays of tempo changes in quarter notes-per-minute and their
        times.

        Returns
        -------
        tempo_change_times : np.ndarray
            Times, in seconds, where the tempo changes.
        tempi : np.ndarray
            What the tempo is, in quarter notes-per-minute, at each point in
            time in ``tempo_change_times``.

        """

        # Pre-allocate return arrays
        tempo_change_times = np.zeros(len(self._tick_scales))
        tempi = np.zeros(len(self._tick_scales))
        for n, (tick, tick_scale) in enumerate(self._tick_scales):
            # Convert tick of this tempo change to time in seconds
            tempo_change_times[n] = self.tick_to_time(tick)
            # Convert tick scale to a tempo
            tempi[n] = 60.0/(tick_scale*self.resolution)
        return tempo_change_times, tempi

    def get_end_time(self):
        """Returns the time of the end of the MIDI object (time of the last
        event in all instruments/meta-events).

        Returns
        -------
        end_time : float
            Time, in seconds, where this MIDI file ends.

        """
        # Get end times from all instruments, and times of all meta-events
        meta_events = [self.time_signature_changes, self.key_signature_changes,
                       self.lyrics, self.text_events]
        times = ([i.get_end_time() for i in self.instruments] +
                 [e.time for m in meta_events for e in m] +
                 self.get_tempo_changes()[0].tolist())
        # If there are no events, return 0
        if len(times) == 0:
            return 0.
        else:
            return max(times)

    def estimate_tempi(self):
        """Return an empirical estimate of tempos and each tempo's probability.
        Based on "Automatic Extraction of Tempo and Beat from Expressive
        Performance", Dixon 2001.

        Returns
        -------
        tempos : np.ndarray
            Array of estimated tempos, in beats per minute.
        probabilities : np.ndarray
            Array of the probabilities of each tempo estimate.

        """
        # Grab the list of onsets
        onsets = self.get_onsets()
        # Compute inner-onset intervals
        ioi = np.diff(onsets)
        # "Rhythmic information is provided by IOIs in the range of
        # approximately 50ms to 2s (Handel, 1989)"
        ioi = ioi[ioi > .05]
        ioi = ioi[ioi < 2]
        # Normalize all iois into the range 30...300bpm
        for n in range(ioi.shape[0]):
            while ioi[n] < .2:
                ioi[n] *= 2
        # Array of inner onset interval cluster means
        clusters = np.array([])
        # Number of iois in each cluster
        cluster_counts = np.array([])
        for interval in ioi:
            # If this ioi falls within a cluster (threshold is 25ms)
            if (np.abs(clusters - interval) < .025).any():
                k = np.argmin(clusters - interval)
                # Update cluster mean
                clusters[k] = (cluster_counts[k]*clusters[k] +
                               interval)/(cluster_counts[k] + 1)
                # Update number of elements in cluster
                cluster_counts[k] += 1
            # No cluster is close, make a new one
            else:
                clusters = np.append(clusters, interval)
                cluster_counts = np.append(cluster_counts, 1.)
        # Sort the cluster list by count
        cluster_sort = np.argsort(cluster_counts)[::-1]
        clusters = clusters[cluster_sort]
        cluster_counts = cluster_counts[cluster_sort]
        # Normalize the cluster scores
        cluster_counts /= cluster_counts.sum()
        return 60./clusters, cluster_counts

    def estimate_tempo(self):
        """Returns the best tempo estimate from
        :func:`pretty_midi.PrettyMIDI.estimate_tempi()`, for convenience.

        Returns
        -------
        tempo : float
            Estimated tempo, in bpm

        """
        tempi = self.estimate_tempi()[0]
        if tempi.size == 0:
            raise ValueError("Can't provide a global tempo estimate when there"
                             " are fewer than two notes.")
        return tempi[0]

    def get_beats(self, start_time=0.):
        """Returns a list of beat locations, according to MIDI tempo changes.
        For compound meters (any whose numerator is a multiple of 3 greater
        than 3), this method returns every third denominator note (for 6/8
        or 6/16 time, for example, it will return every third 8th note or
        16th note, respectively). For all other meters, this method returns
        every denominator note (every quarter note for 3/4 or 4/4 time, for
        example).

        Parameters
        ----------
        start_time : float
            Location of the first beat, in seconds.

        Returns
        -------
        beats : np.ndarray
            Beat locations, in seconds.

        """
        # Get tempo changes and tempos
        tempo_change_times, tempi = self.get_tempo_changes()
        # Create beat list; first beat is at first onset
        beats = [start_time]
        # Index of the tempo we're using
        tempo_idx = 0
        # Move past all the tempo changes up to the supplied start time
        while (tempo_idx < tempo_change_times.shape[0] - 1 and
                beats[-1] > tempo_change_times[tempo_idx + 1]):
            tempo_idx += 1
        # Logic requires that time signature changes are sorted by time
        self.time_signature_changes.sort(key=lambda ts: ts.time)
        # Index of the time signature change we're using
        ts_idx = 0
        # Move past all time signature changes up to the supplied start time
        while (ts_idx < len(self.time_signature_changes) - 1 and
                beats[-1] >= self.time_signature_changes[ts_idx + 1].time):
            ts_idx += 1

        def get_current_bpm():
            ''' Convenience function which computs the current BPM based on the
            current tempo change and time signature events '''
            # When there are time signature changes, use them to compute BPM
            if self.time_signature_changes:
                return qpm_to_bpm(
                    tempi[tempo_idx],
                    self.time_signature_changes[ts_idx].numerator,
                    self.time_signature_changes[ts_idx].denominator)
            # Otherwise, just use the raw tempo change event tempo
            else:
                return tempi[tempo_idx]

        def gt_or_close(a, b):
            ''' Returns True if a > b or a is close to b '''
            return a > b or np.isclose(a, b)

        # Get track end time
        end_time = self.get_end_time()
        # Add beats in
        while beats[-1] < end_time:
            # Update the current bpm
            bpm = get_current_bpm()
            # Compute expected beat location, one period later
            next_beat = beats[-1] + 60.0/bpm
            # If the beat location passes a tempo change boundary...
            if (tempo_idx < tempo_change_times.shape[0] - 1 and
                    next_beat > tempo_change_times[tempo_idx + 1]):
                # Start by setting the beat location to the current beat...
                next_beat = beats[-1]
                # with the entire beat remaining
                beat_remaining = 1.0
                # While a beat with the current tempo would pass a tempo
                # change boundary...
                while (tempo_idx < tempo_change_times.shape[0] - 1 and
                        next_beat + beat_remaining*60.0/bpm >=
                        tempo_change_times[tempo_idx + 1]):
                    # Compute the amount the beat location overshoots
                    overshot_ratio = (tempo_change_times[tempo_idx + 1] -
                                      next_beat)/(60.0/bpm)
                    # Add in the amount of the beat during this tempo
                    next_beat += overshot_ratio*60.0/bpm
                    # Less of the beat remains now
                    beat_remaining -= overshot_ratio
                    # Increment the tempo index
                    tempo_idx = tempo_idx + 1
                    # Update the current bpm
                    bpm = get_current_bpm()
                # Add in the remainder of the beat at the current tempo
                next_beat += beat_remaining*60./bpm
            # Check if we have just passed the first time signature change
            if self.time_signature_changes and ts_idx == 0:
                current_ts_time = self.time_signature_changes[ts_idx].time
                if (current_ts_time > beats[-1] and
                        gt_or_close(next_beat, current_ts_time)):
                    # Set the next beat to the time signature change time
                    next_beat = current_ts_time
            # If the next beat location passes the next time signature change
            # boundary
            if ts_idx < len(self.time_signature_changes) - 1:
                # Time of the next time signature change
                next_ts_time = self.time_signature_changes[ts_idx + 1].time
                if gt_or_close(next_beat, next_ts_time):
                    # Set the next beat to the time signature change time
                    next_beat = next_ts_time
                    # Update the time signature index
                    ts_idx += 1
                    # Update the current bpm
                    bpm = get_current_bpm()
            beats.append(next_beat)
        # The last beat will pass the end_time barrier, so don't include it
        beats = np.array(beats[:-1])
        return beats

    def estimate_beat_start(self, candidates=10, tolerance=.025):
        """Estimate the location of the first beat based on which of the first
        few onsets results in the best correlation with the onset spike train.

        Parameters
        ----------
        candidates : int
            Number of candidate onsets to try.
        tolerance : float
            The tolerance in seconds around which onsets will be used to
            treat a beat as correct.

        Returns
        -------
        beat_start : float
            The offset which is chosen as the beat start location.
        """
        # Get a sorted list of all notes from all instruments
        note_list = [n for i in self.instruments for n in i.notes]
        if not note_list:
            raise ValueError(
                "Can't estimate beat start when there are no notes.")
        note_list.sort(key=lambda note: note.start)
        # List of possible beat trackings
        beat_candidates = []
        # List of start times for each beat candidate
        start_times = []
        onset_index = 0
        # Try the first 10 (unique) onsets as beat tracking start locations
        while (len(beat_candidates) <= candidates and
               len(beat_candidates) <= len(note_list) and
               onset_index < len(note_list)):
            # Make sure we are using a new start location
            if onset_index == 0 or np.abs(note_list[onset_index - 1].start -
                                          note_list[onset_index].start) > .001:
                beat_candidates.append(
                    self.get_beats(note_list[onset_index].start))
                start_times.append(note_list[onset_index].start)
            onset_index += 1
        # Compute onset scores
        onset_scores = np.zeros(len(beat_candidates))
        # Synthesize note onset signal, with velocity-valued spikes at onsets
        fs = 1000
        onset_signal = np.zeros(int(fs*(self.get_end_time() + 1)))
        for note in note_list:
            onset_signal[int(note.start*fs)] += note.velocity
        for n, beats in enumerate(beat_candidates):
            # Create a synthetic beat signal with 25ms windows
            beat_signal = np.zeros(int(fs*(self.get_end_time() + 1)))
            for beat in np.append(0, beats):
                if beat - tolerance < 0:
                    beat_window = np.ones(
                        int(fs*2*tolerance + (beat - tolerance)*fs))
                    beat_signal[:int((beat + tolerance)*fs)] = beat_window
                else:
                    beat_start = int((beat - tolerance)*fs)
                    beat_end = beat_start + int(fs*tolerance*2)
                    beat_window = np.ones(int(fs*tolerance*2))
                    beat_signal[beat_start:beat_end] = beat_window
            # Compute their dot product and normalize to get score
            onset_scores[n] = np.dot(beat_signal, onset_signal)/beats.shape[0]
        # Return the best-scoring beat start
        return start_times[np.argmax(onset_scores)]

    def get_downbeats(self, start_time=0.):
        """Return a list of downbeat locations, according to MIDI tempo changes
        and time signature change events.

        Parameters
        ----------
        start_time : float
            Location of the first downbeat, in seconds.

        Returns
        -------
        downbeats : np.ndarray
            Downbeat locations, in seconds.

        """
        # Get beat locations
        beats = self.get_beats(start_time)
        # Make a copy of time signatures as we will be manipulating it
        time_signatures = copy.deepcopy(self.time_signature_changes)

        # If there are no time signatures or they start after 0s, add a 4/4
        # signature at time 0
        if not time_signatures or time_signatures[0].time > start_time:
            time_signatures.insert(0, TimeSignature(4, 4, start_time))

        def index(array, value, default):
            """ Returns the first index of a value in an array, or `default` if
            the value doesn't appear in the array."""
            idx = np.flatnonzero(np.isclose(array, value))
            if idx.size > 0:
                return idx[0]
            else:
                return default

        downbeats = []
        end_beat_idx = 0
        # Iterate over spans of time signatures
        for start_ts, end_ts in zip(time_signatures[:-1], time_signatures[1:]):
            # Get index of first beat at start_ts.time, or else use first beat
            start_beat_idx = index(beats, start_ts.time, 0)
            # Get index of first beat at end_ts.time, or else use last beat
            end_beat_idx = index(beats, end_ts.time, start_beat_idx)
            # Add beats within this time signature range, skipping beats
            # according to the current time signature
            if start_ts.numerator % 3 == 0 and start_ts.numerator != 3:
                downbeats.append(beats[
                    start_beat_idx:end_beat_idx:(start_ts.numerator // 3)])
            else:
                downbeats.append(beats[
                    start_beat_idx:end_beat_idx:start_ts.numerator])
        # Add in beats from the second-to-last to last time signature
        final_ts = time_signatures[-1]
        start_beat_idx = index(beats, final_ts.time, end_beat_idx)
        if final_ts.numerator % 3 == 0 and final_ts.numerator != 3:
            downbeats.append(beats[start_beat_idx::(final_ts.numerator // 3)])
        else:
            downbeats.append(beats[start_beat_idx::final_ts.numerator])
        # Convert from list to array
        downbeats = np.concatenate(downbeats)
        # Return all downbeats after start_time
        return downbeats[downbeats >= start_time]

    def get_onsets(self):
        """Return a sorted list of the times of all onsets of all notes from
        all instruments.  May have duplicate entries.

        Returns
        -------
        onsets : np.ndarray
            Onset locations, in seconds.

        """
        onsets = np.array([])
        # Just concatenate onsets from all the instruments
        for instrument in self.instruments:
            onsets = np.append(onsets, instrument.get_onsets())
        # Return them sorted (because why not?)
        return np.sort(onsets)

    def get_piano_roll(self, fs=100, times=None, pedal_threshold=64):
        """Compute a piano roll matrix of the MIDI data.

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
            Piano roll of MIDI data, flattened across instruments.

        """

        # If there are no instruments, return an empty array
        if len(self.instruments) == 0:
            return np.zeros((128, 0))

        # Get piano rolls for each instrument
        piano_rolls = [i.get_piano_roll(fs=fs, times=times,
                                        pedal_threshold=pedal_threshold)
                       for i in self.instruments]
        # Allocate piano roll,
        # number of columns is max of # of columns in all piano rolls
        piano_roll = np.zeros((128, np.max([p.shape[1] for p in piano_rolls])))
        # Sum each piano roll into the aggregate piano roll
        for roll in piano_rolls:
            piano_roll[:, :roll.shape[1]] += roll
        return piano_roll

    def get_intervals_and_pitches(self):
        notes = [n for i in self.instruments for n in i.notes]
        notes = sorted(notes, key=lambda n: n.start)
        intervals = np.array([[n.start, n.end] for n in notes])
        pitches = np.array([note_number_to_hz(n.pitch) for n in notes])
        return intervals, pitches

    def get_pitch_class_histogram(self, use_duration=False,
                                  use_velocity=False, normalize=True):
        """Computes the histogram of pitch classes.

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
            Histogram of pitch classes given all tracks, optionally weighted
            by their durations or velocities.
        """
        # Sum up all histograms from all instruments defaulting to np.zeros(12)
        histogram = sum([
            i.get_pitch_class_histogram(use_duration, use_velocity)
            for i in self.instruments], np.zeros(12))

        # Normalize accordingly
        if normalize:
            histogram /= (histogram.sum() + (histogram.sum() == 0))

        return histogram

    def get_pitch_class_transition_matrix(self, normalize=False,
                                          time_thresh=0.05):
        """Computes the total pitch class transition matrix of all instruments.
        Transitions are added whenever the end of a note is within
        ``time_thresh`` from the start of any other note.

        Parameters
        ----------
        normalize : bool
            Normalize transition matrix such that matrix sum equals is 1.
        time_thresh : float
            Maximum temporal threshold, in seconds, between the start of a note
            and end time of any other note for a transition to be added.

        Returns
        -------
        pitch_class_transition_matrix : np.ndarray, shape=(12,12)
            Pitch class transition matrix.
        """
        # Sum up all matrices from all instruments defaulting zeros matrix
        pc_trans_mat = sum(
            [i.get_pitch_class_transition_matrix(normalize, time_thresh)
             for i in self.instruments], np.zeros((12, 12)))

        # Normalize accordingly
        if normalize:
            pc_trans_mat /= (pc_trans_mat.sum() + (pc_trans_mat.sum() == 0))

        return pc_trans_mat

    def get_chroma(self, fs=100, times=None, pedal_threshold=64):
        """Get the MIDI data as a sequence of chroma vectors.

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
            Chromagram of MIDI data, flattened across instruments.

        """
        # First, get the piano roll
        piano_roll = self.get_piano_roll(fs=fs, times=times,
                                         pedal_threshold=pedal_threshold)
        # Fold into one octave
        chroma_matrix = np.zeros((12, piano_roll.shape[1]))
        for note in range(12):
            chroma_matrix[note, :] = np.sum(piano_roll[note::12], axis=0)
        return chroma_matrix

    def synthesize(self, fs=44100, wave=np.sin, normalize=True):
        """Synthesize the pattern using some waveshape.  Ignores drum track.

        Parameters
        ----------
        fs : int
            Sampling rate of the synthesized audio signal.
        wave : function
            Function which returns a periodic waveform,
            e.g. ``np.sin``, ``scipy.signal.square``, etc.
        normalize : bool
            Default ``True``, which normalizes the output wave to the range [-1, 1].
            Otherwise, divides the output by the maximum value of a 16-bit integer 
            multiplied by the number of instruments, and lets the user handle accordingly.

        Returns
        -------
        synthesized : np.ndarray
            Waveform of the MIDI data, synthesized at ``fs``.

        """
        # If there are no instruments, return an empty array
        if len(self.instruments) == 0:
            return np.array([])
        # Get synthesized waveform for each instrument
        waveforms = [i.synthesize(fs=fs, wave=wave) for i in self.instruments]
        # Allocate output waveform, with #sample = max length of all waveforms
        synthesized = np.zeros(np.max([w.shape[0] for w in waveforms]))
        # Sum all waveforms in
        for waveform in waveforms:
            synthesized[:waveform.shape[0]] += waveform

        if normalize:
            # Hard normalize to [-1, 1]
            synthesized /= np.abs(synthesized).max()
        else:
            # Normalize by the maximum absolute value of a 16-bit integer
            # to prevent clipping
            synthesized /= (len(self.instruments) * 2 ** 15)

        return synthesized

    def fluidsynth(self, fs=None, synthesizer=None, sfid=0, sf2_path=None, 
                   normalize=True):
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
        normalize : bool
            Default ``True``, which normalizes the output wave to the range [-1, 1].
            Otherwise, divides the output by the maximum value of a 16-bit integer 
            multiplied by the number of instruments, and lets the user handle accordingly.

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

        # If there are no instruments, or all instruments have no notes, return
        # an empty array
        if len(self.instruments) == 0 or all(len(i.notes) == 0
                                             for i in self.instruments):
            return np.array([])

        # Create a fluidsynth instance if one wasn't provided
        synthesizer, sfid, delete_synthesizer = get_fluidsynth_instance(synthesizer, sfid, fs)

        # Get synthesized waveform for each instrument
        waveforms = [i.fluidsynth(synthesizer=synthesizer, sfid=sfid)
                     for i in self.instruments]

        # Close fluidsynth if it was a local instance created in the function
        if delete_synthesizer:
            synthesizer.delete()

        # Allocate output waveform, with #sample = max length of all waveforms
        synthesized = np.zeros(np.max([w.shape[0] for w in waveforms]))
        # Sum all waveforms in
        for waveform in waveforms:
            synthesized[:waveform.shape[0]] += waveform

        if normalize:
            # Hard normalize to [-1, 1]
            synthesized /= np.abs(synthesized).max()
        else:
            # Normalize by the maximum absolute value of a 16-bit integer
            # to prevent clipping
            synthesized /= (len(self.instruments) * 2 ** 15)
        
        return synthesized

    def tick_to_time(self, tick):
        """Converts from an absolute tick to time in seconds using
        ``self.__tick_to_time``.

        Parameters
        ----------
        tick : int
            Absolute tick to convert.

        Returns
        -------
        time : float
            Time in seconds of tick.

        """
        # Check that the tick isn't too big
        if tick >= MAX_TICK:
            raise IndexError('Supplied tick is too large.')
        # If we haven't compute the mapping for a tick this large, compute it
        if tick >= len(self.__tick_to_time):
            self._update_tick_to_time(tick)
        # Ticks should be integers
        if not isinstance(tick, int):
            warnings.warn('tick should be an int.')
        # Otherwise just return the time
        return self.__tick_to_time[int(tick)]

    def time_to_tick(self, time):
        """Converts from a time in seconds to absolute tick using
        ``self._tick_scales``.

        Parameters
        ----------
        time : float
            Time, in seconds.

        Returns
        -------
        tick : int
            Absolute tick corresponding to the supplied time.

        """
        # Find the index of the ticktime which is smaller than time
        tick = np.searchsorted(self.__tick_to_time, time, side="left")
        # If the closest tick was the final tick in self.__tick_to_time...
        if tick == len(self.__tick_to_time):
            # start from time at end of __tick_to_time
            tick -= 1
            # Add on ticks assuming the final tick_scale amount
            _, final_tick_scale = self._tick_scales[-1]
            tick += (time - self.__tick_to_time[tick])/final_tick_scale
            # Re-round/quantize
            return int(round(tick))
        # If the tick is not 0 and the previous ticktime in a is closer to time
        if tick and (math.fabs(time - self.__tick_to_time[tick - 1]) <
                     math.fabs(time - self.__tick_to_time[tick])):
            # Decrement index by 1
            return tick - 1
        else:
            return tick

    def adjust_times(self, original_times, new_times):
        """Adjusts the timing of the events in the MIDI object.
        The parameters ``original_times`` and ``new_times`` define a mapping,
        so that if an event originally occurs at time ``original_times[n]``, it
        will be moved so that it occurs at ``new_times[n]``.  If events don't
        occur exactly on a time in ``original_times``, their timing will be
        linearly interpolated.

        Parameters
        ----------
        original_times : np.ndarray
            Times to map from.
        new_times : np.ndarray
            New times to map to.

        """
        # Get original downbeat locations (we will use them to determine where
        # to put the first time signature change)
        original_downbeats = self.get_downbeats()
        # Force strict increase in original_times and monotonic in new_times.
        # While enforcing, give warning.
        original_size = len(original_times)
        original_times, unique_idx = np.unique(original_times,
                                               return_index=True)
        if ((unique_idx.size != original_size) or
                any(unique_idx != np.arange(unique_idx.size))):
            warnings.warn('original_times must be strictly increasing; '
                          'automatically enforcing this.')
        new_times = np.asarray(new_times)[unique_idx]
        if not np.all(np.diff(new_times) >= 0):
            warnings.warn('new_times must be monotonic; '
                          'automatically enforcing this.')
            new_times = np.maximum.accumulate(new_times)
        # Only include notes within start/end time of the provided times
        for instrument in self.instruments:
            instrument.notes = [copy.deepcopy(note)
                                for note in instrument.notes
                                if note.start >= original_times[0] and
                                note.end <= original_times[-1]]
        # Get array of note-on locations and correct them
        note_ons = np.array([note.start for instrument in self.instruments
                             for note in instrument.notes])
        adjusted_note_ons = np.interp(note_ons, original_times, new_times)
        # Same for note-offs
        note_offs = np.array([note.end for instrument in self.instruments
                              for note in instrument.notes])
        adjusted_note_offs = np.interp(note_offs, original_times, new_times)
        # Correct notes
        for n, note in enumerate([note for instrument in self.instruments
                                  for note in instrument.notes]):
            note.start = (adjusted_note_ons[n] > 0)*adjusted_note_ons[n]
            note.end = (adjusted_note_offs[n] > 0)*adjusted_note_offs[n]
        # After performing alignment, some notes may have an end time which is
        # on or before the start time.  Remove these!
        self.remove_invalid_notes()

        def adjust_events(event_getter):
            """ This function calls event_getter with each instrument as the
            sole argument and adjusts the events which are returned."""
            # Sort the events by time
            for instrument in self.instruments:
                event_getter(instrument).sort(key=lambda e: e.time)
            # Correct the events by interpolating
            event_times = np.array(
                [event.time for instrument in self.instruments
                 for event in event_getter(instrument)])
            adjusted_event_times = np.interp(
                event_times, original_times, new_times)
            for n, event in enumerate([event for instrument in self.instruments
                                       for event in event_getter(instrument)]):
                event.time = adjusted_event_times[n]
            for instrument in self.instruments:
                # We want to keep only the final event which has time ==
                # new_times[0]
                valid_events = [event for event in event_getter(instrument)
                                if event.time == new_times[0]]
                if valid_events:
                    valid_events = valid_events[-1:]
                # Otherwise only keep events within the new set of times
                valid_events.extend(
                    event for event in event_getter(instrument)
                    if event.time > new_times[0] and
                    event.time < new_times[-1])
                event_getter(instrument)[:] = valid_events

        # Correct pitch bends and control changes
        adjust_events(lambda i: i.pitch_bends)
        adjust_events(lambda i: i.control_changes)

        def adjust_meta(events):
            """ This function adjusts the timing of the track-level meta-events
            in the provided list"""
            # Sort the events by time
            events.sort(key=lambda e: e.time)
            # Correct the events by interpolating
            event_times = np.array([event.time for event in events])
            adjusted_event_times = np.interp(
                event_times, original_times, new_times)
            for event, adjusted_event_time in zip(events,
                                                  adjusted_event_times):
                event.time = adjusted_event_time
            # We want to keep only the final event with time == new_times[0]
            valid_events = [event for event in events
                            if event.time == new_times[0]]
            if valid_events:
                valid_events = valid_events[-1:]
            # Otherwise only keep event within the new set of times
            valid_events.extend(
                event for event in events
                if event.time > new_times[0] and event.time < new_times[-1])
            events[:] = valid_events

        # Adjust key signature change event times
        adjust_meta(self.key_signature_changes)
        # Adjust lyrics
        adjust_meta(self.lyrics)
        # Adjust text events
        adjust_meta(self.text_events)

        # Remove all downbeats which appear before the start of original_times
        original_downbeats = original_downbeats[
            original_downbeats >= original_times[0]]
        # Adjust downbeat timing
        adjusted_downbeats = np.interp(
            original_downbeats, original_times, new_times)
        # Adjust time signature change event times
        adjust_meta(self.time_signature_changes)
        # In some cases there are no remaining downbeats
        if adjusted_downbeats.size > 0:
            # Move the final time signature change which appears before the
            # first adjusted downbeat to appear at the first adjusted downbeat
            ts_changes_before_downbeat = [
                t for t in self.time_signature_changes
                if t.time <= adjusted_downbeats[0]]
            if ts_changes_before_downbeat:
                ts_changes_before_downbeat[-1].time = adjusted_downbeats[0]
                # Remove all other time signature changes which appeared before
                # the first adjusted downbeat
                self.time_signature_changes = [
                    t for t in self.time_signature_changes
                    if t.time >= adjusted_downbeats[0]]
            else:
                # Otherwise, just add a 4/4 signature at the first downbeat
                self.time_signature_changes.insert(
                    0, TimeSignature(4, 4, adjusted_downbeats[0]))

        # Finally, we will adjust and add tempo changes so that the
        # tick-to-time mapping remains valid
        # The first thing we need is to map original_times onto the existing
        # quantized tick grid, because otherwise when we are re-creating tick
        # scales below the rounding errors accumulate and result in a bad,
        # wandering mapping.  This may not be the optimal way of doing this,
        # but it does the right thing.
        self._update_tick_to_time(self.time_to_tick(original_times[-1]))
        original_times = [self.__tick_to_time[self.time_to_tick(time)]
                          for time in original_times]
        # Use spacing between timing to change tempo changes
        tempo_change_times, tempo_changes = self.get_tempo_changes()
        # Since we will be using spacing between times, we must remove all
        # times where there is no difference (or else the scale would be 0 or
        # infinite)
        non_repeats = [0] + [n for n in range(1, len(new_times))
                             if new_times[n - 1] != new_times[n] and
                             original_times[n - 1] != original_times[n]]
        new_times = [new_times[n] for n in non_repeats]
        original_times = [original_times[n] for n in non_repeats]
        # Compute the time scaling between the original and new timebase
        # This indicates how much we should scale tempi within that range
        speed_scales = np.diff(original_times)/np.diff(new_times)
        # Find the index of the first tempo change time within original_times
        tempo_idx = 0
        while (tempo_idx + 1 < len(tempo_changes) and
               original_times[0] >= tempo_change_times[tempo_idx + 1]):
            tempo_idx += 1
        # Create new lists of tempo change time and scaled tempi
        new_tempo_change_times, new_tempo_changes = [], []
        for start_time, end_time, speed_scale in zip(
                original_times[:-1], original_times[1:], speed_scales):
            # Add the tempo change time and scaled tempo
            new_tempo_change_times.append(start_time)
            new_tempo_changes.append(tempo_changes[tempo_idx]*speed_scale)
            # Also add and scale all tempi within the range of this scaled zone
            while (tempo_idx + 1 < len(tempo_changes) and
                   start_time <= tempo_change_times[tempo_idx + 1] and
                   end_time > tempo_change_times[tempo_idx + 1]):
                tempo_idx += 1
                new_tempo_change_times.append(tempo_change_times[tempo_idx])
                new_tempo_changes.append(tempo_changes[tempo_idx]*speed_scale)
        # Interpolate the new tempo change times
        new_tempo_change_times = np.interp(
            new_tempo_change_times, original_times, new_times)

        # Now, convert tempo changes to ticks and tick scales
        # Start from the first tempo change time if its time 0, otherwise use
        # 120 bpm by default at time 0.
        if new_tempo_change_times[0] == 0:
            last_tick = 0
            new_tempo_change_times = new_tempo_change_times[1:]
            last_tick_scale = 60.0/(new_tempo_changes[0]*self.resolution)
            new_tempo_changes = new_tempo_changes[1:]
        else:
            last_tick, last_tick_scale = 0, 60.0/(120.0*self.resolution)
        self._tick_scales = [(last_tick, last_tick_scale)]
        # Keep track of the previous tick scale time for computing the tick
        # for each tick scale
        previous_time = 0.
        for time, tempo in zip(new_tempo_change_times, new_tempo_changes):
            # Compute new tick location as the last tick plus the time between
            # the last and next tempo change, scaled by the tick scaling
            tick = last_tick + (time - previous_time)/last_tick_scale
            # Update the tick scale
            tick_scale = 60.0/(tempo*self.resolution)
            # Don't add tick scales if they are repeats
            if tick_scale != last_tick_scale:
                # Add in the new tick scale
                self._tick_scales.append((int(round(tick)), tick_scale))
                # Update the time and values of the previous tick scale
                previous_time = time
                last_tick, last_tick_scale = tick, tick_scale
        # Update the tick-to-time mapping
        self._update_tick_to_time(self._tick_scales[-1][0] + 1)

    def crop(self, start_time=0.0, end_time=None):
        """Crops the MIDI object, keeping only the segment from ``start_time``
        to ``end_time``.

        Parameters
        ----------
        start_time : float
            Segment start time, in seconds. Defaults to 0.0.
        end_time : float
            Segment end time, in seconds. Defaults to self.end_time().

        """
        midi_end_time = self.get_end_time()

        # Enforce that start_time is non-negative and end_time is lower than
        # the MIDI object's end time. 
        if start_time < 0.0:
            raise ValueError('start_time must be non-negative.')
        if end_time is None:
            end_time = midi_end_time
        elif end_time > midi_end_time:
            warnings.warn('end_time is greater than the MIDI object\'s '
                          'end time; automatically adjusting to it.')
            end_time = midi_end_time

        # Enforce that end_time is strictly higher than start_time
        if start_time >= end_time:
            raise ValueError('end_time must be strictly higher than ' 
                             'start_time.')
                             
        # Invoke self.adjust_times to perform the cropping.
        self.adjust_times([start_time, end_time], [start_time, end_time])

    def remove_invalid_notes(self):
        """Removes any notes whose end time is before or at their start time.

        """
        # Simply call the child method on all instruments
        for instrument in self.instruments:
            instrument.remove_invalid_notes()

    def write(self, filename):
        """Write the MIDI data out to a .mid file.

        Parameters
        ----------
        filename : str or file
            Path or file to write .mid file to.

        """

        def event_compare(event1, event2):
            """Compares two events for sorting.

            Events are sorted by tick time ascending. Events with the same tick
            time ares sorted by event type. Some events are sorted by
            additional values. For example, Note On events are sorted by pitch
            then velocity, ensuring that a Note Off (Note On with velocity 0)
            will never follow a Note On with the same pitch.

            Parameters
            ----------
            event1, event2 : mido.Message
               Two events to be compared.
            """
            # Construct a dictionary which will map event names to numeric
            # values which produce the correct sorting.  Each dictionary value
            # is a function which accepts an event and returns a score.
            # The spacing for these scores is 256, which is larger than the
            # largest value a MIDI value can take.
            secondary_sort = {
                'set_tempo': lambda e: (1 * 256 * 256),
                'time_signature': lambda e: (2 * 256 * 256),
                'key_signature': lambda e: (3 * 256 * 256),
                'lyrics': lambda e: (4 * 256 * 256),
                'text_events' :lambda e: (5 * 256 * 256),
                'program_change': lambda e: (6 * 256 * 256),
                'pitchwheel': lambda e: ((7 * 256 * 256) + e.pitch),
                'control_change': lambda e: (
                    (8 * 256 * 256) + (e.control * 256) + e.value),
                'note_off': lambda e: ((9 * 256 * 256) + (e.note * 256)),
                'note_on': lambda e: (
                    (10 * 256 * 256) + (e.note * 256) + e.velocity),
                'end_of_track': lambda e: (11 * 256 * 256)
            }
            # If the events have the same tick, and both events have types
            # which appear in the secondary_sort dictionary, use the dictionary
            # to determine their ordering.
            if (event1.time == event2.time and
                    event1.type in secondary_sort and
                    event2.type in secondary_sort):
                return (secondary_sort[event1.type](event1) -
                        secondary_sort[event2.type](event2))
            # Otherwise, just return the difference of their ticks.
            return event1.time - event2.time

        # Initialize output MIDI object
        mid = mido.MidiFile(ticks_per_beat=self.resolution, charset=self._charset)
        # Create track 0 with timing information
        timing_track = mido.MidiTrack()
        # Add a default time signature only if there is not one at time 0.
        add_ts = True
        if self.time_signature_changes:
            add_ts = min([ts.time for ts in self.time_signature_changes]) > 0.0
        if add_ts:
            # Add time signature event with default values (4/4)
            timing_track.append(mido.MetaMessage(
                'time_signature', time=0, numerator=4, denominator=4))

        # Add in each tempo change event
        for (tick, tick_scale) in self._tick_scales:
            timing_track.append(mido.MetaMessage(
                'set_tempo', time=tick,
                # Convert from microseconds per quarter note to BPM
                tempo=int(6e7/(60./(tick_scale*self.resolution)))))
        # Add in each time signature
        for ts in self.time_signature_changes:
            timing_track.append(mido.MetaMessage(
                'time_signature', time=self.time_to_tick(ts.time),
                numerator=ts.numerator, denominator=ts.denominator))
        # Add in each key signature
        # Mido accepts key changes in a different format than pretty_midi, this
        # list maps key number to mido key name
        key_number_to_mido_key_name = [
            'C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B',
            'Cm', 'C#m', 'Dm', 'D#m', 'Em', 'Fm', 'F#m', 'Gm', 'G#m', 'Am',
            'Bbm', 'Bm']
        for ks in self.key_signature_changes:
            timing_track.append(mido.MetaMessage(
                'key_signature', time=self.time_to_tick(ks.time),
                key=key_number_to_mido_key_name[ks.key_number]))
        # Add in all lyrics events
        for l in self.lyrics:
            timing_track.append(mido.MetaMessage(
                'lyrics', time=self.time_to_tick(l.time), text=l.text))
        # Add text events
        for l in self.text_events:
            timing_track.append(mido.MetaMessage(
                'text', time=self.time_to_tick(l.time), text=l.text))
        # Sort the (absolute-tick-timed) events.
        timing_track.sort(key=functools.cmp_to_key(event_compare))
        # Add in an end of track event
        timing_track.append(mido.MetaMessage(
            'end_of_track', time=timing_track[-1].time + 1))
        mid.tracks.append(timing_track)
        # Create a list of possible channels to assign - this seems to matter
        # for some synths.
        channels = list(range(16))
        # Don't assign the drum channel by mistake!
        channels.remove(9)
        for n, instrument in enumerate(self.instruments):
            # Initialize track for this instrument
            track = mido.MidiTrack()
            # Add track name event if instrument has a name
            if instrument.name:
                track.append(mido.MetaMessage(
                    'track_name', time=0, name=instrument.name))
            # If it's a drum event, we need to set channel to 9
            if instrument.is_drum:
                channel = 9
            # Otherwise, choose a channel from the possible channel list
            else:
                channel = channels[n % len(channels)]
            # Set the program number
            track.append(mido.Message(
                'program_change', time=0, program=instrument.program,
                channel=channel))
            # Add all note events
            for note in instrument.notes:
                # Construct the note-on event
                track.append(mido.Message(
                    'note_on', time=self.time_to_tick(note.start),
                    channel=channel, note=note.pitch, velocity=note.velocity))
                # Also need a note-off event (note on with velocity 0)
                track.append(mido.Message(
                    'note_on', time=self.time_to_tick(note.end),
                    channel=channel, note=note.pitch, velocity=0))
            # Add all pitch bend events
            for bend in instrument.pitch_bends:
                track.append(mido.Message(
                    'pitchwheel', time=self.time_to_tick(bend.time),
                    channel=channel, pitch=bend.pitch))
            # Add all control change events
            for control_change in instrument.control_changes:
                track.append(mido.Message(
                    'control_change',
                    time=self.time_to_tick(control_change.time),
                    channel=channel, control=control_change.number,
                    value=control_change.value))
            # Sort all the events using the event_compare comparator.
            track = sorted(track, key=functools.cmp_to_key(event_compare))

            # If there's a note off event and a note on event with the same
            # tick and pitch, put the note off event first
            for n, (event1, event2) in enumerate(zip(track[:-1], track[1:])):
                if (event1.time == event2.time and
                        event1.type == 'note_on' and
                        event2.type == 'note_on' and
                        event1.note == event2.note and
                        event1.velocity != 0 and
                        event2.velocity == 0):
                    track[n] = event2
                    track[n + 1] = event1
            # Finally, add in an end of track event
            track.append(mido.MetaMessage(
                'end_of_track', time=track[-1].time + 1))
            # Add to the list of output tracks
            mid.tracks.append(track)
        # Turn ticks to relative time from absolute
        for track in mid.tracks:
            tick = 0
            for event in track:
                event.time -= tick
                tick += event.time
        # Write it out
        if isinstance(filename, six.string_types) or isinstance(filename, pathlib.PurePath):
            # If a string or path was given, pass it as the filename
            mid.save(filename=filename)
        else:
            # Otherwise, try passing it in as a file pointer
            mid.save(file=filename)
