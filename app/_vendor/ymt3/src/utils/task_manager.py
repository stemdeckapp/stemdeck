# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
import numpy as np
from typing import Optional, Union, Tuple, Dict, Any, List, Counter
from utils.note_event_dataclasses import NoteEvent, Event, NoteEventListsBundle
from config.task import task_cfg
from config.config import model_cfg
from utils.tokenizer import NoteEventTokenizer
from utils.utils import create_program2channel_vocab
from utils.note2event import separate_channel_by_program_group_from_note_event_lists_bundle

SINGING_PROGRAM = 100
DRUM_PROGRAM = 128
UNANNOTATED_PROGRAM = 129

# import random
# class RandomProgramSampler:
#     def __init__(self, program_vocab: Dict[str, int], max_n: int = 7):
#         for key, values in program_vocab.items():
#             for value in values:
#                 self.inverse_vocab_program[value] = values[0]
#         self.max_n = max_n
#         self.shuffled_

#     def sample(self):

# def shuffle_and_repeat_randomly(lst, max_n=5):
#     shuffled = lst.copy()
#     random.shuffle(shuffled)
#     index = 0

#     while True:
#         if index >= len(shuffled):  # 리스트의 모든 요소가 사용되면, 다시 셔플
#             random.shuffle(shuffled)
#             index = 0

#         n = random.randint(1, max_n)  # 1과 max_n 사이의 랜덤한 개수 결정
#         end_index = index + n

#         if end_index > len(shuffled):  # 리스트의 끝을 넘어가는 경우, 리스트의 끝까지만 반환
#             yield shuffled[index:]
#             index = len(shuffled)
#         else:
#             yield shuffled[index:end_index]
#             index = end_index


class TaskManager:
    """
    The TaskManager class manages tasks for training. It is initialized with a task name and retrieves 
    the corresponding configuration from the task_cfg dictionary defined in config/task.py.

    Attributes:
        # Basic
        task_name (str): The name of the task being managed.
        base_codec (str): The base codec associated with the task.
        train_program_vocab (dict): The program vocabulary used for training.
        train_drum_vocab (dict): The drum vocabulary used for training.
        subtask_tokens (list): Additional tokens specific to subtasks, if any.
        extra_tokens (list): Extra tokens used in the task, including subtask tokens.
        ignore_decoding_tokens (list): Tokens to ignore during decoding.
        ignore_decoding_tokens_by_delimiter (Optional, list[str, str]): Tokens to ignore during decoding by delimiters. Default is None.
        tokenizer (NoteEventTokenizer): An instance of the NoteEventTokenizer class for tokenizing note events.
        eval_subtask_prefix (dict): A dictionary defining evaluation subtask prefixes to tokens.

        # Multi-channel decoding task exclusive
        num_decoding_channels (int): The number of decoding channels.
        max_token_length_per_ch (int): The maximum token length per channel.
        mask_loss_strategy (str): The mask loss strategy to use. NOT IMPLEMENTED YET.
        program2channel_vocab (dict): A dictionary mapping program to channel.

    Methods:
        get_tokenizer(): Returns the tokenizer instance associated with the task.
        set_tokenizer(): Initializes the tokenizer using the NoteEventTokenizer class with the appropriate parameters.
    """

    def __init__(self, task_name: str = "mt3_full_plus", max_shift_steps: int = 206, debug_mode: bool = False):
        """
        Initializes a TaskManager object with the specified task name.

        Args:
            task_name (str): The name of the task to manage.
            max_shift_steps (int): The maximum shift steps for the tokenizer. Default is 206. Definable in config/config.py.
            debug_mode (bool): Whether to enable debug mode. Default is False.
        """
        self.debug_mode = debug_mode
        self.task_name = task_name

        if task_name not in task_cfg.keys():
            raise ValueError("Invalid task name")
        else:
            self.task = task_cfg[task_name]

        # Basic task parameters
        self.base_codec = self.task.get("base_codec", "mt3")
        self.train_program_vocab = self.task["train_program_vocab"]
        self.train_drum_vocab = self.task["train_drum_vocab"]
        self.subtask_tokens = self.task.get("subtask_tokens", [])
        self.extra_tokens = self.subtask_tokens + self.task.get("extra_tokens", [])
        self.ignore_decoding_tokens = self.task.get("ignore_decoding_tokens", [])
        self.ignore_decoding_tokens_from_and_to = self.task.get("ignore_decoding_tokens_from_and_to", None)
        self.max_note_token_length = self.task.get("max_note_token_length", model_cfg["event_length"])
        self.max_task_token_length = self.task.get("max_task_token_length", 0)
        self.padding_task_token = self.task.get("padding_task_token", False)
        self._eval_subtask_prefix = self.task.get("eval_subtask_prefix", None)
        self.eval_subtask_prefix_dict = {}

        # Multi-channel decoding exclusive parameters
        self.num_decoding_channels = self.task.get("num_decoding_channels", 1)
        if self.num_decoding_channels > 1:
            program2channel_vocab_source = self.task.get("program2channel_vocab_source", None)
            if program2channel_vocab_source is None:
                program2channel_vocab_source = self.train_program_vocab

            # Create an inverse mapping of program to channel
            if self.num_decoding_channels == len(program2channel_vocab_source) + 1:
                self.program2channel_vocab, _ = create_program2channel_vocab(program2channel_vocab_source)
            else:
                raise ValueError("Invalid num_decoding_channels, or program2channel_vocab not provided")

            self.max_note_token_length_per_ch = self.task.get("max_note_token_length_per_ch")
            self.mask_loss_strategy = self.task.get("mask_loss_strategy", None)  # Not implemented yet
        else:
            self.max_note_token_length_per_ch = self.max_note_token_length

        # Define max_total_token_length
        self.max_total_token_length = self.max_note_token_length_per_ch + self.max_task_token_length

        # Max shift steps for the tokenizer
        self.max_shift_steps = max_shift_steps

        # Initialize a tokenizer
        self.set_tokenizer()
        self.set_eval_task_prefix()
        self.num_tokens = self.tokenizer.num_tokens
        self.inverse_vocab_program = self.tokenizer.codec.inverse_vocab_program

    def set_eval_task_prefix(self) -> None:
        """
        Sets the evaluation task prefix for the task.

        Example:
            self.eval_task_prefix_dict = {
                "default": [Event("transcribe_all", 0), Event("task", 0)],
                "singing-only": [Event("transcribe_singing", 0), Event("task", 0)]
                }
        """
        if self._eval_subtask_prefix is not None:
            assert "default" in self._eval_subtask_prefix.keys()
            for key, val in self._eval_subtask_prefix.items():
                if self.padding_task_token:
                    self.eval_subtask_prefix_dict[key] = self.tokenizer.encode_task(
                        val, max_length=self.max_task_token_length)
                else:
                    self.eval_subtask_prefix_dict[key] = self.tokenizer.encode_task(val)
        else:
            self.eval_subtask_prefix_dict["default"] = []

    def get_eval_subtask_prefix_dict(self) -> dict:
        return self.eval_subtask_prefix_dict

    def get_tokenizer(self) -> NoteEventTokenizer:
        """
        Returns the tokenizer instance associated with the task.

        Returns:
            NoteEventTokenizer: The tokenizer instance.
        """
        return self.tokenizer

    def set_tokenizer(self) -> None:
        """
        Initializes the tokenizer using the NoteEventTokenizer class with the appropriate parameters.
        """
        self.tokenizer = NoteEventTokenizer(base_codec=self.base_codec,
                                            max_length=self.max_total_token_length,
                                            program_vocabulary=self.train_program_vocab,
                                            drum_vocabulary=self.train_drum_vocab,
                                            special_tokens=['PAD', 'EOS', 'UNK'],
                                            extra_tokens=self.extra_tokens,
                                            max_shift_steps=self.max_shift_steps,
                                            ignore_decoding_tokens=self.ignore_decoding_tokens,
                                            ignore_decoding_tokens_from_and_to=self.ignore_decoding_tokens_from_and_to,
                                            debug_mode=self.debug_mode)

    # Newly implemented for exclusive transcription task
    def tokenize_task_and_note_events_batch(
            self,
            programs_segments: List[List[int]],
            has_unannotated_segments: List[bool],
            note_event_segments: NoteEventListsBundle,
            subunit_programs_segments: Optional[List[List[np.ndarray]]] = None,  # TODO
            subunit_note_event_segments: Optional[List[NoteEventListsBundle]] = None,  # TODO
            stage: str = 'train'  # 'train' or 'eval'
    ):
        """Tokenizes a batch of note events into a batch of encoded tokens.
           Optionally, appends task tokens to the note event tokens.
        
        Args:
            programs_segments (List[int]): A list of program numbers.
            has_unannotated_segments (bool): Whether the batch has unannotated segments.
            note_event_segments (NoteEventListsBundle): A bundle of note events.
            subunit_programs_segments (Optional[List[List[np.ndarray]]]): A list of subunit programs.
            subunit_note_event_segments (Optional[List[NoteEventListsBundle]]): A list of subunit note events.
        
        Returns:
            np.ndarray: A batch of encoded tokens, with shape (B, C, L).        
        """
        if self.task_name == 'exclusive':
            # batch_sz = len(programs_segments)
            # token_array = np.zeros((batch_sz, self.num_decoding_channels, self.max_note_token_length_per_ch),
            #                        dtype=np.int32)

            # for programs, has_unannotated, note_events, tie_note_events, start_times in zip(
            #         programs_segments, has_unannotated_segments, note_event_segments['note_events'],
            #         note_event_segments['tie_note_events'], note_event_segments['start_times']):
            #     if has_unannotated:
            #         annotated_programs = [p for p in programs if p != UNANNOTATED_PROGRAM]
            #         note_token_array = self.tokenizer.encode_plus(note_events,
            #                                                       tie_note_events,
            #                                                       start_times,
            #                                                       pad_to_max_length=False) # will append EOS token
            #         task_token_array = self.tokenizer.encode_task(task_events)
            #     else:
            #         annotated_programs = programs

            #     task_events = [Event('transcribe_all', 0), Event('task', 0)]
            #     note_token_array = self.tokenize_note_events_batch(note_events)
            #     task_token_array = self.tokenize_task_events(annotated_programs, has_unannotated)
            # return []
            raise NotImplementedError("Exclusive transcription task is not implemented yet.")
        else:
            # Default task: single or multi-channel decoding, without appending task tokens
            return self.tokenize_note_events_batch(note_event_segments)  # (B, C, L)
            # Exclusive transcription task
            # if has_unannotated_segments:
            #     annotated_programs = [p for p in programs_segments if p != UNANNOTATED_PROGRAM]
            # else:
            #     annotated_programs = programs_segments

            # # Main task: transcribe all
            # main_task_events = self.task.get("eval_subtask_prefix")

    def tokenize_note_events_batch(self,
                                   note_event_segments: NoteEventListsBundle,
                                   start_time_to_zero: bool = False,
                                   sort: bool = True) -> np.ndarray:
        """Tokenizes a batch of note events into a batch of encoded tokens.
        
        Args:
            note_event_segments (NoteEventListsBundle): A bundle of note events.
        
        Returns:
            np.ndarray: A batch of encoded tokens, with shape (B, C, L).        
        """
        batch_sz = len(note_event_segments["note_events"])
        note_token_array = np.zeros((batch_sz, self.num_decoding_channels, self.max_note_token_length_per_ch),
                                    dtype=np.int32)

        if self.num_decoding_channels == 1:
            # Single-channel decoding task
            zipped_events = list(zip(*note_event_segments.values()))
            for b in range(batch_sz):
                note_token_array[b, 0, :] = self.tokenizer.encode_plus(*zipped_events[b],
                                                                       max_length=self.max_note_token_length,
                                                                       pad_to_max_length=True)
        elif self.num_decoding_channels > 1:
            # Multi-channel decoding task
            ch_sep_ne_bundle = separate_channel_by_program_group_from_note_event_lists_bundle(
                source_note_event_lists_bundle=note_event_segments,
                num_program_groups=self.num_decoding_channels,
                program2channel_vocab=self.program2channel_vocab,
                start_time_to_zero=start_time_to_zero,
                sort=sort)  # (batch_sz,)

            for b in range(batch_sz):
                zipped_channel = list(zip(*ch_sep_ne_bundle[b].values()))
                for c in range(self.num_decoding_channels):
                    note_token_array[b, c, :] = self.tokenizer.encode_plus(*zipped_channel[c],
                                                                           max_length=self.max_note_token_length_per_ch,
                                                                           pad_to_max_length=True)
        return note_token_array  # (B, C, L)

    def tokenize_note_events(self,
                             note_events: List[NoteEvent],
                             tie_note_events: Optional[List[NoteEvent]] = None,
                             start_time: float = 0.,
                             **kwargs: Any) -> List[int]:
        """(Deprecated) Tokenizes a sequence of note events into a sequence of encoded tokens."""
        return self.tokenizer.encode_plus(note_events, tie_note_events, start_time, **kwargs)


# # This will be deprecated, currently used by datasets_eval.py

#     def tokenize_task_events_batch(self, programs_segments: List[int],
#                                    has_unannotated_segments: List[bool]) -> List[int]:
#         """Tokenizes batch of task tokens from annotation info.

#         Args:
#             programs_segments (List[int]): A list of program numbers.
#             has_unannotated_segments (bool): Whether the batch has unannotated segments.

#         Returns:
#             np.ndarray: Shape (B, C, L).

#         """
#         batch_sz = len(programs_segments)
#         task_token_array = np.zeros((batch_sz, self.num_decoding_channels, self.max_task_token_length), dtype=np.int32)

#         if self.max_task_token_length == 0:
#             return task_token_array

#         if self.num_decoding_channels == 1:
#             for b in range(batch_sz):
#                 task_token_array[b, 0, :] = self.tokenize_task_events(programs_segments[b], has_unannotated_segments[b])
#         elif self.num_decoding_channels > 1:
#             for b in range(batch_sz):
#                 task_token_array[b, :, :] = self.tokenize_task_events(programs_segments[b], has_unannotated_segments[b])
#         return task_token_array  # (B, C, L)

    def tokenize_task_events(self, programs: List[int], has_unannotated: bool) -> List[int]:
        """Tokenizes a sequence of programs into a sequence of encoded tokens. Used for training."""
        if self.task_name == 'singing_drum_v1':
            if has_unannotated:
                if SINGING_PROGRAM in programs:
                    task_events = [Event('transcribe_singing', 0), Event('task', 0)]
                elif DRUM_PROGRAM in programs:
                    task_events = [Event('transcribe_drum', 0), Event('task', 0)]
            else:
                task_events = [Event('transcribe_all', 0), Event('task', 0)]
        else:
            return []

        if self.padding_task_token:
            return self.tokenizer.encode_task(task_events, max_length=self.max_task_token_length)
        else:
            return self.tokenizer.encode_task(task_events)

    def detokenize(
        self,
        tokens: List[int],
        start_time: float = 0.,
        return_events: bool = False
    ) -> Union[Tuple[List[NoteEvent], List[NoteEvent]], Tuple[List[NoteEvent], List[NoteEvent], List[Event], int]]:
        """Decodes a sequence of tokens into note events, ignoring specific token IDs.
        Returns:
            Union[Tuple[List[NoteEvent], List[NoteEvent]],
                Tuple[List[NoteEvent], List[NoteEvent], List[Event], int]]: The decoded note events.
            If `return_events` is False, the returned tuple contains `note_events`, `tie_note_events`,
            `last_activity`, and `err_cnt`.
            If `return_events` is True, the returned tuple contains `note_events`, `tie_note_events`,
            `last_activity`, `events`, and `err_cnt`.

        Notes:
            This decoding process ignores specific token IDs based on `self.ids_to_ignore_decoding` attribute.
        """
        return self.tokenizer.decode(tokens=tokens, start_time=start_time, return_events=return_events)

    def detokenize_list_batches(
        self,
        list_batch_tokens: Union[List[List[List[int]]], List[np.ndarray]],
        list_start_times: Union[List[List[float]], List[float]],
        return_events: bool = False
    ) -> Union[Tuple[List[List[Tuple[List[NoteEvent], List[NoteEvent], int, float]]], Counter[str]], Tuple[
            List[List[Tuple[List[NoteEvent], List[NoteEvent], int, float]]], List[List[Event]], Counter[str]]]:
        """ Decodes a list of variable size batches of token array to a list of
            zipped note_events and tie_note_events.

        Args:
            list_batch_tokens: List[np.ndarray], where array shape is (batch_size, variable_length)
            list_start_times: List[float], where the length is sum of all batch_sizes.
            return_events: bool
        
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
        return self.tokenizer.decode_list_batches(list_batch_tokens, list_start_times, return_events)
