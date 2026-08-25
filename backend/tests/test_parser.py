"""Unit tests for the ACMI 2.2 Text parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.acmi.models import (
    HeaderEvent,
    MissionEvent,
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
    events = parser.feed_line("0,ReferenceTime=2011-06-02T05:00:00Z")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].obj_id == "0"
    assert parser.header["ReferenceTime"] == "2011-06-02T05:00:00Z"


def test_frame_time_is_absolute_offset() -> None:
    """``#<seconds>`` is the absolute offset from ReferenceTime, not a delta

    to accumulate onto the previous value (real DCS streams send a slowly
    increasing absolute offset on every frame, e.g. #2211.13, #2211.35, ...;
    summing them would blow up to nonsensical values within minutes).
    """
    parser = AcmiParser()
    first = parser.feed_line("#47.13")
    second = parser.feed_line("#55.75")
    assert isinstance(first[0], TimeEvent) and first[0].time == pytest.approx(47.13)
    assert isinstance(second[0], TimeEvent) and second[0].time == pytest.approx(55.75)


def test_comments_and_blank_lines_ignored() -> None:
    parser = AcmiParser()
    assert parser.feed_line("// a comment") == []
    assert parser.feed_line("") == []
    assert parser.feed_line("   ") == []


def test_bom_on_first_line_is_stripped() -> None:
    parser = AcmiParser()
    events = parser.feed_line("\ufeffFileType=text/acmi/tacview")
    assert isinstance(events[0], HeaderEvent)
    assert events[0].value == "text/acmi/tacview"


def test_object_add_update_remove_from_fixture() -> None:
    parser = AcmiParser()
    updates: list[ObjectUpdateEvent] = []
    removals: list[ObjectRemoveEvent] = []

    # Feed line by line so we can inspect state before the removal.
    lines = load_sample().splitlines()
    consumed = 0
    for idx, line in enumerate(lines):
        consumed = idx + 1
        for event in parser.feed_line(line):
            if isinstance(event, ObjectUpdateEvent):
                updates.append(event)
                if event.obj_id == "102":
                    carrier = parser.objects["102"]
                    assert carrier.type == "Sea+Watercraft+AircraftCarrier"
                    assert carrier.name == "Kuznetsov"
            elif isinstance(event, ObjectRemoveEvent):
                removals.append(event)
        if removals:
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
    # First update at t=0, second at t=47.13, third at t=55.75 (each # line is
    # the absolute offset from ReferenceTime, not a delta onto the previous one).
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
    # Slot 9 is the projected/flat-world heading; it is offset from true
    # north by the meridian convergence and belongs with u/v, not lat/lon.
    assert obj.grid_heading == pytest.approx(185.3)
    # `heading` is the true heading used by the lat/lon-based graders, so it
    # must resolve to Yaw, not to the flat-world value.
    assert obj.heading == pytest.approx(180.0)


def test_object_coordinates_are_absolute_after_reference_offset() -> None:
    """ACMI stores object lat/lon relative to the global reference origin."""
    parser = AcmiParser()
    parser.feed_line("0,ReferenceLongitude=36,ReferenceLatitude=38")
    parser.feed_line("101,T=4.57|5.10|1500")
    obj = parser.objects["101"]
    assert obj.longitude == pytest.approx(40.57)
    assert obj.latitude == pytest.approx(43.10)


def test_reference_offset_applies_to_partial_transform_updates() -> None:
    """A later line updating only one axis must not lose or double the origin."""
    parser = AcmiParser()
    parser.feed_line("0,ReferenceLongitude=36,ReferenceLatitude=38")
    parser.feed_line("101,T=4.57|5.10|1500")
    parser.feed_line("101,T=4.58||1400")
    obj = parser.objects["101"]
    assert obj.longitude == pytest.approx(40.58)
    assert obj.latitude == pytest.approx(43.10)  # unchanged, still absolute
    assert obj.altitude == pytest.approx(1400.0)


def test_reference_offset_leaves_native_uv_untouched() -> None:
    """U/V are already absolute in the simulator's flat world."""
    parser = AcmiParser()
    parser.feed_line("0,ReferenceLongitude=36,ReferenceLatitude=38")
    parser.feed_line("101,T=4.57|5.10|1500|0|0|90|517583.7|-198841.6|154.6")
    obj = parser.objects["101"]
    assert obj.u == pytest.approx(517583.7)
    assert obj.v == pytest.approx(-198841.6)


def test_heading_falls_back_to_flat_world_when_yaw_absent() -> None:
    parser = AcmiParser()
    parser.feed_line("101,T=-129|43|1500,HDG=77.5")
    obj = parser.objects["101"]
    assert obj.yaw is None
    assert obj.heading == pytest.approx(77.5)


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
    events = parser.feed_line("101,Comments=Touchdown\\, smooth arrival,Name=A")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].properties["Comments"] == "Touchdown, smooth arrival"
    assert updates[0].properties["Name"] == "A"


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


# ---------------------------------------------------------------------------
# Mission events (Event property)
# ---------------------------------------------------------------------------


def test_mission_event_bookmark() -> None:
    parser = AcmiParser()
    parser.feed_line("#8.62")
    events = parser.feed_line("0,Event=Bookmark|Starting precautionary landing practice")

    mission_events = [e for e in events if isinstance(e, MissionEvent)]
    assert len(mission_events) == 1
    event = mission_events[0]
    assert event.event_type == "Bookmark"
    assert event.object_ids == ()
    assert event.text == "Starting precautionary landing practice"
    assert event.time == pytest.approx(8.62)


def test_mission_event_with_object_id() -> None:
    parser = AcmiParser()
    parser.feed_line("#114.76")
    events = parser.feed_line("0,Event=Landed|705|Maverick has landed on the USS Ranger")

    landed = [e for e in events if isinstance(e, MissionEvent)][0]
    assert landed.event_type == "Landed"
    assert landed.object_ids == ("705",)
    assert landed.text == "Maverick has landed on the USS Ranger"


def test_mission_events_do_not_pollute_object_properties() -> None:
    parser = AcmiParser()
    parser.feed_line("0,Event=Bookmark|test")
    assert "Event" not in parser.header


def test_multiple_mission_events_on_one_line() -> None:
    parser = AcmiParser()
    events = parser.feed_line(
        "0,Event=Message|101|first,Event=Message|102|second"
    )
    mission_events = [e for e in events if isinstance(e, MissionEvent)]
    assert [e.text for e in mission_events] == ["first", "second"]
    assert [e.object_ids for e in mission_events] == [("101",), ("102",)]


def test_object_update_with_inline_event_yields_both() -> None:
    parser = AcmiParser()
    events = parser.feed_line("101,T=1|2|3,Event=Landed|101|touchdown")
    kinds = {type(e).__name__ for e in events}
    assert kinds == {"ObjectUpdateEvent", "MissionEvent"}


# ---------------------------------------------------------------------------
# Continuation lines (multiline string properties)
# ---------------------------------------------------------------------------


def test_continuation_line_simple() -> None:
    """A property value split across two physical lines with continuation backslash."""
    parser = AcmiParser()
    # First line ends with unescaped backslash
    events1 = parser.feed_line("0,Comments=First line of briefing\\")
    assert events1 == []  # Buffered, not yet parsed
    # Second line completes the value
    events2 = parser.feed_line("Second line of briefing")
    updates = [e for e in events2 if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].properties["Comments"] == "First line of briefingSecond line of briefing"


def test_continuation_line_multiple() -> None:
    """Multiple continuation lines chained together."""
    parser = AcmiParser()
    events1 = parser.feed_line("0,Comments=Line 1\\")
    assert events1 == []
    events2 = parser.feed_line("Line 2\\")
    assert events2 == []
    events3 = parser.feed_line("Line 3")
    updates = [e for e in events3 if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].properties["Comments"] == "Line 1Line 2Line 3"


def test_continuation_line_with_escaped_backslash() -> None:
    """A literal backslash at end of line (escaped) does NOT trigger continuation."""
    parser = AcmiParser()
    # Double backslash at end = literal backslash, not continuation
    events = parser.feed_line("0,Comments=Path with trailing backslash\\\\")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    # The value should contain a literal backslash, not be waiting for continuation
    assert updates[0].properties["Comments"] == "Path with trailing backslash\\"


def test_continuation_line_then_normal_line() -> None:
    """After a continuation sequence, normal parsing resumes."""
    parser = AcmiParser()
    parser.feed_line("0,Comments=Continued\\")
    events = parser.feed_line("value")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert updates[0].properties["Comments"] == "Continuedvalue"

    # Next line should be parsed normally
    events2 = parser.feed_line("101,T=1|2|3,Name=Test")
    updates2 = [e for e in events2 if isinstance(e, ObjectUpdateEvent)]
    assert len(updates2) == 1
    assert updates2[0].obj_id == "101"
    assert updates2[0].properties["Name"] == "Test"


def test_continuation_in_object_update() -> None:
    """Continuation lines work within object updates (not just global object)."""
    parser = AcmiParser()
    parser.feed_line("101,T=1|2|3,Comments=Start\\")
    events = parser.feed_line("End")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].obj_id == "101"
    assert updates[0].properties["Comments"] == "StartEnd"


def test_continuation_with_escaped_comma() -> None:
    """Escaped commas work correctly within continuation lines."""
    parser = AcmiParser()
    parser.feed_line("0,Comments=First part\\,\\")
    events = parser.feed_line("second part")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    # Escaped comma should be preserved as literal comma
    assert updates[0].properties["Comments"] == "First part,second part"


def test_continuation_line_with_frame_time_between() -> None:
    """Frame time lines are not part of continuation and break the sequence."""
    parser = AcmiParser()
    parser.feed_line("0,Comments=Start\\")
    # Frame time line should be processed normally (not buffered)
    time_events = parser.feed_line("#10.0")
    assert len(time_events) == 1
    assert time_events[0].time == 10.0
    # Continuation should resume after frame time
    events = parser.feed_line("End")
    updates = [e for e in events if isinstance(e, ObjectUpdateEvent)]
    assert len(updates) == 1
    assert updates[0].properties["Comments"] == "StartEnd"
