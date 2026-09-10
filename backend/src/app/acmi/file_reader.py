"""Utilities to read ACMI content from files.

Real-time telemetry streams are uncompressed by specification, but ACMI
files on disk may be wrapped in a zip container (``.acmi.zip``). These
helpers let future tasks (e.g. re-evaluation of stored raw approach data,
FR-7) feed file content through the same parser used for live streams.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path


def iter_acmi_lines(path: str | Path) -> Iterator[str]:
    """Yield ACMI text lines from a plain or zip-wrapped ACMI file.

    - Plain files are decoded as UTF-8 (BOM tolerated).
    - ``.zip`` containers are opened and every member file is yielded in
      archive order.

    Note: 7z containers mentioned by the ACMI specification are not
    supported by the standard library; use a converted zip instead.
    """
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                with archive.open(member) as raw:
                    # newline=None enables universal newlines so callers get
                    # clean lines regardless of LF/CRLF in the archive.
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig")
                    for line in text:
                        stripped = line.rstrip("\r\n")
                        if stripped:
                            yield stripped
                    text.detach()  # avoid closing the shared raw stream twice
    else:
        with open(path, encoding="utf-8-sig") as text:
            for line in text:
                stripped = line.rstrip("\r\n")
                if stripped:
                    yield stripped
