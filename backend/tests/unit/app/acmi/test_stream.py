"""Unit tests for ACMI stream line and compression decoding."""

from __future__ import annotations

import gzip
import zlib
from typing import Callable

import pytest

from app.acmi.stream import LineAssembler, StreamDecodeError, StreamDecoder

ACMI_TEXT = (
    "FileType=text/acmi/tacview\nFileVersion=2.2\n#1.50\n101,T=41.6|41.5|100,Type=Air+FixedWing\n"
)


def test_line_assembler_reassembles_split_chunks() -> None:
    assembler = LineAssembler()
    assert assembler.feed("FileType=text/acmi") == []
    assert assembler.feed("/tacview\nFileVer") == ["FileType=text/acmi/tacview"]
    assert assembler.feed("sion=2.2\n101,T=1|2|3\n") == ["FileVersion=2.2", "101,T=1|2|3"]
    assert assembler.flush() is None


def test_line_assembler_handles_crlf() -> None:
    assert LineAssembler().feed("line1\r\nline2\r\n") == ["line1", "line2"]


def test_line_assembler_flush_returns_partial_tail() -> None:
    assembler = LineAssembler()
    assert assembler.feed("complete\npartial-without-newline") == ["complete"]
    assert assembler.flush() == "partial-without-newline"
    assert assembler.flush() is None


def test_line_assembler_empty_lines_preserved() -> None:
    assert LineAssembler().feed("\n\nx\n") == ["", "", "x"]


def _feed_in_chunks(decoder: StreamDecoder, payload: bytes, size: int) -> str:
    return "".join(decoder.feed(payload[i : i + size]) for i in range(0, len(payload), size))


def plain(data: bytes) -> bytes:
    return data


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
    return compressor.compress(data) + compressor.flush()


def deflate_compress(data: bytes) -> bytes:
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
    )
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize(
    "compress",
    [
        pytest.param(plain, id="plain"),
        pytest.param(gzip.compress, id="gzip"),
        pytest.param(zlib.compress, id="zlib"),
        pytest.param(raw_deflate, id="raw-deflate"),
    ],
)
def test_stream_decoder_detects_and_decodes(compress: Callable[[bytes], bytes]) -> None:
    decoder = StreamDecoder()
    assert _feed_in_chunks(decoder, compress(ACMI_TEXT.encode()), 7) == ACMI_TEXT


def test_stream_decoder_plain_text_commits_early() -> None:
    decoder = StreamDecoder()
    assert decoder.feed(b"FileType=text/ac") == "FileType=text/ac"
    assert decoder.feed(b"mi/tacview\n") == "mi/tacview\n"
    assert decoder.flush() == ""


def test_stream_decoder_binary_junk_falls_back_to_plain() -> None:
    decoder = StreamDecoder()
    junk = b"\x07" * 600
    assert decoder.feed(junk) == junk.decode("utf-8", errors="replace")


def test_stream_decoder_corrupt_gzip_raises() -> None:
    with pytest.raises(StreamDecodeError):
        _ = StreamDecoder().feed(b"\x1f\x8b" + b"\xff" * 32)


def test_stream_decoder_deflate_corrupted_midstream_raises() -> None:
    payload = (lambda c: c.compress(ACMI_TEXT.encode()) + c.flush())(
        zlib.compressobj(9, zlib.DEFLATED, -15)
    )
    decoder = StreamDecoder()
    assert decoder.feed(payload[:12]) == "FileType=te"
    with pytest.raises(StreamDecodeError):
        _ = decoder.feed(bytes(b ^ 0xA5 for b in payload[12:]))
