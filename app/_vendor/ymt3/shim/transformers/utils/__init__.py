# StemDeck stand-in for part of Hugging Face transformers 4.45.1
# (Apache-2.0, Copyright 2018- The Hugging Face team; licence text in
# app/_vendor/ymt3/LICENSE.transformers): only what the vendored YourMT3+ inference code
# in app/_vendor/ymt3/src imports, so neither transformers nor its dependencies are needed.
# Written for StemDeck: a small rewrite, no code of the original.
# See app/_vendor/ymt3/README.md and manifest.json for the source hashes.

from dataclasses import dataclass
from ..modeling_outputs import ModelOutput
from . import logging
