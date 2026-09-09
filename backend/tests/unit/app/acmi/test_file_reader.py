"""Tests for reading ACMI content from plain and zip-wrapped files."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.acmi.file_reader import iter_acmi_lines

SAMPLE_TEXT = (
    "FileType=text/acmi/tacview\nFileVersion=2.2\n#1.50\n101,T=41.6|41.5|100,Type=Air+FixedWing\n"
)


def _write_zip(path: Path, member_name: str = "mission.acmi") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member_name, SAMPLE_TEXT)


def test_iter_lines_plain_file(tmp_path: Path) -> None:
    target = tmp_path / "plain.acmi"
    _ = target.write_text(SAMPLE_TEXT, encoding="utf-8")
    assert list(iter_acmi_lines(target)) == SAMPLE_TEXT.splitlines()


def test_iter_lines_plain_file_with_bom(tmp_path: Path) -> None:
    target = tmp_path / "bom.acmi"
    _ = target.write_text(SAMPLE_TEXT, encoding="utf-8-sig")
    lines = list(iter_acmi_lines(target))
    assert lines[0] == "FileType=text/acmi/tacview"


def test_iter_lines_zip_container(tmp_path: Path) -> None:
    target = tmp_path / "wrapped.acmi.zip"
    _write_zip(target)
    lines = list(iter_acmi_lines(target))
    assert lines == SAMPLE_TEXT.splitlines()
