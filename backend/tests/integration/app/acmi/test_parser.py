"""Integration test for parsing ACMI lines from an archive."""

import zipfile

from app.acmi.file_reader import iter_acmi_lines
from app.acmi.parser import AcmiParser


def test_parser_consumes_zip_content_end_to_end(tmp_path) -> None:
    target = tmp_path / "wrapped.acmi.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            "mission.acmi",
            "FileType=text/acmi/tacview\nFileVersion=2.2\n#1.50\n"
            "101,T=41.6|41.5|100,Type=Air+FixedWing\n",
        )

    parser = AcmiParser()
    for line in iter_acmi_lines(target):
        parser.feed_line(line)

    obj = parser.objects["101"]
    assert obj.type == "Air+FixedWing"
    assert parser.time == 1.5
