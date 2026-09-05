"""Source-splitting strategy, isolated so it can be swapped/improved later
without touching the loader/embedding pipeline.

Strategy: try to split on common ABAP block boundaries (METHOD/ENDMETHOD,
FORM/ENDFORM, FUNCTION/ENDFUNCTION). If none are detected, fall back to
fixed-size line chunks.
"""
from __future__ import annotations

import re

_BLOCK_PAIRS = [
    (re.compile(r"^\s*METHOD\b", re.IGNORECASE), re.compile(r"^\s*ENDMETHOD\b", re.IGNORECASE)),
    (re.compile(r"^\s*FORM\b", re.IGNORECASE), re.compile(r"^\s*ENDFORM\b", re.IGNORECASE)),
    (re.compile(r"^\s*FUNCTION\b", re.IGNORECASE), re.compile(r"^\s*ENDFUNCTION\b", re.IGNORECASE)),
]


def split_source_into_chunks(source: str, fallback_chunk_size_lines: int = 100) -> list[str]:
    """Split `source` into a list of non-empty text chunks."""
    if not source.strip():
        return []

    lines = source.splitlines()
    block_chunks = _split_on_block_boundaries(lines)
    if block_chunks:
        return block_chunks

    return _split_fixed_size(lines, fallback_chunk_size_lines)


def _split_on_block_boundaries(lines: list[str]) -> list[str] | None:
    open_re_by_pair_idx: dict[int, int] = {}
    boundaries: list[tuple[int, int]] = []  # (start_line_idx, end_line_idx) inclusive

    open_idx: int | None = None
    open_pair: int | None = None

    for i, line in enumerate(lines):
        if open_idx is None:
            for pair_idx, (open_re, _close_re) in enumerate(_BLOCK_PAIRS):
                if open_re.match(line):
                    open_idx = i
                    open_pair = pair_idx
                    break
        else:
            _open_re, close_re = _BLOCK_PAIRS[open_pair]
            if close_re.match(line):
                boundaries.append((open_idx, i))
                open_idx = None
                open_pair = None

    if not boundaries:
        return None

    chunks: list[str] = []
    prev_end = -1
    leading = "\n".join(lines[0:boundaries[0][0]]).strip()
    if leading:
        chunks.append(leading)

    for idx, (start, end) in enumerate(boundaries):
        chunks.append("\n".join(lines[start:end + 1]).strip())
        next_start = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        between = "\n".join(lines[end + 1:next_start]).strip()
        if between:
            chunks.append(between)

    return [c for c in chunks if c]


def _split_fixed_size(lines: list[str], chunk_size_lines: int) -> list[str]:
    chunks = []
    for i in range(0, len(lines), chunk_size_lines):
        chunk = "\n".join(lines[i:i + chunk_size_lines]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks
