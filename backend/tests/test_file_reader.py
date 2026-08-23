"""Tests for reading ACMI content from plain and zip-wrapped files."""

from __future__ import annotations

import zipfile

from app.acmi.file_reader import iter_acmi_lines
from app.acmi.parser import AcmiParser

SAMPLE_TEXT = (
    "FileType=text/acmi/tacview\n"
    "FileVersion=2.2\n"
    "#1.50\n"
    "101,T=41.6|41.5|100,Type=Air+FixedWing\n"
)


def _write_zip(path, member_name: str = "mission.acmi") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member_name, SAMPLE_TEXT)


def test_iter_lines_plain_file(tmp_path) -> None:
    target = tmp_path / "plain.acmi"
    target.write_text(SAMPLE_TEXT, encoding="utf-8")
    assert list(iter_acmi_lines(target)) == SAMPLE_TEXT.splitlines()


def test_iter_lines_plain_file_with_bom(tmp_path) -> None:
    target = tmp_path / "bom.acmi"
    target.write_text(SAMPLE_TEXT, encoding="utf-8-sig")
    lines = list(iter_acmi_lines(target))
    assert lines[0] == "FileType=text/acmi/tacview"


def test_iter_lines_zip_container(tmp_path) -> None:
    target = tmp_path / "wrapped.acmi.zip"
    _write_zip(target)
    lines = list(iter_acmi_lines(target))
    assert lines == SAMPLE_TEXT.splitlines()


def test_parser_consumes_zip_content_end_to_end(tmp_path) -> None:
    target = tmp_path / "wrapped.acmi.zip"
    _write_zip(target)

    parser = AcmiParser()
    for line in iter_acmi_lines(target):
        parser.feed_line(line)

    obj = parser.objects["101"]
    assert obj.type == "Air+FixedWing"
    assert parser.time == 1.5
