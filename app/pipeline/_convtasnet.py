"""Conv-TasNet masker, vendored from asteroid (MIT), plus the StemDeck wrapper.

The duet separator needs four things out of `asteroid`: a Conv-TasNet masker, a
global layer norm, and two shape helpers. Depending on the package for them
costs 23 transitive packages including pytorch-lightning, pandas and pesq, which
is not a reasonable thing to put inside a packaged desktop app, and any of it
landing in uv.lock sends every existing install through a full re-download.

So the pieces are copied here instead. `asteroid` is MIT licensed, and this is
its code, unmodified apart from removing branches nothing on this path uses.

  asteroid -- https://github.com/asteroid-team/asteroid
  Copyright (c) 2019 Pariente Manuel
  MIT, full text in app/_vendor/LICENSE.asteroid

MIT asks for the permission notice to travel with the code, not just the
copyright line, so the licence sits beside the yt-dlp-ejs one rather than being
paraphrased here. Everything below MixtureConsistent is StemDeck's own and
Apache-2.0 like the rest of the project.

`asteroid-filterbanks` is still a real dependency: it supplies the STFT encoder
and decoder, and unlike `asteroid` it pulls in nothing of its own.

Do not hand-edit the vendored classes. They must stay byte-identical to the
version the published checkpoint was trained against or the weights stop
loading. Regenerate instead.
"""

import functools
import inspect
from typing import List, Optional

import torch
from torch import nn
from torch.jit import is_tracing


# The checkpoint's config asks for norm_type "gLN" and mask_act "linear"; these
# stand in for asteroid.masknn.{norms,activations}.get without the registries.
class _Norms:
    @staticmethod
    def get(identifier):
        if identifier in (None, "gLN", "GlobLN"):
            return GlobLN
        raise ValueError(f"vendored build supports gLN only, got {identifier!r}")


class _Activations:
    @staticmethod
    def get(identifier):
        if identifier in (None, "linear"):
            return nn.Identity
        if identifier == "relu":
            return nn.ReLU
        if identifier == "sigmoid":
            return nn.Sigmoid
        raise ValueError(f"vendored build does not support mask_act {identifier!r}")


norms = _Norms()
activations = _Activations()


def script_if_tracing(fn):
    # Taken for pytorch for compat in 1.6.0
    """
    Compiles ``fn`` when it is first called during tracing. ``torch.jit.script``
    has a non-negligible start up time when it is first called due to
    lazy-initializations of many compiler builtins. Therefore you should not use
    it in library code. However, you may want to have parts of your library work
    in tracing even if they use control flow. In these cases, you should use
    ``@torch.jit.script_if_tracing`` to substitute for
    ``torch.jit.script``.

    Arguments:
        fn: A function to compile.

    Returns:
        If called during tracing, a :class:`ScriptFunction` created by `
        `torch.jit.script`` is returned. Otherwise, the original function ``fn`` is returned.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_tracing():
            # Not tracing, don't do anything
            return fn(*args, **kwargs)

        compiled_fn = torch.jit.script(wrapper.__original_fn)  # type: ignore
        return compiled_fn(*args, **kwargs)

    wrapper.__original_fn = fn  # type: ignore
    wrapper.__script_if_tracing_wrapper = True  # type: ignore

    return wrapper


@script_if_tracing
def pad_x_to_y(x: torch.Tensor, y: torch.Tensor, axis: int = -1) -> torch.Tensor:
    """Right-pad or right-trim first argument to have same size as second argument

    Args:
        x (torch.Tensor): Tensor to be padded.
        y (torch.Tensor): Tensor to pad `x` to.
        axis (int): Axis to pad on.

    Returns:
        torch.Tensor, `x` padded to match `y`'s shape.
    """
    if axis != -1:
        raise NotImplementedError
    inp_len = y.shape[axis]
    output_len = x.shape[axis]
    return nn.functional.pad(x, [0, inp_len - output_len])


@script_if_tracing
def jitable_shape(tensor):
    """Gets shape of ``tensor`` as ``torch.Tensor`` type for jit compiler

    .. note::
        Returning ``tensor.shape`` of ``tensor.size()`` directly is not torchscript
        compatible as return type would not be supported.

    Args:
        tensor (torch.Tensor): Tensor

    Returns:
        torch.Tensor: Shape of ``tensor``
    """
    return torch.tensor(tensor.shape)


def has_arg(fn, name):
    """Checks if a callable accepts a given keyword argument.

    Args:
        fn (callable): Callable to inspect.
        name (str): Check if ``fn`` can be called with ``name`` as a keyword
            argument.

    Returns:
        bool: whether ``fn`` accepts a ``name`` keyword argument.
    """
    signature = inspect.signature(fn)
    parameter = signature.parameters.get(name)
    if parameter is None:
        return False
    return parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def z_norm(x, dims: List[int], eps: float = 1e-8):
    mean = x.mean(dim=dims, keepdim=True)
    var2 = torch.var(x, dim=dims, keepdim=True, unbiased=False)
    value = (x - mean) / torch.sqrt((var2 + eps))
    return value


@script_if_tracing
def _glob_norm(x, eps: float = 1e-8):
    dims: List[int] = torch.arange(1, len(x.shape)).tolist()
    return z_norm(x, dims, eps)


class _LayerNorm(nn.Module):
    """Layer Normalization base class."""

    def __init__(self, channel_size):
        super(_LayerNorm, self).__init__()
        self.channel_size = channel_size
        self.gamma = nn.Parameter(torch.ones(channel_size), requires_grad=True)
        self.beta = nn.Parameter(torch.zeros(channel_size), requires_grad=True)

    def apply_gain_and_bias(self, normed_x):
        """Assumes input of size `[batch, chanel, *]`."""
        return (self.gamma * normed_x.transpose(1, -1) + self.beta).transpose(1, -1)


class GlobLN(_LayerNorm):
    """Global Layer Normalization (globLN)."""

    def forward(self, x, EPS: float = 1e-8):
        """Applies forward pass.

        Works for any input size > 2D.

        Args:
            x (:class:`torch.Tensor`): Shape `[batch, chan, *]`

        Returns:
            :class:`torch.Tensor`: gLN_x `[batch, chan, *]`
        """
        value = _glob_norm(x, eps=EPS)
        return self.apply_gain_and_bias(value)


class _Chop1d(nn.Module):
    """To ensure the output length is the same as the input."""

    def __init__(self, chop_size):
        super().__init__()
        self.chop_size = chop_size

    def forward(self, x):
        return x[..., : -self.chop_size].contiguous()


class Conv1DBlock(nn.Module):
    """One dimensional convolutional block, as proposed in [1].

    Args:
        in_chan (int): Number of input channels.
        hid_chan (int): Number of hidden channels in the depth-wise
            convolution.
        skip_out_chan (int): Number of channels in the skip convolution.
            If 0 or None, `Conv1DBlock` won't have any skip connections.
            Corresponds to the the block in v1 or the paper. The `forward`
            return res instead of [res, skip] in this case.
        kernel_size (int): Size of the depth-wise convolutional kernel.
        padding (int): Padding of the depth-wise convolution.
        dilation (int): Dilation of the depth-wise convolution.
        norm_type (str, optional): Type of normalization to use. To choose from

            -  ``'gLN'``: global Layernorm.
            -  ``'cLN'``: channelwise Layernorm.
            -  ``'cgLN'``: cumulative global Layernorm.
            -  Any norm supported by :func:`~.norms.get`
        causal (bool, optional) : Whether or not the convolutions are causal


    References
        [1] : "Conv-TasNet: Surpassing ideal time-frequency magnitude masking
        for speech separation" TASLP 2019 Yi Luo, Nima Mesgarani
        https://arxiv.org/abs/1809.07454
    """

    def __init__(
        self,
        in_chan,
        hid_chan,
        skip_out_chan,
        kernel_size,
        padding,
        dilation,
        norm_type="gLN",
        causal=False,
    ):
        super(Conv1DBlock, self).__init__()
        self.skip_out_chan = skip_out_chan
        conv_norm = norms.get(norm_type)
        in_conv1d = nn.Conv1d(in_chan, hid_chan, 1)
        depth_conv1d = nn.Conv1d(
            hid_chan, hid_chan, kernel_size, padding=padding, dilation=dilation, groups=hid_chan
        )
        if causal:
            depth_conv1d = nn.Sequential(depth_conv1d, _Chop1d(padding))
        self.shared_block = nn.Sequential(
            in_conv1d,
            nn.PReLU(),
            conv_norm(hid_chan),
            depth_conv1d,
            nn.PReLU(),
            conv_norm(hid_chan),
        )
        self.res_conv = nn.Conv1d(hid_chan, in_chan, 1)
        if skip_out_chan:
            self.skip_conv = nn.Conv1d(hid_chan, skip_out_chan, 1)

    def forward(self, x):
        r"""Input shape $(batch, feats, seq)$."""
        shared_out = self.shared_block(x)
        res_out = self.res_conv(shared_out)
        if not self.skip_out_chan:
            return res_out
        skip_out = self.skip_conv(shared_out)
        return res_out, skip_out


class TDConvNet(nn.Module):
    """Temporal Convolutional network used in ConvTasnet.

    Args:
        in_chan (int): Number of input filters.
        n_src (int): Number of masks to estimate.
        out_chan (int, optional): Number of bins in the estimated masks.
            If ``None``, `out_chan = in_chan`.
        n_blocks (int, optional): Number of convolutional blocks in each
            repeat. Defaults to 8.
        n_repeats (int, optional): Number of repeats. Defaults to 3.
        bn_chan (int, optional): Number of channels after the bottleneck.
        hid_chan (int, optional): Number of channels in the convolutional
            blocks.
        skip_chan (int, optional): Number of channels in the skip connections.
            If 0 or None, TDConvNet won't have any skip connections and the
            masks will be computed from the residual output.
            Corresponds to the ConvTasnet architecture in v1 or the paper.
        conv_kernel_size (int, optional): Kernel size in convolutional blocks.
        norm_type (str, optional): To choose from ``'BN'``, ``'gLN'``,
            ``'cLN'``.
        mask_act (str, optional): Which non-linear function to generate mask.
        causal (bool, optional) : Whether or not the convolutions are causal.

    References
        [1] : "Conv-TasNet: Surpassing ideal time-frequency magnitude masking
        for speech separation" TASLP 2019 Yi Luo, Nima Mesgarani
        https://arxiv.org/abs/1809.07454
    """

    def __init__(
        self,
        in_chan,
        n_src,
        out_chan=None,
        n_blocks=8,
        n_repeats=3,
        bn_chan=128,
        hid_chan=512,
        skip_chan=128,
        conv_kernel_size=3,
        norm_type="gLN",
        mask_act="relu",
        causal=False,
    ):
        super(TDConvNet, self).__init__()
        self.in_chan = in_chan
        self.n_src = n_src
        out_chan = out_chan if out_chan else in_chan
        self.out_chan = out_chan
        self.n_blocks = n_blocks
        self.n_repeats = n_repeats
        self.bn_chan = bn_chan
        self.hid_chan = hid_chan
        self.skip_chan = skip_chan
        self.conv_kernel_size = conv_kernel_size
        self.norm_type = norm_type
        self.mask_act = mask_act
        self.causal = causal

        layer_norm = norms.get(norm_type)(in_chan)
        bottleneck_conv = nn.Conv1d(in_chan, bn_chan, 1)
        self.bottleneck = nn.Sequential(layer_norm, bottleneck_conv)
        # Succession of Conv1DBlock with exponentially increasing dilation.
        self.TCN = nn.ModuleList()
        for r in range(n_repeats):
            for x in range(n_blocks):
                if not causal:
                    padding = (conv_kernel_size - 1) * 2**x // 2
                else:
                    padding = (conv_kernel_size - 1) * 2**x
                self.TCN.append(
                    Conv1DBlock(
                        bn_chan,
                        hid_chan,
                        skip_chan,
                        conv_kernel_size,
                        padding=padding,
                        dilation=2**x,
                        norm_type=norm_type,
                        causal=causal,
                    )
                )
        mask_conv_inp = skip_chan if skip_chan else bn_chan
        mask_conv = nn.Conv1d(mask_conv_inp, n_src * out_chan, 1)
        self.mask_net = nn.Sequential(nn.PReLU(), mask_conv)
        # Get activation function.
        mask_nl_class = activations.get(mask_act)
        # For softmax, feed the source dimension.
        if has_arg(mask_nl_class, "dim"):
            self.output_act = mask_nl_class(dim=1)
        else:
            self.output_act = mask_nl_class()

    def forward(self, mixture_w):
        r"""Forward.

        Args:
            mixture_w (:class:`torch.Tensor`): Tensor of shape $(batch, nfilters, nframes)$

        Returns:
            :class:`torch.Tensor`: estimated mask of shape $(batch, nsrc, nfilters, nframes)$
        """
        batch, _, n_frames = mixture_w.size()
        output = self.bottleneck(mixture_w)
        skip_connection = torch.tensor([0.0], device=output.device)
        for layer in self.TCN:
            # Common to w. skip and w.o skip architectures
            tcn_out = layer(output)
            if self.skip_chan:
                residual, skip = tcn_out
                skip_connection = skip_connection + skip
            else:
                residual = tcn_out
            output = output + residual
        # Use residual output when no skip connection
        mask_inp = skip_connection if self.skip_chan else output
        score = self.mask_net(mask_inp)
        score = score.view(batch, self.n_src, self.out_chan, n_frames)
        est_mask = self.output_act(score)
        return est_mask

    def get_config(self):
        config = {
            "in_chan": self.in_chan,
            "out_chan": self.out_chan,
            "bn_chan": self.bn_chan,
            "hid_chan": self.hid_chan,
            "skip_chan": self.skip_chan,
            "conv_kernel_size": self.conv_kernel_size,
            "n_blocks": self.n_blocks,
            "n_repeats": self.n_repeats,
            "n_src": self.n_src,
            "norm_type": self.norm_type,
            "mask_act": self.mask_act,
            "causal": self.causal,
        }
        return config


@script_if_tracing
def _unsqueeze_to_3d(x):
    """Normalize shape of `x` to [batch, n_chan, time]."""
    if x.ndim == 1:
        return x.reshape(1, 1, -1)
    elif x.ndim == 2:
        return x.unsqueeze(1)
    else:
        return x


@script_if_tracing
def _shape_reconstructed(reconstructed, size):
    """Reshape `reconstructed` to have same size as `size`

    Args:
        reconstructed (torch.Tensor): Reconstructed waveform
        size (torch.Tensor): Size of desired waveform

    Returns:
        torch.Tensor: Reshaped waveform

    """
    if len(size) == 1:
        return reconstructed.squeeze(0)
    return reconstructed


class MixtureConsistent(nn.Module):
    """Encoder / masker / decoder whose two outputs are made to sum to the input.

    Wisdom et al., "Differentiable consistency constraints for improved deep
    speech enhancement", ICASSP 2019: share whatever the estimates failed to
    account for equally between them. This is why the two stems add back up to
    the recording, so soloing and muting behave.

    Replaces asteroid's BaseEncoderMaskerDecoder, whose base class reaches for
    huggingface_hub at import time.
    """

    def __init__(self, encoder, masker, decoder, encoder_activation=None):
        super().__init__()
        self.encoder = encoder
        self.masker = masker
        self.decoder = decoder
        self.enc_activation = activations.get(encoder_activation or "linear")()

    def forward(self, wav):
        shape = jitable_shape(wav)
        wav = _unsqueeze_to_3d(wav)
        tf = self.enc_activation(self.encoder(wav))
        masked = self.masker(tf) * tf.unsqueeze(1)
        out = _shape_reconstructed(pad_x_to_y(self.decoder(masked), wav), shape)
        return out + (wav - out.sum(dim=1, keepdim=True)) / out.shape[1]


def build_from_config(cfg):
    """Build the separator a checkpoint's own config describes."""
    from asteroid_filterbanks import make_enc_dec

    enc, dec = make_enc_dec(
        "torch_stft",
        n_filters=cfg["nfft"],
        kernel_size=cfg["nfft"],
        stride=cfg["nhop"],
        sample_rate=cfg["sample_rate"],
    )
    masker = TDConvNet(
        in_chan=enc.n_feats_out,
        n_src=cfg["n_src"],
        out_chan=None,
        n_blocks=cfg["n_blocks"],
        n_repeats=cfg["n_repeats"],
        bn_chan=cfg["bn_chan"],
        hid_chan=cfg["hid_chan"],
        skip_chan=cfg["skip_chan"],
        mask_act=cfg["mask_act"],
    )
    return MixtureConsistent(enc, masker, dec, encoder_activation=cfg["encoder_activation"])
