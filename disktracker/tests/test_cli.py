"""The command-line text exports show a file's nesting."""

import sys
from pathlib import Path

import pytest

from disktracker.disktracker import Item, HEADER_SIZE
from disktracker.writer import write_dtc
from disktracker.__main__ import main


def _catalog(tmp_path: Path) -> Path:
    items = [
        Item(kind='disk', name='TestDisk', offset=HEADER_SIZE, items=2),
        Item(kind='folder', name='Docs', depth=1, items=1, descendants=1),
        Item(kind='file', name='notes.txt', depth=2, size=1200,
             type=b'TEXT', creator=b'ttxt'),
        Item(kind='file', name='top.txt', depth=1, size=50),
    ]
    src = tmp_path / 't.dtc'
    src.write_bytes(write_dtc(items))
    return src


def test_parse_text_export_indents_by_depth(tmp_path, monkeypatch):
    src = _catalog(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['disktracker', 'parse', str(src)])
    main()
    lines = (tmp_path / 't.dtc.parsed.txt').read_text().splitlines()
    assert lines[0].startswith('name\t')
    assert lines[1].startswith('TestDisk\t')          # the disk record: no indent
    assert lines[2].startswith('Docs\t')              # depth 1: no indent
    assert lines[3].startswith('  notes.txt\t')       # depth 2: one level of spaces
    assert lines[4].startswith('top.txt\t')


def test_totxt_export_indents_by_depth(tmp_path, monkeypatch):
    src = _catalog(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['disktracker', '--totxt', str(src)])
    main()
    lines = src.with_suffix('.dtc.txt').read_text().splitlines()
    assert lines[0].startswith('Name\t')
    assert not lines[1].startswith(' ')
    assert not lines[2].startswith(' ')
    assert lines[3].startswith('  notes.txt\t')
    assert not lines[4].startswith(' ')
