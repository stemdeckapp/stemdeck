# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""t5mod_helper.py"""
import torch
from torch import nn
from model.t5mod import T5DecoderYMT3, MultiChannelT5Decoder
from typing import Optional, Callable, Union, Literal


@torch.no_grad()
def task_cond_dec_generate(decoder: Union[T5DecoderYMT3, MultiChannelT5Decoder],
                           decoder_type: Literal["t5", "multi-t5"],
                           embed_tokens: nn.Embedding,
                           lm_head: nn.Module,
                           encoder_hidden_states: torch.FloatTensor,
                           shift_right_fn: Callable,
                           prefix_ids: Optional[torch.LongTensor] = None,
                           max_length: int = 1024,
                           stop_at_eos: bool = True,
                           eos_id: Optional[int] = 1,
                           pad_id: Optional[int] = 0,
                           decoder_start_token_id: Optional[int] = 0,
                           debug: bool = False) -> torch.LongTensor:
    """
    Generate sequence by task conditioning on the decoder side
    :An extension of transofrmers.generate() function for the model with 
    conditioning only on the decoder side. 
    
    Args:
        decoder: T5DecoderYMT3 or MultiChannelT5Decoder, any decoder model with T5Stack architecture
        decoder_type: Literal["t5", "multi-t5"], type of decoder
        embed_tokens: nn.Embedding, embedding layer for the decoder
        lm_head: nn.Module, language model head
        encoder_hidden_states: torch.FloatTensor, (B, T, D) or (B, K, T, D) last hidden states
        shift_right_fn: Callable, shift_right function of the decoder
        prefix_ids: torch.LongTensor, (B, prefix_len) prefix ids typically used as task conditioning to decoder.
        max_length: int, max token length to generate (default is 1024)
        stop_at_eos: bool, whether to early-stop when all predictions in the batch are the <eos> token.
        eos_id: int, the id of the <eos> token (default is 1)
        pad_id: int, the id of the <pad> token (default is 0)
        decoder_start_token_id: int, the id of the <bos> token (default is 0)
        debug: bool, whether to print debug information

    Returns:
        pred_ids: torch.LongTensor, (B, task_len + N) or (B, C, task_len + N) predicted token ids
    """
    bsz = encoder_hidden_states.shape[0]
    device = encoder_hidden_states.device

    # Prepare dec_input_shape: (B, 1) or (B, C, 1)
    if decoder_type == "t5":
        dec_input_shape = (bsz, 1)
    elif decoder_type == "multi-t5":
        dec_input_shape = (bsz, decoder.num_channels, 1)
    else:
        raise ValueError(f"decoder_type {decoder_type} is not supported.")

    # Prepare dec_input_ids: <bos> + task_prefix_token (B, prefix_len + 1) or (B, C, prefix_len + 1)
    if prefix_ids is not None and prefix_ids.numel() > 0:
        dec_input_ids = shift_right_fn(prefix_ids)
        prefix_length = prefix_ids.shape[-1]
    else:
        # if prefix_ids is None, use <bos> as initial inSput
        dec_input_ids = torch.tile(torch.LongTensor([decoder_start_token_id]).to(device), dec_input_shape)
        prefix_length = 0
    dec_inputs_embeds = embed_tokens(dec_input_ids)  # (B, L, D) or (B, C, L, D)

    # Generate decoder hidden state and past_key_values using prefix:
    """
    - initial inputs_embeds can be a sequence, without using past_key_values
    - dec_hs: (B, 1, D)
    - past_key_values: Tuple of length M for M layers of decoder
    - pred_ids: (B, prefix_len) where N is the length of prefix_ids 
    """
    dec_hs, past_key_values = decoder(inputs_embeds=dec_inputs_embeds,
                                      encoder_hidden_states=encoder_hidden_states,
                                      return_dict=False)
    logits = lm_head(dec_hs)  # (b, T=1, vocab_size) or (b, C, T=1, vocab_size)
    pred_ids = logits.argmax(-1)  # (B, prefix_len + 1) or (B, C, prefix_len + 1)

    # keep track of which sequences are already finished
    unfinished_sequences = torch.ones(dec_input_shape, dtype=torch.long, device=device)

    # Fast generation with past_key_values for the rest of the sequence
    if decoder_type == "t5":
        dec_input_ids = pred_ids[:, -1].unsqueeze(-1)  # (B, 1)
    elif decoder_type == "multi-t5":
        dec_input_ids = pred_ids[:, :, -1].unsqueeze(-1)  # (B, C, 1)
    for i in range(max_length - prefix_length - 1):  # -1 for <eos> token
        if debug:
            past_key_values_length = past_key_values[0][0].shape[
                2]  # past_key_values_length determines the positional embedding
            print(f'i = {i}, past_key_values_length = {past_key_values_length}, pred_ids.shape = {pred_ids.shape}')

        # when past_key_values is provided, we use only the last token as input_ids
        dec_inputs_embeds = embed_tokens(dec_input_ids)  # (B, 1, D) or (B, C, 1, D)
        dec_hs, _past_key_values = decoder(inputs_embeds=dec_inputs_embeds,
                                           encoder_hidden_states=encoder_hidden_states,
                                           past_key_values=past_key_values,
                                           return_dict=False)
        logits = lm_head(dec_hs)  # (b, 1, vocab_size) or (b, K, 1, vocab_size)
        _pred_ids = logits.argmax(-1)  # (B, 1) or (B, K, 1)

        # update input_ids and past_key_values for next iteration
        dec_input_ids = _pred_ids.clone(
        )  # (B, 1) or (B, C, 1), deepcopy of _pred_ids because _pred_ids will be modified for finished sentences
        past_key_values = _past_key_values

        # finished sentences should have their next token be a padding token
        if eos_id is not None:
            if pad_id is None:
                raise ValueError("If `eos_token_id` is defined, make sure that `pad_token_id` is defined.")
            _pred_ids = _pred_ids * unfinished_sequences + pad_id * (1 - unfinished_sequences)

        # update pred_ids
        pred_ids = torch.cat((pred_ids, _pred_ids), dim=-1)  # (B, T') or (B, C, T') with increasing T'

        # update state of unfinished_sequences
        if eos_id is not None:
            unfinished_sequences = unfinished_sequences * _pred_ids.ne(eos_id).long()

            # early-stop when each sentence is finished
            if stop_at_eos is True and unfinished_sequences.max() == 0:
                break

    return pred_ids  # (B, L) or (B, C, L)
