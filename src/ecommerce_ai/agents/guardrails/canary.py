"""The per-run canary rendered into the system prompt (FR-007, research §6).

``cnry-`` + 24 random hex characters, new for every run. Only the system prompt holds it, so an
answer containing it proves a prompt leak. It lives in the run's context only: never stored,
checkpointed or logged.
"""

import re
import secrets
from typing import Final

CANARY_PREFIX: Final = "cnry-"
CANARY_HEX_BYTES: Final = 12
CANARY_PATTERN: Final = re.compile(r"cnry-[0-9a-f]{24}")
"""What a canary looks like."""


def new_canary() -> str:
    """A fresh canary (``CanaryFactory``): ``cnry-`` and 24 random hex characters."""
    return CANARY_PREFIX + secrets.token_hex(CANARY_HEX_BYTES)
