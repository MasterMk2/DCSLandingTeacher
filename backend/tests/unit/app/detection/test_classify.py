"""Tests for detection object classification."""

from app.detection.classify import ObjectClass, classify_object_type


def test_classify_object_types() -> None:
    assert classify_object_type("Air+FixedWing") is ObjectClass.AIRCRAFT
    assert classify_object_type("Carrier+FixedWing") is ObjectClass.CARRIER
    assert classify_object_type("Sea+Watercraft+AircraftCarrier") is ObjectClass.CARRIER
    assert classify_object_type("Ground+Static+Aircraft") is ObjectClass.STATIC
    assert classify_object_type("Sea+Watercraft+Destroyer") is ObjectClass.OTHER
    assert classify_object_type(None) is ObjectClass.OTHER
