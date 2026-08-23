"""Unit tests for the ACMI 2.2 Text parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.acmi.models import (
    HeaderEvent,
    ObjectRemoveEvent,
    ObjectUpdateEvent,
    TimeEvent,
)
from app.acmi.parser import AcmiParseError, AcmiParser, expand_transform

FIXTURES = Path(__file__).parent / "fixtures"


def load_sample() -> str:
    return (FIXTURES / "sample.acmi").read_text(encoding="utf-8")


def test_header_lines() -> None:
    parser = AcmiParser()
    events = parser.feed("FileType=text/acmi/tacview\nFileVersion=2.2\n")
    assert len(events) == 2
    assert isinstance(events[0], HeaderEvent)
    assert events[0].key == "FileType"
    assert events[0].value == "text/acmi/tacview"
    assert isinstance(events[1], HeaderEvent)
    assert events[1].key == "FileVersion"
    assert events[1].value == "2.2"
    assert parser.header["FileType"] == "text/acmi/tacview"


def test_global_object_metadata() -> None:
    parser = AcmiParser()
    event = parser.feed_line("0,ReferenceTime=2011-06-02T05:00:00Z")
    assert isinstance(event, ObjectUpdateEvent)
    assert event.obj_id == "0"
    assert parser.header["ReferenceTime"] == "2011-06-02T05:00:00Z"


def test_frame_time_accumulation() -> None:
    parser = AcmiParser()
    first = parser.feed_line("#47.13")
    second = parser.feed_line("#8.62")
    assert isinstance(first, TimeEvent) and first.time == pytest.approx(47.13)
    assert isinstance(second, TimeEvent) and second.time == pytest.approx(55.75)


def test_comments_and_blank_lines_ignored() -> None:
    parser = AcmiParser()
    assert parser.feed_line("// a comment") is None
    assert parser.feed_line("") is None
    assert parser.feed_line("   ") is None


def test_bom_on_first_line_is_stripped() -> None:
    parser = AcmiParser()
    event = parser.feed_line("\ufeffFileType=text/acmi/tacview")
    assert isinstance(event, HeaderEvent)
    assert event.value == "text/acmi/tacview"


def test_object_add_update_remove_from_fixture() -> None:
    parser = AcmiParser()
    updates: list[ObjectUpdateEvent] = []
    removals: list[ObjectRemoveEvent] = []

    # Feed line by line so we can inspect state before the removal.
    lines = load_sample().splitlines()
    consumed = 0
    for idx, line in enumerate(lines):
        consumed = idx + 1
        event = parser.feed_line(line)
        if isinstance(event, ObjectUpdateEvent):
            updates.append(event)
            if event.obj_id == "102":
                carrier = parser.objects["102"]
                assert carrier.type == "Sea+Watercraft+AircraftCarrier"
                assert carrier.name == "Kuznetsov"
        elif isinstance(event, ObjectRemoveEvent):
            removals.append(event)
            break

    # 3 global metadata lines + 2 object adds + 1 partial update before removal
    assert len(updates) == 6
    assert len(removals) == 1
    assert removals[0].obj_id == "102"

    # The removal line has been processed: the object is gone from live state.
    assert "102" not in parser.objects

    # Feed the remaining lines and verify the removal took effect and the
    # aircraft kept receiving partial updates.
    for line in lines[consumed:]:
        parser.feed_line(line)

    aircraft = parser.objects["101"]
    assert aircraft.type == "Air+FixedWing"
    assert aircraft.name == "C172"
    assert aircraft.pilot == "Viggen"
    assert aircraft.group == "Training"
    assert aircraft.country == "us"
    # First update at t=0, second at t=47.13, third at t=55.75
    assert aircraft.last_seen == pytest.approx(55.75)



def test_transform_full_syntax_typed_values() -> None:
    parser = AcmiParser()
    parser.feed_line(
        "101,T=41.6251307|41.5910417|2000.14|0|5|90,"
        "Type=Air+FixedWing,Name=C172"
    )
    obj = parser.objects["101"]
    assert obj.longitude == pytest.approx(41.6251307)
    assert obj.latitude == pytest.approx(41.5910417)
    assert obj.altitude == pytest.approx(2000.14)
    assert obj.roll == pytest.approx(0.0)
    assert obj.pitch == pytest.approx(5.0)
    assert obj.yaw == pytest.approx(90.0)


def test_transform_partial_update_keeps_previous_values() -> None:
    parser = AcmiParser()
    parser.feed_line("101,T=41.62|41.58|100|10|20|30")
    parser.feed_line("101,T=41.63||200|||91")

    obj = parser.objects["101"]
    # Updated fields
    assert obj.longitude == pytest.approx(41.63)
    assert obj.altitude == pytest.approx(200.0)
    assert obj.yaw == pytest.approx(91.0)
    # Omitted fields keep their previous values per the specification.
    assert obj.latitude == pytest.approx(41.58)
    assert obj.roll == pytest.approx(10.0)
    assert obj.pitch == pytest.approx(20.0)


def test_transform_syntax4_heading_flat_world() -> None:
    parser = AcmiParser()
    parser.feed_line("101,T=-129|43|1500|15|-5|180|1000|2000|185.3")
    obj = parser.objects["101"]
    assert obj.u == pytest.approx(1000.0)
    assert obj.v == pytest.approx(2000.0)
    assert obj.heading == pytest.approx(185.3)


def test_speed_prefers_tas_then_cas_then_ias() -> None:
    parser = AcmiParser()
    parser.feed_line("101,T=1|2|3,TAS=80,CAS=70,IAS=60")
    assert parser.objects["101"].speed == pytest.approx(80.0)

    parser.feed_line("102,T=1|2|3,CAS=70,IAS=60")
    assert parser.objects["102"].speed == pytest.approx(70.0)

    parser.feed_line("103,T=1|2|3,IAS=60")
    assert parser.objects["103"].speed == pytest.approx(60.0)


def test_on_ground_property() -> None:
    parser = AcmiParser()
    parser.feed_line("101,T=1|2|3,OnGround=0")
    assert parser.objects["101"].on_ground is False
    parser.feed_line("101,OnGround=1")
    assert parser.objects["101"].on_ground is True


def test_escaped_comma_in_value() -> None:
    parser = AcmiParser()
    event = parser.feed_line("101,Comments=Touchdown\\, smooth arrival,Name=A")
    assert isinstance(event, ObjectUpdateEvent)
    assert event.properties["Comments"] == "Touchdown, smooth arrival"
    assert event.properties["Name"] == "A"


def test_hexadecimal_ids_normalized_to_uppercase() -> None:
    parser = AcmiParser()
    parser.feed_line("a1b2,T=1|2|3")
    assert "A1B2" in parser.objects
    parser.feed_line("-A1B2")
    assert "A1B2" not in parser.objects


def test_invalid_time_line_raises() -> None:
    parser = AcmiParser()
    with pytest.raises(AcmiParseError):
        parser.feed_line("#not-a-number")


def test_expand_transform_empty_components() -> None:
    expanded = expand_transform("41.626||2000|||91")
    assert expanded == {
        "Longitude": "41.626",
        "Altitude": "2000",
        "Yaw": "91",
    }
