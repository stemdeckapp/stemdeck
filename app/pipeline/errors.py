"""Pipeline failure classification (#294) and the separation error type (#277).

"Audio processing failed. Please try again." is shown for out-of-memory, a
GPU fault, a bad input file, and a full disk alike. The classifier below maps
the captured stderr/exception text to a small set of user-meaningful causes so
the job can carry an actionable `error_detail` and the failed-job quarantine
can record what actually happened. Phase 2's GPU->CPU fallback reuses it.
"""

from __future__ import annotations


class SeparationError(RuntimeError):
    """Demucs (or a later separation pass) failed.

    Carries the stderr tail and the compute device so the runner's
    quarantine can preserve the evidence that the old error path threw away.
    """

    def __init__(
        self,
        message: str,
        *,
        tail: list[str] | None = None,
        device: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tail: list[str] = tail or []
        self.device = device


# Ordered, first match wins. Substring match against lowercased text.
# Deliberately coarse: these route a user (or a bug report) to the right
# next step, they don't diagnose. "unknown" is the honest default.
_CAUSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "out-of-memory",
        (
            "cuda out of memory",
            "mps backend out of memory",
            "not enough memory",
            "cannot allocate memory",
            "out of memory",
            "memoryerror",
        ),
    ),
    (
        "unsupported-device",
        (
            "no kernel image is available",
            "invalid device",
            "cuda driver version is insufficient",
            "cudnn error",
            "not currently implemented for the mps device",
            "torch not compiled with cuda",
        ),
    ),
    (
        "disk-full",
        (
            "no space left on device",
            "errno 28",
            "disk quota exceeded",
        ),
    ),
    (
        "bad-input",
        (
            "invalid data found",
            "could not open file",
            "unable to open",
            "no stems produced",
            "failed to read",
            "unsupported format",
            "ffmpeg transcode failed",
        ),
    ),
)


def classify_failure(text: str) -> str:
    """Map failure output to one of: out-of-memory, unsupported-device,
    disk-full, bad-input, unknown."""
    low = text.lower()
    for cause, patterns in _CAUSE_PATTERNS:
        if any(p in low for p in patterns):
            return cause
    return "unknown"
