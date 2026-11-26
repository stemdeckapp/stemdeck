# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
""" tokenizer.py: Encodes and decodes events to/from tokens. """
import numpy as np
import warnings
from abc import ABC, abstractmethod
from utils.note_event_dataclasses import Event, EventRange, Note  #, Codec
from utils.event_codec import FastCodec as Codec
from utils.note_event_dataclasses import NoteEvent
from utils.note2event import note_event2event
from utils.event2note import event2note_event, note_event2note
from typing import List, Optional, Union, Tuple, Dict, Counter


#TODO: Too complex to be an abstract class.
class EventTokenizerBase(ABC):
    """
    A base class for encoding and decoding events to and from tokens.
    """

    def __init__(
        self,
        base_codec: Union[Codec, str] = 'mt3',
        special_tokens: List[str] = ['PAD', 'EOS', 'UNK'],
        extra_tokens: List[str] = [],
        max_shift_steps: int = 206,  # 1001 in Gardner et al.
        program_vocabulary: Optional[Dict] = None,
        drum_vocabulary: Optional[Dict] = None,
    ) -> None:
        """
        Initializes the EventTokenizerBase object.

        :param base_codec: The codec to use for encoding and decoding.
        :param special_tokens: None or list of special tokens to include in the vocabulary.
        :param extra_tokens: None or list of tokens to be treated as additional special tokens.
        :param program_vocabulary: None or a dictionary mapping program names to program indices.
        :param drum_vocabulary: None or a dictionary mapping drum names to drum indices.
        :param max_shift_steps: The maximum number of shift steps to use for the codec.
        """
        # Initialize the codec attribute based on the input codec parameter.
        if isinstance(base_codec, str):
            # If codec is a string, initialize codec with the appropriate Codec object.
            if base_codec.lower() == 'mt3':
                event_ranges = [
                    EventRange('pitch', min_value=0, max_value=127),
                    EventRange('velocity', min_value=0, max_value=1),
                    EventRange('tie', min_value=0, max_value=0),
                    EventRange('program', min_value=0, max_value=127),
                    EventRange('drum', min_value=0, max_value=127),
                ]
            else:
                raise ValueError(f'Unknown codec name: {base_codec}')

            # Initialize codec
            self.codec = Codec(special_tokens=special_tokens + extra_tokens,
                               max_shift_steps=max_shift_steps,
                               event_ranges=event_ranges,
                               program_vocabulary=program_vocabulary,
                               drum_vocabulary=drum_vocabulary,
                               name='mt3')

        elif isinstance(base_codec, Codec):
            # If codec is a Codec object, store it directly.
            self.codec = base_codec
            if program_vocabulary is not None or drum_vocabulary is not None:
                print('')
                warnings.warn("Vocabulary cannot be applied when using a custom codec.")
        else:
            # If codec is neither a string nor a Codec object, raise a NotImplementedError.
            raise TypeError(f'Unknown codec type: {type(base_codec)}')
        self.num_tokens = self.codec._num_classes

    def _encode(self, events: List[Event]) -> List[int]:
        return [self.codec.encode_event(e) for e in events]

    def _decode(self, tokens: List[int]) -> List[Event]:
        return [self.codec.decode_event_index(idx) for idx in tokens]

    @abstractmethod
    def encode(self):
        """ Encode your custom events to tokens. """
        pass

    @abstractmethod
    def decode(self):
        """ Decode your custom tokens to events."""
        pass


class EventTokenizer(EventTokenizerBase):
    """
    Eencoding and decoding events to and from tokens.
    """

    def __init__(self,
                 base_codec: Union[Codec, str] = 'mt3',
                 special_tokens: List[str] = ['PAD', 'EOS', 'UNK'],
                 extra_tokens: List[str] = [],
                 max_shift_steps: int = 206,
                 program_vocabulary: Optional[Dict] = None,
                 drum_vocabulary: Optional[Dict] = None) -> None:
        """
        Initializes the EventTokenizerBase object.

        :param codec: The codec to use for encoding and decoding.
        :param special_tokens: None or list of special tokens to include in the vocabulary.
        :param extra_tokens: None or list of tokens to be treated as additional special tokens.
        :param program_vocabulary: None or a dictionary mapping program names to program indices.
        :param drum_vocabulary: None or a dictionary mapping drum names to drum indices.
        :param max_shift_steps: The maximum number of shift steps to use for the codec.
        """
        # Initialize the codec attribute based on the input codec parameter.
        super().__init__(
            base_codec=base_codec,
            special_tokens=special_tokens,
            extra_tokens=extra_tokens,
            max_shift_steps=max_shift_steps,
            program_vocabulary=program_vocabulary,
            drum_vocabulary=drum_vocabulary,
        )

    def encode(self, events):
        """ Encode your custom events to tokens. """
        return super()._encode(events)

    def decode(self, tokens):
        """ Decode your custom tokens to events."""
        return super()._decode(tokens)


class NoteEventTokenizer(EventTokenizerBase):
    """ Encodes and decodes note events to/from tokens. """

    def __init__(
            self,
            base_codec: Union[Codec, str] = 'mt3',
            max_length: int = 1024,  # max length of tokens 
            tps: int = 100,
            sort_note_event: bool = True,
            special_tokens: List[str] = ['PAD', 'EOS', 'UNK'],
            extra_tokens: List[str] = [],
            max_shift_steps: int = 206,
            program_vocabulary: Optional[Dict] = None,
            drum_vocabulary: Optional[Dict] = None,
            ignore_decoding_tokens: List[str] = [],
            ignore_decoding_tokens_from_and_to: Optional[List[str]] = None,
            debug_mode: bool = False) -> None:
        """
        Initializes the TaskEventNoteTokenizer object.

        List[NoteEvent] -> encdoe_note_events -> np.ndarray[int]

        np.ndarray[int] -> decode_note_events -> Tuple[List[NoteEvent], List[NoteEvent]]
                             
        :param codec: The codec to use for encoding and decoding.
        :param special_tokens: None or list of special tokens to include in the vocabulary.
        :param extra_tokens: None or list of tokens to be treated as additional special tokens.
        :param program_vocabulary: None or a dictionary mapping program names to program indices.
        :param drum_vocabulary: None or a dictionary mapping drum names to drum indices.
        :param max_shift_steps: The maximum number of shift steps to use for the codec.

        :param ignore_decoding_tokens: List of tokens to ignore during decoding.
        :param ignore_decoding_tokens_from_and_to: List of tokens to ignore during decoding. [from, to]
        """
        super().__init__(base_codec=base_codec,
                         special_tokens=special_tokens,
                         extra_tokens=extra_tokens,
                         max_shift_steps=max_shift_steps,
                         program_vocabulary=program_vocabulary,
                         drum_vocabulary=drum_vocabulary)
        self.max_length = max_length
        self.tps = tps
        self.sort = sort_note_event

        # Prepare prefix, suffix and pad tokens.
        self._prefix = []
        self._suffix = []
        for stk in self.codec.special_tokens:
            if stk == 'EOS':
                self._suffix.append(self.codec.special_tokens.index('EOS'))
            elif stk == 'PAD':
                self._zero_pad = [0] * 1024
            elif stk == 'UNK':
                pass
            else:
                pass
                # raise NotImplementedError(f'Unknown special token: {stk}')
        self.eos_id = self.codec.special_tokens.index('EOS')
        self.pad_id = self.codec.special_tokens.index('PAD')
        self.ids_to_ignore_decoding = [self.codec.special_tokens.index(t) for t in ignore_decoding_tokens]
        self.ignore_tokens_from_and_to = ignore_decoding_tokens_from_and_to
        self.debug_mode = debug_mode

    def _decode(self, tokens):
        # This is event detokenizer, not note_event. It is required for displaying events in validation dashboard
        return super()._decode(tokens)

    def encode(
        self,
        note_events: List[NoteEvent],
        tie_note_events: Optional[List[NoteEvent]] = None,
        start_time: float = 0.,
    ) -> List[int]:
        """ Encodes note events and tie note events to tokens. """
        events = note_event2event(
            note_events=note_events,
            tie_note_events=tie_note_events,
            start_time=start_time,  # required for calcuating relative time
            tps=self.tps,
            sort=self.sort)
        return super()._encode(events)

    def encode_plus(
            self,
            note_events: List[NoteEvent],
            tie_note_events: Optional[List[NoteEvent]] = None,
            start_times: float = 0.,  # Fixing bug: start_time --> start_times 
            add_special_tokens: Optional[bool] = True,
            max_length: Optional[int] = None,  #  if None, use self.max_length
            pad_to_max_length: Optional[bool] = True,
            return_attention_mask: bool = False) -> Union[List[int], Tuple[List[int], List[int]]]:
        """ Encodes note events and tie note info to padded tokens. """
        encoded = self.encode(note_events, tie_note_events, start_times)

        # if task_events:
        #     encoded = super()._encode(task_events) + encoded
        if add_special_tokens:
            if self._prefix:
                encoded = self._prefix + encoded
            if self._suffix:
                encoded = encoded + self._suffix

        if max_length is None:
            max_length = self.max_length

        length = len(encoded)
        if length >= max_length:
            encoded = encoded[:max_length]
            length = max_length

        if return_attention_mask:
            attention_mask = [1] * length

        # <PAD>
        if pad_to_max_length is True:
            if len(self._zero_pad) != max_length:
                self._zero_pad = [self.pad_id] * max_length
            if return_attention_mask:
                attention_mask += self._zero_pad[length:]
            encoded = encoded + self._zero_pad[length:]

        if return_attention_mask:
            return encoded, attention_mask

        return encoded

    def encode_task(self, task_events: List[Event], max_length: Optional[int] = None) -> List[int]:
        # NOTE: This is an event tokenizer that generates task ids, not the list of note_event objects.
        encoded = super()._encode(task_events)

        # <PAD>
        if max_length is not None:
            if len(self._zero_pad_task) != max_length:
                self._zero_pad_task = [self.pad_id] * max_length
            length = len(encoded)
            encoded = encoded + self._zero_pad[length:]

        return encoded

    def decode(
        self,
        tokens: List[int],
        start_time: float = 0.,
        return_events: bool = False,
    ) -> Union[Tuple[List[NoteEvent], List[NoteEvent]], Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]],
                                                              List[Event], int]]:
        """Decodes a sequence of tokens into note events.

        Args:
            tokens (List[int]): The list of tokens to be decoded.
            start_time (float, optional): The starting time for the note events. Defaults to 0.
            return_events (bool, optional): Indicates whether to include the raw events in the return value.
                                            Defaults to False.

        Returns:
            Union[Tuple[List[NoteEvent], List[NoteEvent]],
                Tuple[List[NoteEvent], List[NoteEvent], List[Event], int]]: The decoded note events.
            If `return_events` is False, the returned tuple contains `note_events`, `tie_note_events`,
            `last_activity`, and `err_cnt`.
            If `return_events` is True, the returned tuple contains `note_events`, `tie_note_events`,
            `last_activity`, `events`, and `err_cnt`.
        """
        if self.debug_mode:
            ignored_tokens_from_input = [t for t in tokens if t in self.ids_to_ignore_decoding]
            print(ignored_tokens_from_input)

        if self.ids_to_ignore_decoding:
            tokens = [t for t in tokens if t not in self.ids_to_ignore_decoding]

        events = super()._decode(tokens)
        note_events, tie_note_events, last_activity, err_cnt = event2note_event(events, start_time, True, self.tps)
        if return_events:
            return note_events, tie_note_events, last_activity, events, err_cnt
        else:
            return note_events, tie_note_events, last_activity, err_cnt

    def decode_batch(
        self,
        batch_tokens: Union[List[List[int]], np.ndarray],
        start_times: List[float],
        return_events: bool = False
    ) -> Union[Tuple[List[Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]], List[float]]], int],
               Tuple[List[Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]], List[float]]], List[List[Event]],
                     Counter[str]]]:
        """ 
        Decodes a batch of tokens to note_events and tie_note_events.

        Args:
            batch_tokens (List[List[int]] or np.ndarray): Tokens to be decoded.
            start_times (List[float]): List of start times for each token set.
            return_events (bool, optional): Flag to determine if events should be returned. Defaults to False.

        """
        if isinstance(batch_tokens, np.ndarray):
            batch_tokens = batch_tokens.tolist()

        if len(batch_tokens) != len(start_times):
            raise ValueError('The length of batch_tokens and start_times must be same.')

        zipped_note_events_and_tie = []
        list_events = []
        total_err_cnt = 0

        for tokens, start_time in zip(batch_tokens, start_times):
            if return_events:
                note_events, tie_note_events, last_activity, events, err_cnt = self.decode(
                    tokens, start_time, return_events)
                list_events.append(events)
            else:
                note_events, tie_note_events, last_activity, err_cnt = self.decode(tokens, start_time, return_events)

            zipped_note_events_and_tie.append((note_events, tie_note_events, last_activity, start_time))
            total_err_cnt += err_cnt

        if return_events:
            return zipped_note_events_and_tie, list_events, total_err_cnt
        else:
            return zipped_note_events_and_tie, total_err_cnt

    def decode_list_batches(
        self,
        list_batch_tokens: Union[List[List[List[int]]], List[np.ndarray]],
        list_start_times: Union[List[List[float]], List[float]],
        return_events: bool = False
    ) -> Union[Tuple[List[List[Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]], List[float]]]], Counter[str]],
               Tuple[List[List[Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]], List[float]]]],
                     List[List[Event]], Counter[str]]]:
        """ 
        Decodes a list of variable-size batches of token array to a list of
        zipped note_events and tie_note_events.

        Args:
            list_batch_tokens: List[np.ndarray], where array shape is (batch_size, variable_length)
            list_start_times: List[float], where the length is sum of all batch_sizes.
            return_events: bool, Defaults to False.

        Returns:
            list_list_zipped_note_events_and_tie:
                List[
                    Tuple[
                        List[NoteEvent]: A list of note events.
                        List[NoteEvent]: A list of tie note events.
                        List[Tuple[int]]: A list of last activity of segment. [(program, pitch), ...]. This is useful
                            for validating notes within a batch of segments extracted from a file.
                        List[float]: A list of segment start times.
                    ]
                ]
            (Optional) list_events:
                List[List[Event]]
            total_err_cnt:
                Counter[str]: error counter.
        """
        list_tokens = []
        for arr in list_batch_tokens:
            for tokens in arr:
                list_tokens.append(tokens)
        assert (len(list_tokens) == len(list_start_times))

        zipped_note_events_and_tie = []
        list_events = []
        total_err_cnt = Counter()
        for tokens, start_time in zip(list_tokens, list_start_times):
            note_events, tie_note_events, last_activity, events, err_cnt = self.decode(
                tokens, start_time, return_events)
            zipped_note_events_and_tie.append((note_events, tie_note_events, last_activity, start_time))
            if return_events:
                list_events.append(events)
            total_err_cnt += err_cnt

        if return_events:
            return zipped_note_events_and_tie, list_events, total_err_cnt
        else:
            return zipped_note_events_and_tie, total_err_cnt
