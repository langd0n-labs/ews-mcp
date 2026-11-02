"""Offline regression coverage for free/busy timezones and status codes."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from exchangelib import EWSDateTime, EWSTimeZone

from src.tools import calendar_tools as calendar
from test_calendar_draft_fixes import run, tool_instance


START = "2026-11-02T17:00:00-05:00"
END = "2026-11-02T18:00:00-05:00"


@pytest.fixture(autouse=True)
def eastern_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("TIMEZONE", "America/New_York")


def make_tool(cls, merged, events=()):
    request = Mock(return_value=[NS(merged=merged, calendar_events=events)])
    account = NS(primary_smtp_address="user@example.com",
                 protocol=NS(get_free_busy_info=request))
    return tool_instance(cls, account), request


def assert_utc_request(request):
    args = request.call_args.kwargs
    for key, value in (("start", START), ("end", END)):
        assert isinstance(args[key], EWSDateTime)
        assert args[key].tzinfo == EWSTimeZone("UTC")
        assert args[key].utcoffset() == timedelta(0)
        assert args[key] == datetime.fromisoformat(value)


@pytest.mark.parametrize("start,end", [(START, END),
    ("2026-11-03T00:00:00+02:00", "2026-11-03T01:00:00+02:00")])
def test_check_availability_utc_request_events_and_slots(start, end):
    event = NS(start=datetime(2026, 11, 2, 22),
               end=datetime(2026, 11, 2, 23), busy_type="Busy", details=None)
    tool, request = make_tool(calendar.CheckAvailabilityTool, "2", [event])
    result = run(tool, email_addresses=["user@example.com"],
                 start_time=start, end_time=end, interval_minutes=60)
    assert_utc_request(request)
    availability = result["availability"][0]
    returned = availability["calendar_events"][0]
    assert returned["start"] == start
    assert returned["end"] == end
    for key in ("slot_summaries", "blocking_slots", "non_free_slots"):
        assert availability[key][0]["start"] == start
        assert availability[key][0]["end"] == end


def test_working_elsewhere_label_and_summary():
    slot = calendar.build_slot_summaries(datetime.fromisoformat(START), 60, "5")[0]
    assert slot["label"] == "WorkingElsewhere"
    assert not slot["is_blocking"]
    summary = calendar.summarize_availability("15", [])
    assert summary["has_working_elsewhere"]
    assert summary["primary_status"] == "working_elsewhere"
    for code, primary in (("2", "busy"), ("3", "out_of_office"), ("4", "no_data")):
        assert calendar.summarize_availability(code + "5", [])["primary_status"] == primary


@pytest.mark.parametrize("code,allow_tentative,expected", [
    ("1", None, True), ("1", False, False), ("1", True, True),
    ("5", None, True), ("5", True, True), ("0", None, True),
    ("2", True, False), ("3", True, False), ("4", True, False),
])
def test_meeting_times_status_and_utc_request(code, allow_tentative, expected):
    tool, request = make_tool(calendar.FindMeetingTimesTool, code * 4)
    preferences = {"working_hours_only": False, "avoid_back_to_back": False}
    if allow_tentative is not None:
        preferences["allow_tentative"] = allow_tentative
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start=START, date_range_end=END, preferences=preferences)
    assert_utc_request(request)
    assert bool(result["suggestions"]) == expected
    if expected:
        assert result["suggestions"][0]["start_time"] == START
        assert result["suggestions"][0]["end_time"] == END


def test_meeting_times_merged_index_starts_at_requested_instant():
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "1000")
    result = run(tool, attendees=["user@example.com"], duration_minutes=15,
                 date_range_start=START, date_range_end=END,
                 preferences={"working_hours_only": False, "avoid_back_to_back": False})
    assert result["suggestions"][0]["start_time"] == "2026-11-02T17:15:00-05:00"


def test_allow_tentative_schema():
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "0")
    preferences = tool.get_schema()["inputSchema"]["properties"]["preferences"]
    assert preferences["properties"]["allow_tentative"]["default"] is True


def test_fully_free_slot_outranks_earlier_tentative_slot():
    # 17:00-18:00 tentative, 18:00-19:00 free.
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "1111" + "0000")
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start=START, date_range_end="2026-11-02T19:00:00-05:00",
                 max_suggestions=1,
                 preferences={"working_hours_only": False, "avoid_back_to_back": False})
    best = result["suggestions"][0]
    assert best["start_time"] == "2026-11-02T18:00:00-05:00"
    assert best["includes_tentative"] is False


def test_fully_free_slot_outranks_tentative_slot_with_preference_bonus():
    # 09:00-10:00 tentative (morning bonus), 17:00-18:00 free (evening penalty).
    merged = "1111" + "0" * 28 + "0000"
    tool, _ = make_tool(calendar.FindMeetingTimesTool, merged)
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start="2026-11-02T09:00:00-05:00",
                 date_range_end="2026-11-02T18:00:00-05:00",
                 preferences={"working_hours_only": False, "avoid_back_to_back": True,
                              "prefer_morning": True})
    flags = [s["includes_tentative"] for s in result["suggestions"]]
    assert flags == sorted(flags)
    assert result["suggestions"][0]["includes_tentative"] is False


def test_free_slot_inside_tentative_slot_is_still_offered():
    # Only 17:00-17:15 is tentative; 17:15-18:15 is fully free.
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "1" + "0" * 7)
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start=START, date_range_end="2026-11-02T19:00:00-05:00",
                 max_suggestions=1,
                 preferences={"working_hours_only": False, "avoid_back_to_back": True})
    best = result["suggestions"][0]
    assert best["start_time"] == "2026-11-02T17:15:00-05:00"
    assert best["includes_tentative"] is False


def test_meeting_times_index_across_dst_change():
    # US DST ends 2026-11-01 02:00 EDT. The local day 00:00-06:00 is 7 hours.
    # Busy for the first 4 real hours (16 intervals), then free.
    merged = "2" * 16 + "0" * 12
    tool, _ = make_tool(calendar.FindMeetingTimesTool, merged)
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start="2026-11-01T00:00:00-04:00",
                 date_range_end="2026-11-01T06:00:00-05:00", max_suggestions=1,
                 preferences={"working_hours_only": False, "avoid_back_to_back": False})
    # Four real hours after 00:00 EDT is 03:00 EST.
    assert result["suggestions"][0]["start_time"] == "2026-11-01T03:00:00-05:00"


def test_aware_utc_response_time_is_shown_with_caller_offset():
    event = NS(start=EWSDateTime(2026, 11, 2, 22, tzinfo=EWSTimeZone("UTC")),
               end=EWSDateTime(2026, 11, 2, 23, tzinfo=EWSTimeZone("UTC")),
               busy_type="Busy", details=None)
    tool, _ = make_tool(calendar.CheckAvailabilityTool, "2", [event])
    result = run(tool, email_addresses=["user@example.com"],
                 start_time=START, end_time=END, interval_minutes=60)
    assert result["availability"][0]["calendar_events"][0]["start"] == START


def test_allow_tentative_must_be_boolean():
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "0000")
    with pytest.raises(Exception):
        run(tool, attendees=["user@example.com"], duration_minutes=60,
            date_range_start=START, date_range_end=END,
            preferences={"allow_tentative": "false"})


def test_long_tentative_block_gives_no_overlapping_suggestions():
    # Three hours of tentative time, no free time.
    tool, _ = make_tool(calendar.FindMeetingTimesTool, "1" * 12)
    result = run(tool, attendees=["user@example.com"], duration_minutes=60,
                 date_range_start=START, date_range_end="2026-11-02T20:00:00-05:00",
                 max_suggestions=10,
                 preferences={"working_hours_only": False, "avoid_back_to_back": False})
    starts = [s["start_time"] for s in result["suggestions"]]
    assert starts == ["2026-11-02T17:00:00-05:00", "2026-11-02T18:00:00-05:00",
                      "2026-11-02T19:00:00-05:00"]
