"""Offline coverage for calendar show-as inputs and responses."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from exchangelib import Account, CalendarItem
from exchangelib.folders import Calendar

from src.exceptions import ValidationError
from src.tools import calendar_tools as calendar
from test_calendar_draft_fixes import VERSION, event, run, tool_instance


VALUES = ["Free", "Tentative", "Busy", "OOF", "WorkingElsewhere"]
CREATE = dict(subject="Appointment", start_time="2026-11-02T09:00:00Z",
              end_time="2026-11-02T10:00:00Z")


def make_tool(cls, account):
    tool = tool_instance(cls, account)
    tool.get_mailbox_info.return_value = "user@example.com"
    return tool


@pytest.fixture
def create_setup(monkeypatch):
    account = Mock(spec=Account)
    account.version = VERSION
    account.calendar = Mock(spec=Calendar)
    account.calendar.account = account
    account.calendar.id = "calendar-id"
    saved = []

    def save(item, **kwargs):
        saved.append(item)
        item.id = "created-id"

    monkeypatch.setattr(CalendarItem, "save", save)
    factory = Mock(wraps=CalendarItem)
    monkeypatch.setattr(calendar, "CalendarItem", factory)
    return make_tool(calendar.CreateAppointmentTool, account), saved, factory


@pytest.mark.parametrize("value", VALUES)
def test_create_show_as(create_setup, value):
    tool, saved, _ = create_setup
    result = run(tool, **CREATE, show_as=value)
    assert saved[0].legacy_free_busy_status == value
    assert not saved[0].required_attendees
    assert result["show_as"] == value


def test_create_omitted_show_as_keeps_default(create_setup):
    tool, saved, factory = create_setup
    result = run(tool, **CREATE)
    assert "legacy_free_busy_status" not in factory.call_args.kwargs
    assert saved[0].legacy_free_busy_status == CalendarItem().legacy_free_busy_status
    assert result["show_as"] == saved[0].legacy_free_busy_status


@pytest.mark.parametrize("value", VALUES)
def test_update_only_show_as(monkeypatch, value):
    item = event()
    account = NS(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    save = Mock()
    monkeypatch.setattr(CalendarItem, "save", save)
    result = run(make_tool(calendar.UpdateAppointmentTool, account),
                 item_id=item.id, show_as=value)
    assert item.legacy_free_busy_status == value
    save.assert_called_once_with(update_fields=["legacy_free_busy_status"])
    assert result["show_as"] == value


@pytest.mark.parametrize("cls,args", [
    (calendar.CreateAppointmentTool, CREATE),
    (calendar.UpdateAppointmentTool, {"item_id": "event-id"}),
])
@pytest.mark.parametrize("value", ["NoData", "tentative", "", None])
def test_invalid_show_as_before_exchange(cls, args, value):
    account = Mock()
    tool = make_tool(cls, account)
    with pytest.raises(ValidationError, match="show_as"):
        run(tool, **args, show_as=value)
    tool.get_account.assert_not_called()
    account.assert_not_called()
    assert not account.mock_calls


@pytest.mark.parametrize("value", VALUES + ["NoData", None])
def test_get_calendar_returns_show_as(value):
    item = event()
    item.legacy_free_busy_status = value
    query = Mock()
    query.only.return_value.order_by.return_value = [item]
    account = NS(calendar=Mock(id="calendar-id", view=Mock(return_value=query)))
    result = run(make_tool(calendar.GetCalendarTool, account))
    assert result["events"][0]["show_as"] == value
    assert "legacy_free_busy_status" in query.only.call_args.args


@pytest.mark.parametrize("cls", [calendar.CreateAppointmentTool, calendar.UpdateAppointmentTool])
def test_show_as_schema(cls):
    schema = make_tool(cls, Mock()).get_schema()["inputSchema"]
    assert schema["properties"]["show_as"] == {
        "type": "string", "enum": VALUES,
        "description": "Free/busy status shown for this event (Outlook 'Show as').",
    }
    assert "show_as" not in schema["required"]
