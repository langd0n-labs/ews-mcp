"""Offline regression coverage using real exchangelib objects and mocked I/O."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from exchangelib import Account, CalendarItem, Message, FileAttachment, Mailbox, HTMLBody, EWSTimeZone
from exchangelib.folders import Calendar
from exchangelib.properties import ParentFolderId
from exchangelib.version import Version, EXCHANGE_2016
from lxml import etree

from src.exceptions import ValidationError, ToolExecutionError
from src.tools import calendar_tools as calendar
from src.tools import email_tools_draft as drafts
from src.tools.calendar_support import calendar_datetime, recurrence_from_input, calendar_metadata

VERSION = Version(build=EXCHANGE_2016)


def run(tool, **kwargs):
    return asyncio.run(tool.execute(**kwargs))


def tool_instance(cls, account):
    # Avoid initializing production logging, configuration, credentials or accounts.
    tool = object.__new__(cls)
    tool.logger = Mock()
    tool.get_account = Mock(return_value=account)
    tool.get_mailbox_info = Mock(return_value="mailbox@example.test")
    return tool


@pytest.mark.parametrize("zone,dates,offsets", [
    ("America/New_York", ["2026-03-01", "2026-03-08"], [-5, -4]),
    ("America/New_York", ["2026-10-25", "2026-11-01"], [-4, -5]),
    ("Europe/London", ["2026-03-22", "2026-03-29"], [0, 1]),
    ("Europe/London", ["2026-10-18", "2026-10-25"], [1, 0]),
])
def test_named_zone_dst_and_exchange_xml(zone, dates, offsets):
    for day, offset in zip(dates, offsets):
        start = calendar_datetime(day + "T09:00:00", zone)
        end = calendar_datetime(day + "T10:00:00", zone)
        assert start.hour == 9
        assert start.utcoffset() == timedelta(hours=offset)
        assert start.tzinfo.key == zone
        item = CalendarItem(subject="Weekly", start=start, end=end,
                            recurrence=recurrence_from_input(dict(frequency="weekly", start=dates[0], weekdays=[7], count=4)))
        item.clean(version=VERSION)
        xml = etree.tostring(item.to_xml(version=VERSION)).decode()
        assert f'Id="{start.tzinfo.ms_id}"' in xml
        assert "StartTimeZone" in xml and "EndTimeZone" in xml
        assert "WeeklyRecurrence" in xml
        assert f"T09:00:00{start.isoformat()[-6:]}" in xml


@pytest.mark.parametrize("value", ["2026-03-08T02:30:00", "2026-11-01T01:30:00", "2026-03-08T02:30:00-05:00", "garbage"])
def test_bad_and_ambiguous_local_times(value):
    with pytest.raises(ValidationError):
        calendar_datetime(value, "America/New_York")


@pytest.mark.parametrize("zone", ["-05:00", "UTC-05:00", "not/a-zone"])
def test_numeric_offsets_are_not_named_zones(zone):
    with pytest.raises(ValidationError):
        calendar_datetime("2026-01-01T09:00:00", zone)


@pytest.mark.parametrize("offset,fold", [("-04:00", 0), ("-05:00", 1)])
def test_explicit_offset_selects_autumn_fold(offset, fold):
    dt = calendar_datetime("2026-11-01T01:30:00" + offset, "America/New_York")
    assert dt.fold == fold
    assert dt.isoformat().endswith(offset)


def test_legacy_offset_is_instant_in_configured_zone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    assert calendar_datetime("2026-03-08T13:00:00Z").isoformat() == "2026-03-08T09:00:00-04:00"


def event(kind="Single"):
    return CalendarItem(id="event-id", changekey="key", type=kind, parent_folder_id=ParentFolderId(id="calendar-id"),
                        start=calendar_datetime("2026-03-01T09:00:00", "America/New_York"),
                        end=calendar_datetime("2026-03-01T10:00:00", "America/New_York"),
                        _start_timezone=EWSTimeZone("America/New_York"),
                        _end_timezone=EWSTimeZone("America/New_York"), subject="Original")


def test_create_appointment_preserves_named_timezone(monkeypatch):
    account = Mock(spec=Account)
    account.version = VERSION
    account.calendar = Mock(spec=Calendar)
    account.calendar.account = account
    created = []
    def save(item, **kwargs):
        item.clean(version=VERSION)
        item.id = "created-id"
        created.append(item)
    monkeypatch.setattr(CalendarItem, "save", save)
    tool = tool_instance(calendar.CreateAppointmentTool, account)
    tool.validate_input = lambda cls, **kw: cls(**kw)
    result = run(tool, subject="Weekly", start_time="2026-03-01T09:00:00", end_time="2026-03-01T10:00:00",
                 timezone="America/New_York", recurrence=dict(frequency="weekly", start="2026-03-01", weekdays=[7], count=8))
    assert result["item_id"] == "created-id"
    assert created[0]._start_timezone.key == "America/New_York"
    assert created[0].recurrence.pattern.weekdays == [7]


@pytest.mark.parametrize("kind,scope", [("RecurringMaster", None), ("Occurrence", None), ("Exception", None), ("RecurringMaster", "occurrence"), ("Occurrence", "series"), ("Single", "future")])
def test_rejects_unsafe_recurrence_scope(monkeypatch, kind, scope):
    item = event(kind)
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    save = Mock()
    monkeypatch.setattr(CalendarItem, "save", save)
    kwargs = dict(item_id=item.id, subject="Changed")
    if scope:
        kwargs["scope"] = scope
    with pytest.raises(ToolExecutionError):
        run(tool_instance(calendar.UpdateAppointmentTool, account), **kwargs)
    save.assert_not_called()


def test_series_rule_and_fields_updated_in_place(monkeypatch):
    item = event("RecurringMaster")
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    saved = Mock()
    monkeypatch.setattr(CalendarItem, "save", saved)
    result = run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="series", subject="Changed",
                 recurrence=dict(frequency="daily", start="2026-03-01", count=4))
    assert result["item_id"] == item.id
    assert item.subject == "Changed" and item.recurrence.boundary.number == 4
    saved.assert_called_once()


@pytest.mark.parametrize("kind", ["Occurrence", "Exception"])
def test_occurrence_only_edits_selected_item(monkeypatch, kind):
    item = event(kind)
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    saved = []
    monkeypatch.setattr(CalendarItem, "save", lambda obj, **kw: saved.append(obj))
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="occurrence", subject="One only")
    assert saved == [item]
    with pytest.raises(ToolExecutionError, match="Recurrence changes require series scope"):
        run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="occurrence", recurrence=None)
    assert saved == [item]


def test_master_index_resolves_occurrence_before_update(monkeypatch):
    master = event("RecurringMaster")
    occurrence = event("Occurrence")
    occurrence.id = "occurrence-id"
    occurrence_method = Mock(return_value=occurrence)
    refresh = Mock()
    monkeypatch.setattr(CalendarItem, "occurrence", occurrence_method)
    monkeypatch.setattr(CalendarItem, "refresh", refresh)
    saved = []
    monkeypatch.setattr(CalendarItem, "save", lambda obj, **kw: saved.append(obj))
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=master)))
    result = run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=master.id, scope="occurrence", occurrence_index=3, subject="One")
    occurrence_method.assert_called_once_with(3)
    refresh.assert_called_once()
    assert saved == [occurrence] and master.subject == "Original"
    assert result["item_id"] == "occurrence-id"


def test_update_time_refreshes_exchange_timezone_fields(monkeypatch):
    item = event("RecurringMaster")
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    monkeypatch.setattr(CalendarItem, "save", lambda obj, **kw: obj.clean(version=VERSION))
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="series",
        start_time="2026-11-01T09:00:00", end_time="2026-11-01T10:00:00", timezone="America/New_York")
    assert item.start.isoformat() == "2026-11-01T09:00:00-05:00"
    assert item._start_timezone.ms_id == "Eastern Standard Time"
    # Existing callers may still use UTC instants without an explicit timezone.
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="series",
        start_time="2026-03-08T13:00:00Z", end_time="2026-03-08T14:00:00Z")
    assert item.start.isoformat() == "2026-03-08T09:00:00-04:00"


def test_occurrence_metadata_includes_master(monkeypatch):
    item = event("Exception")
    master = event("RecurringMaster")
    master.id = "master-id"
    monkeypatch.setattr(CalendarItem, "recurring_master", lambda obj: master)
    monkeypatch.setattr(CalendarItem, "refresh", lambda obj: None)
    metadata = calendar_metadata(item)
    assert metadata["series_master_id"] == "master-id"
    assert metadata["calendar_item_type"] == "Exception"


@pytest.mark.parametrize("value", [dict(frequency="monthly", start="2026-01-01"), dict(frequency="weekly", start="2026-01-01"), dict(frequency="daily", start="2026-01-01", count=0), dict(frequency="daily", start="2026-01-01", end="2026-01-03", count=3)])
def test_recurrence_validation(value):
    with pytest.raises(ValidationError):
        recurrence_from_input(value)


def draft_fixture(monkeypatch):
    message = Message(is_draft=True, is_submitted=False,
                      subject="Original", body=HTMLBody("<p>Quoted context</p>"),
                      to_recipients=[Mailbox(email_address="old@example.test")],
                      in_reply_to="<parent@example.test>",
                      attachments=[FileAttachment(name="keep.txt", content=b"keep")])
    message.id = "draft-id"
    message.changekey = "key"
    monkeypatch.setattr(drafts, "find_message_for_account", Mock(return_value=message))
    saved = []
    def save(obj, **kwargs):
        saved.append((obj, kwargs))
        obj.id = "current-id"
    monkeypatch.setattr(Message, "save", save)
    def must_not_send(*args, **kwargs):
        pytest.fail("Draft editing attempted to send")
    monkeypatch.setattr(Message, "send", must_not_send)
    monkeypatch.setattr(Message, "send_and_save", must_not_send)
    return message, saved, tool_instance(drafts.UpdateDraftTool, object())


def test_update_draft_preserves_omitted_fields_attachments_reply(monkeypatch):
    message, saved, tool = draft_fixture(monkeypatch)
    attachments = message.attachments
    result = run(tool, message_id="draft-id", subject="Changed")
    assert result["message_id"] == "current-id"
    assert message.body == "<p>Quoted context</p>"
    assert message.to_recipients[0].email_address == "old@example.test"
    assert message.attachments is attachments
    assert message.in_reply_to == "<parent@example.test>"
    assert saved == [(message, dict(update_fields=["subject"]))]


def test_update_draft_body_and_all_recipients(monkeypatch):
    message, saved, tool = draft_fixture(monkeypatch)
    run(tool, message_id="draft-id", body="Hello", body_format="text", to=["new@example.test"], cc=[], bcc=["blind@example.test"], subject="")
    assert "Hello" in message.body
    assert message.to_recipients[0].email_address == "new@example.test"
    assert message.cc_recipients == []
    assert message.bcc_recipients[0].email_address == "blind@example.test"
    assert message.in_reply_to == "<parent@example.test>"
    assert set(saved[0][1]["update_fields"]) == {"subject", "body", "to_recipients", "cc_recipients", "bcc_recipients"}


@pytest.mark.parametrize("state", [dict(is_draft=False), dict(is_submitted=True), dict(datetime_sent=calendar_datetime("2026-01-01T09:00:00Z"))])
def test_rejects_sent_or_submitted_messages(monkeypatch, state):
    message, saved, tool = draft_fixture(monkeypatch)
    for key, value in state.items():
        setattr(message, key, value)
    with pytest.raises(ToolExecutionError, match="unsent draft"):
        run(tool, message_id="draft-id", subject="Changed")
    assert not saved


def test_invalid_draft_recipient_does_not_save(monkeypatch):
    message, saved, tool = draft_fixture(monkeypatch)
    with pytest.raises(ToolExecutionError):
        run(tool, message_id="draft-id", subject="Changed", to=["invalid"])
    assert message.subject == "Original" and not saved


def test_real_exchange_draft_save_uses_save_only(monkeypatch):
    message = Message(id="draft-id", changekey="key", is_draft=True)
    update = Mock(return_value=("draft-id", "new-key"))
    monkeypatch.setattr(Message, "_update", update)
    message.save(update_fields=["subject"])
    assert update.call_args.kwargs["message_disposition"] == "SaveOnly"
    assert update.call_args.kwargs["update_fieldnames"] == ["subject"]


@pytest.mark.parametrize("start,end,valid", [
    ("2026-11-01T01:45:00-04:00", "2026-11-01T01:15:00-05:00", True),
    ("2026-11-01T01:15:00-05:00", "2026-11-01T01:45:00-04:00", False),
])
def test_fold_interval_validation_create_and_update(monkeypatch, start, end, valid):
    from src.models import CreateAppointmentRequest
    first = calendar_datetime(start, "America/New_York")
    last = calendar_datetime(end, "America/New_York")
    if valid:
        CreateAppointmentRequest(subject="Fold", start_time=first, end_time=last)
    else:
        with pytest.raises(ValueError, match="end_time must be after start_time"):
            CreateAppointmentRequest(subject="Fold", start_time=first, end_time=last)
    item = event()
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    save = Mock()
    monkeypatch.setattr(CalendarItem, "save", save)
    kwargs = dict(item_id=item.id, start_time=start, end_time=end, timezone="America/New_York")
    tool = tool_instance(calendar.UpdateAppointmentTool, account)
    if valid:
        run(tool, **kwargs)
        save.assert_called_once_with(update_fields=["start", "end"])
    else:
        with pytest.raises(ToolExecutionError, match="end_time must be after start_time"):
            run(tool, **kwargs)
        save.assert_not_called()


def offline_account():
    from exchangelib import Configuration, Credentials
    return Account(primary_smtp_address="fixture@example.test", autodiscover=False,
                   config=Configuration(server="exchange.example.test", auth_type="basic",
                                        credentials=Credentials("fixture", "fixture"), version=VERSION))


@pytest.mark.parametrize("kind", ["RecurringMaster", "Occurrence", "Exception"])
def test_real_calendar_save_payload_limits_fields(monkeypatch, kind):
    from exchangelib.services import UpdateItem
    item = event(kind)
    item.account = offline_account()
    payloads = []
    def update(service, **kwargs):
        payloads.append(service.get_payload(**{k: v for k, v in kwargs.items() if k != "expect_result"}))
        return item.id, "new-key"
    monkeypatch.setattr(UpdateItem, "get", update)
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id,
        scope="series" if kind == "RecurringMaster" else "occurrence", subject="Only subject")
    xml = etree.tostring(payloads[0]).decode()
    assert 'FieldURI="item:Subject"' in xml
    assert "Recurrence" not in xml and "UID" not in xml
    assert xml.count("<t:SetItemField>") == 1
    assert 'Id="event-id"' in xml


def test_real_timezone_update_payload_contains_new_named_zone(monkeypatch):
    from exchangelib.services import UpdateItem
    item = event("RecurringMaster")
    item.account = offline_account()
    payloads = []
    def update(service, **kwargs):
        payloads.append(service.get_payload(**{k: v for k, v in kwargs.items() if k != "expect_result"}))
        return item.id, "new-key"
    monkeypatch.setattr(UpdateItem, "get", update)
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="series",
        timezone="Europe/London", start_time="2026-10-25T09:00:00", end_time="2026-10-25T10:00:00")
    xml = etree.tostring(payloads[0]).decode()
    assert 'Id="GMT Standard Time"' in xml
    assert "StartTimeZone" in xml and "EndTimeZone" in xml
    assert "2026-10-25T09:00:00+00:00" in xml
    assert "Eastern Standard Time" not in xml


def test_real_create_clean_and_xml_across_autumn_fold():
    item = CalendarItem(subject="Across fold",
                        start=calendar_datetime("2026-11-01T01:45:00-04:00", "America/New_York"),
                        end=calendar_datetime("2026-11-01T01:15:00-05:00", "America/New_York"))
    item.clean(version=VERSION)
    xml = etree.tostring(item.to_xml(version=VERSION)).decode()
    assert "2026-11-01T01:45:00-04:00" in xml
    assert "2026-11-01T01:15:00-05:00" in xml
    assert 'Id="Eastern Standard Time"' in xml


@pytest.mark.parametrize("rule", [None, dict(frequency="daily", start="2026-03-01", count=5)])
def test_real_series_recurrence_update_payload(monkeypatch, rule):
    from exchangelib.services import UpdateItem
    item = event("RecurringMaster")
    item.account = offline_account()
    item.recurrence = recurrence_from_input(dict(frequency="weekly", start="2026-03-01", weekdays=[7], count=8))
    payloads = []
    def update(service, **kwargs):
        payloads.append(service.get_payload(**{k: v for k, v in kwargs.items() if k != "expect_result"}))
        return item.id, "new-key"
    monkeypatch.setattr(UpdateItem, "get", update)
    account = SimpleNamespace(calendar=Mock(id="calendar-id", get=Mock(return_value=item)))
    run(tool_instance(calendar.UpdateAppointmentTool, account), item_id=item.id, scope="series", recurrence=rule)
    xml = etree.tostring(payloads[0]).decode()
    assert 'FieldURI="calendar:Recurrence"' in xml
    if rule is None:
        assert "DeleteItemField" in xml
    else:
        assert "DailyRecurrence" in xml and "WeeklyRecurrence" not in xml
    assert 'Id="event-id"' in xml


def test_get_calendar_returns_recurrence_metadata(monkeypatch):
    item = event("Occurrence")
    master = event("RecurringMaster")
    master.id = "master-id"
    monkeypatch.setattr(CalendarItem, "recurring_master", lambda obj: master)
    monkeypatch.setattr(CalendarItem, "refresh", lambda obj: None)
    query = Mock()
    query.only.return_value.order_by.return_value = [item]
    account = SimpleNamespace(calendar=Mock(view=Mock(return_value=query)))
    result = run(tool_instance(calendar.GetCalendarTool, account), start_date="2026-03-01", end_date="2026-03-02")
    assert "type" in query.only.call_args.args
    assert "_start_timezone" in query.only.call_args.args
    assert result["events"][0]["series_master_id"] == "master-id"
    assert result["events"][0]["calendar_item_type"] == "Occurrence"


def test_draft_real_update_payload_preserves_context_and_clears_fields(monkeypatch):
    from exchangelib.services import UpdateItem
    from exchangelib.properties import ConversationId
    message = Message(id="draft-id", changekey="key", account=offline_account(), is_draft=True,
                      subject="Original", body=HTMLBody("Quoted"), in_reply_to="parent",
                      conversation_id=ConversationId(id="conversation"))
    monkeypatch.setattr(drafts, "find_message_for_account", Mock(return_value=message))
    payloads = []
    def update(service, **kwargs):
        payloads.append(service.get_payload(**{k: v for k, v in kwargs.items() if k != "expect_result"}))
        return message.id, "new-key"
    monkeypatch.setattr(UpdateItem, "get", update)
    tool = tool_instance(drafts.UpdateDraftTool, message.account)
    run(tool, message_id=message.id, body="", to=[], bcc=[])
    xml = etree.tostring(payloads[0]).decode()
    assert 'MessageDisposition="SaveOnly"' in xml
    assert 'FieldURI="message:ToRecipients"' in xml
    assert 'FieldURI="message:BccRecipients"' in xml
    assert "Attachments" not in xml and "InReplyTo" not in xml and "ConversationId" not in xml
    assert "Subject" not in xml and "CcRecipients" not in xml
    assert message.in_reply_to == "parent" and message.conversation_id.id == "conversation"
