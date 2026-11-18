"""Original 16x16 colour icons for Classetta's chrome, drawn in-repo.

The art is plain pixel data: each icon is a list of sixteen rows, one
character per pixel, looked up in PALETTE; '.' is transparent.  Nothing
else is read at draw time and no external asset ships.

Artwork and the licence that covers it live together here; see
LICENSE.txt in this directory.
"""

from functools import cache

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap

PALETTE = {
    '.': None,                # transparent
    'K': QColor('#1A1A1A'),   # outline, near-black
    'W': QColor('#FFFFFF'),
    'L': QColor('#E4E4E4'),   # light gray
    'G': QColor('#9C9C9C'),   # mid gray
    'D': QColor('#4F4F4F'),   # dark gray
    'M': QColor('#D9D2BE'),   # disk beige
    'S': QColor('#B8AF98'),   # disk beige shade
    'B': QColor('#2E4A9E'),   # blue (rules)
    'b': QColor('#7A90C8'),   # light blue
    'F': QColor('#F6D878'),   # folder body
    'f': QColor('#D9B45C'),   # folder shade
    'h': QColor('#FFF2BC'),   # folder highlight
    'P': QColor('#FFFFFF'),   # page
    'p': QColor('#D8D8D8'),   # page dog-ear shade
    'l': QColor('#B9B9B9'),   # page text lines
    'A': QColor('#B06030'),   # archive body
    'a': QColor('#7A3F1F'),   # archive dark
    'T': QColor('#E8C878'),   # archive tape
    'C': QColor('#4A5568'),   # catalog slate
    'c': QColor('#2F3A4A'),   # catalog dark slate
    'q': QColor('#FFFFFF'),   # catalog card
    'r': QColor('#3A5AA0'),   # catalog card rules
}

ART = {
    # A hard disk, beige, with a blue-ruled label.
    'disk': [
        "................",
        "................",
        "..KKKKKKKKKKKK..",
        "..KMLLLLLLLLMK..",
        "..KMLLLLLLLLMK..",
        "..KMKKKKKKKKMK..",
        "..KMKBBBBBBKMK..",
        "..KMKbbbbbbKMK..",
        "..KMKBBBBBBKMK..",
        "..KMKKKKKKKKMK..",
        "..KMMMMMMMMMMK..",
        "..KMMMGGGGMMMK..",
        "..KSSSSSSSSSSK..",
        "..KKKKKKKKKKKK..",
        "................",
        "................",
    ],
    # A classic Mac folder.
    'folder': [
        "................",
        "................",
        ".KKKKKK.........",
        ".KFFFFK.........",
        ".KFFFFKKKKKKKKK.",
        ".KhhhhhhhhhhhhK.",
        ".KFFFFFFFFFFFFK.",
        ".KFFFFFFFFFFFFK.",
        ".KFFFFFFFFFFFFK.",
        ".KFFFFFFFFFFFFK.",
        ".KFFFFFFFFFFFFK.",
        ".KFFFFFFFFFFFFK.",
        ".KffffffffffffK.",
        ".KKKKKKKKKKKKKK.",
        "................",
        "................",
    ],
    # A box with packing tape across it.
    'archive': [
        "................",
        "................",
        "................",
        "..KKKKKKKKKKKK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KAAATaaTAAAK..",
        "..KaaaTaaTaaaK..",
        "..KKKKKKKKKKKK..",
        "................",
        "................",
    ],
    # A page with four grey text lines.
    'document': [
        "................",
        "................",
        "...KKKKKKKK.....",
        "...KPPPPPPK.....",
        "...KPPPPPPK.....",
        "...KPllllPK.....",
        "...KPPPPPPK.....",
        "...KPllllPK.....",
        "...KPPPPPPK.....",
        "...KPllllPK.....",
        "...KPPPPPPK.....",
        "...KPllllPK.....",
        "...KPPPPPPK.....",
        "...KKKKKKKK.....",
        "................",
        "................",
    ],
    # A cut gem: the generic application diamond, highlight up the
    # middle; the colour counterpart of theme.GENERIC_APP_ICON.
    'app': [
        "................",
        ".......KK.......",
        "......KbbK......",
        ".....KbWWbK.....",
        "....KbbWbbbK....",
        "...KbbWWWWbbK...",
        "..KbBBWWWWBBbK..",
        ".KbBBBBWWBBBBbK.",
        ".KbBBBBBBBBBBbK.",
        "..KbBBWWWWBBbK..",
        "...KbbWWWWbbK...",
        "....KbbWbbbK....",
        ".....KbWWbK.....",
        "......KbbK......",
        ".......KK.......",
        "................",
    ],
    # A slate card box with white cards and blue rules; the Disk Tracker
    # catalog icon.
    'catalog': [
        "................",
        "................",
        ".KKKKKKKKKKKKKK.",
        ".KCCCCCCCCCCCCK.",
        ".KCqrrrrrrqqqCK.",
        ".KCCCCCCCCCCCCK.",
        ".KCCCCCCCCCCCCK.",
        ".KCqrrrrrrqqqCK.",
        ".KCCCCCCCCCCCCK.",
        ".KCCCCCCCCCCCCK.",
        ".KCqrrrrrrqqqCK.",
        ".KccccccccccccK.",
        ".KccccccccccccK.",
        ".KKKKKKKKKKKKKK.",
        "................",
        "................",
    ],
}


def _check(rows: list[str]) -> list[str]:
    if len(rows) != 16:
        raise ValueError(f"a colour icon's art is 16 rows, not {len(rows)}")
    for i, row in enumerate(rows):
        if len(row) != 16:
            raise ValueError(f"row {i} is {len(row)} pixels wide, not 16")
        for ch in row:
            if ch not in PALETTE:
                raise ValueError(f"row {i} uses {ch!r}, which is not in PALETTE")
    return rows


@cache
def image(kind: str) -> QImage:
    """The icon as a 16x16 ARGB image; transparency where the art says '.'"""
    rows = _check(ART[kind])
    img = QImage(16, 16, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            color = PALETTE[ch]
            if color is not None:
                img.setPixelColor(x, y, color)
    return img


@cache
def pixmap(kind: str) -> QPixmap:
    """The icon as a 16x16 pixmap, for title bars and row icons."""
    return QPixmap.fromImage(image(kind))
