"""Text normalisation and matching variants for the input rules (FR-001, research §2).

``normalize`` is what the service stores and the model sees: NFKC (full-width and compatibility
forms fold to plain ones), then every format character (``Cf``: zero-width, bidi controls, BOM)
and every control character except ``\\n`` and ``\\t`` dropped. ``match_variants`` adds the forms
the rules are matched against, so an obfuscated attack hits the same rule as the plain one:
casefolded, de-leeted, separator-squashed (``i.g.n.o.r.e`` → ``ignore``) and bounded base64/hex
decodes. Decoding is bounded (runs of at least ``DECODE_MIN_RUN`` characters, strict UTF-8,
``DECODE_MAX_CHARS`` decoded characters in all); a run that does not decode stays plain text.
Standard library only.
"""

import base64
import binascii
import re
import unicodedata
from typing import Final

DECODE_MIN_RUN: Final = 16
"""Shortest base64 or hex run worth decoding."""

DECODE_MAX_CHARS: Final = 2000
"""Most decoded characters ``match_variants`` adds for one text."""

_KEPT_CONTROLS: Final = frozenset("\n\t")

_LEET: Final = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)

# Three or more single letters/digits joined by short separator runs: "i.g.n.o.r.e", "s y s".
_SPACED: Final = re.compile(r"(?<![^\W_])[^\W_](?:[\s.\-_*|/+·•]{1,3}[^\W_](?![^\W_])){2,}")
_ALNUM: Final = re.compile(r"[^\W_]")

_BASE64_RUN: Final = re.compile(rf"[A-Za-z0-9+/]{{{DECODE_MIN_RUN},}}={{0,2}}")
_HEX_RUN: Final = re.compile(rf"\b(?:[0-9a-fA-F]{{2}}){{{DECODE_MIN_RUN // 2},}}\b")


def normalize(text: str) -> str:
    """NFKC, without format characters and without control characters except newline and tab."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if _kept(ch))


def match_variants(text: str) -> tuple[str, ...]:
    """Distinct forms of ``text`` to match rules against, the casefolded normal form first."""
    normal = normalize(text)
    plain = normal.casefold()
    deleeted = plain.translate(_LEET)
    variants = [plain, deleeted, _squash(plain), _squash(deleeted), *_decodes(normal)]
    return tuple(dict.fromkeys(variants))


def _kept(ch: str) -> bool:
    """Whether a normalised character survives: no ``Cf``, no ``Cc`` but newline and tab."""
    category = unicodedata.category(ch)
    if category == "Cf":
        return False
    return category != "Cc" or ch in _KEPT_CONTROLS


def _squash(text: str) -> str:
    """Join single characters spread out by separators (``i.g.n.o.r.e`` → ``ignore``)."""
    return _SPACED.sub(lambda match: "".join(_ALNUM.findall(match.group())), text)


def _decodes(text: str) -> list[str]:
    """Casefolded normal forms of the hex and base64 runs that decode to text, bounded."""
    found: list[str] = []
    budget = DECODE_MAX_CHARS
    runs = [(m.start(), m.group(), _from_hex) for m in _HEX_RUN.finditer(text)]
    runs += [(m.start(), m.group(), _from_base64) for m in _BASE64_RUN.finditer(text)]
    for _, run, decode in sorted(runs, key=lambda item: item[0]):
        decoded = decode(run)
        if decoded is None:
            continue
        decoded = normalize(decoded).casefold()[:budget]
        if decoded:
            found.append(decoded)
            budget -= len(decoded)
        if budget <= 0:
            break
    return found


def _from_hex(run: str) -> str | None:
    """The run as hex-encoded UTF-8 text, or ``None``."""
    try:
        return _printable(bytes.fromhex(run).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _from_base64(run: str) -> str | None:
    """The run as base64-encoded UTF-8 text (padding optional), or ``None``."""
    body = run.rstrip("=")
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
        return _printable(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def _printable(text: str) -> str | None:
    """``text`` when every character is printable or whitespace (real text, not binary)."""
    if text and all(ch.isprintable() or ch.isspace() for ch in text):
        return text
    return None
