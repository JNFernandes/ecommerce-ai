"""Cut streamed answer text into segments the gatekeeper checks one by one (FR-016, research §10).

A segment ends at a sentence end (``.``, ``!`` or ``?`` followed by whitespace) or at a newline,
and keeps the whitespace that follows it, so the segments always concatenate back to the exact
text. A boundary only counts once a non-whitespace character follows it (the next delta may add
more whitespace), and only after the segment has some text, so no segment is empty or blank. A
``.`` inside a token (``10.50``, ``a1b2.c3``) is not a boundary. ``finish`` returns the rest.
"""

from typing import Final

_SENTENCE_ENDS: Final = frozenset(".!?")


class Segmenter:
    """Buffer text deltas and release complete segments."""

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        """Add ``delta``; return the segments it completed, in order (possibly none)."""
        self._buffer += delta
        segments: list[str] = []
        while (cut := self._cut()) is not None:
            segments.append(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
        return segments

    def finish(self) -> str | None:
        """Return the buffered rest at turn end, or ``None`` when it is blank; empty the buffer."""
        rest, self._buffer = self._buffer, ""
        return rest if rest.strip() else None

    def _cut(self) -> int | None:
        """Index where the first complete segment of the buffer ends, if one is complete."""
        text = self._buffer
        start = len(text) - len(text.lstrip())
        for index in range(start, len(text)):
            char = text[index]
            is_end = char == "\n" or (
                char in _SENTENCE_ENDS and index + 1 < len(text) and text[index + 1].isspace()
            )
            if not is_end:
                continue
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            return end if end < len(text) else None
        return None
