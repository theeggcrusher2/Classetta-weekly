"""Small shared helpers kept free of heavy dependencies."""
from __future__ import annotations


def member_segments(name: str) -> list[str]:
    """The folders/files of an archive member name, read the way macextract's
    sanitizer reads them: back‑slashes and colons as separators, empty, '.'
    and '..' segments dropped, trailing dots stripped and NUL bytes removed.

    An empty list means nothing usable is left; callers refuse the member,
    as macextract refuses to extract it."""
    parts: list[str] = []
    for segment in name.replace("\\", "/").replace(":", "/").split("/"):
        segment = segment.strip().rstrip(".")
        if segment in ("", ".", ".."):
            continue
        segment = segment.replace("\0", "")
        if segment:
            parts.append(segment)
    return parts
