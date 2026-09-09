"""Recovery for model checkpoints that were cached incompletely.

Both model loaders StemDeck depends on treat a cached file's *existence* as
proof that it is usable, and neither checks that what landed is what was sent.

torch.hub, which is what beat_this loads through::

    if not os.path.exists(cached_file):
        download_url_to_file(url, cached_file, hash_prefix, progress=progress)

and `download_url_to_file` reads until the body ends, then moves the result
into place with no comparison against Content-Length. `check_hash` defaults to
False and beat_this does not pass it. audio-separator has the same shape, which
is why a bad file there surfaces as an MD5 that matches nothing.

So a connection that drops mid-body and closes cleanly is promoted to a
complete checkpoint, and every later run finds the file present and stops
there. The failure never heals on its own, and it is silent: beat detection
falls back to librosa (see app/pipeline/beat_detect.py) and the karaoke split
just reports itself unavailable. A user gets a permanently worse beat grid and
no reason to suspect why (#502).

The fix is to stop trusting existence. If a load fails and there was a cached
artifact to blame, remove it and try once.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger("stemdeck.modelcache")

# Artifacts already discarded once in this process. Healing is for a file that
# arrived damaged, which a single re-fetch settles either way; repeating it
# turns a permanently failing load into a ~100 MB download on every attempt.
_healed: set[Path] = set()

T = TypeVar("T")


def _remove(path: Path) -> bool:
    """Delete one cached artifact. True if something was actually removed."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return not path.exists()
        if path.exists():
            path.unlink()
            return True
    except OSError:
        # A file held open by another process, or a read-only cache dir. Not
        # worth failing the load over: the caller's own error is the real one.
        logger.warning("could not remove cached model artifact %s", path, exc_info=True)
    return False


def load_or_heal(load: Callable[[], T], stale: Callable[[], Iterable[Path]]) -> T:
    """Call ``load``; on failure drop the cached artifacts and call it once more.

    ``stale`` is a callable so that working out where the cache lives (which
    for torch means importing torch) only happens on the failure path.

    The retry is conditional on having actually removed something, and that is
    the point rather than an optimisation. If nothing was cached, the load
    failed for some other reason -- offline, missing dependency, no disk -- and
    a second attempt just pays the same network timeout again for the same
    answer. Only a cache we have now invalidated earns another try.

    An ImportError is never treated as a cache problem. The package is missing,
    and deleting model files would not help.
    """
    try:
        return load()
    except ImportError:
        raise
    except Exception as first:
        try:
            paths = list(stale())
        except Exception:
            logger.warning("could not resolve model cache paths", exc_info=True)
            raise first from None
        # Once per artifact per process. beat_this collapses every failure into
        # one ValueError, so a load that is broken for some reason a fresh copy
        # cannot fix looks exactly like a truncated one, and without this it
        # would delete and re-fetch ~100 MB on every attempt for as long as the
        # process lives.
        fresh = [p for p in paths if p not in _healed]
        if not fresh:
            raise
        removed = [p for p in fresh if _remove(p)]
        _healed.update(fresh)
        if not removed:
            raise
        logger.warning(
            "model load failed (%s); discarded %s and retrying once",
            first,
            ", ".join(str(p) for p in removed),
        )
        return load()


def beat_this_artifacts(checkpoint: str) -> list[Path]:
    """Where torch.hub keeps the beat_this checkpoint.

    Mirrors beat_this.inference.load_checkpoint, which asks torch.hub for
    ``beat_this-<name>.ckpt``. Kept next to the healing logic because the two
    have to agree: pointed at the wrong path this silently never heals
    anything.
    """
    import torch

    return [Path(torch.hub.get_dir()) / "checkpoints" / f"beat_this-{checkpoint}.ckpt"]


def vocal_split_artifacts(models_dir: Path, model_file: str) -> list[Path]:
    """The audio-separator model file, and the metadata indexes that name it.

    The indexes are listed too because audio-separator reports a truncated
    model as an unknown MD5, and that is a lookup against them rather than
    against the file itself. There are two, one per architecture family, and
    they are the names audio-separator actually writes: a single
    "model_data.json" is not one of them, so listing that instead heals the
    checkpoint and leaves the index that produced the error in place.
    """
    root = models_dir / "audio-separator"
    return [
        root / model_file,
        root / "vr_model_data.json",
        root / "mdx_model_data.json",
    ]
