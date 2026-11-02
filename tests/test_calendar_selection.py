"""Offline selection boundaries and hierarchy traversal regressions."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from exchangelib import CalendarItem
from src.tools import calendar_selection as selection
from src.tools import calendar_tools as calendar
from src.exceptions import ValidationError, ToolExecutionError
from test_calendar_draft_fixes import tool_instance, run, event


def folder(id, name="Work"):
    return NS(id=id, name=name, parent_folder_id=NS(id="ipm"), folder_class="IPF.Appointment")


@pytest.fixture
def hierarchy(monkeypatch):
    first, second = folder("default"), folder("secondary")
    account = NS(calendar=first, root=object(), msg_folder_root=object())
    monkeypatch.setattr(selection, "Root", lambda **kwargs: account.root)
    monkeypatch.setattr(selection, "MsgFolderRoot", lambda **kwargs: account.msg_folder_root)
    collection = Mock()
    collection.find_folders.return_value = [first, second, NS(folder_class="IPF.Note")]
    factory = Mock(return_value=collection)
    monkeypatch.setattr(selection, "FolderCollection", factory)
    monkeypatch.setattr(selection.Calendar, "resolve", Mock(return_value=first))
    return account, first, second, factory, collection


def test_discovery_uses_ipm_not_root_and_preserves_duplicate_names(hierarchy):
    account, first, second, factory, collection = hierarchy
    result = run(tool_instance(selection.ListCalendarsTool, account))
    assert [f["calendar_id"] for f in result["calendars"]] == ["default", "secondary"]
    assert [f["is_default"] for f in result["calendars"]] == [True, False]
    factory.assert_called_once_with(account=account, folders=[account.msg_folder_root])
    collection.find_folders.assert_called_once_with(depth="Deep")
    assert selection.select_calendar(account, {"calendar_id": "secondary"}) is second
    assert selection.select_calendar(account, {}) is first


@pytest.mark.parametrize("id", [None, "", 1, "Work", "foreign-mailbox-id", "deleted-id"])
def test_explicit_invalid_target_never_falls_back(hierarchy, id):
    with pytest.raises(ValidationError):
        selection.select_calendar(hierarchy[0], {"calendar_id": id})


@pytest.mark.parametrize("reason", ["ErrorAccessDenied", "restricted non-IPM folder policy"])
def test_discovery_errors_are_not_partial_success(hierarchy, reason):
    hierarchy[4].find_folders.return_value = [hierarchy[1], ToolExecutionError(reason)]
    with pytest.raises(ToolExecutionError, match=reason):
        run(tool_instance(selection.ListCalendarsTool, hierarchy[0]))


def test_read_targets_only_selected_calendar(hierarchy):
    account, default, selected, _, _ = hierarchy
    query = Mock()
    query.only.return_value.order_by.return_value = []
    selected.view = Mock(return_value=query)
    result = run(tool_instance(calendar.GetCalendarTool, account), calendar_id="secondary", target_mailbox="delegate@example.test")
    assert result["calendar_id"] == "secondary"
    selected.view.assert_called_once()


@pytest.mark.parametrize("parent", [None, "default", "foreign-folder"])
def test_update_rejects_mismatched_item_before_mutation(hierarchy, monkeypatch, parent):
    account, _, selected, _, _ = hierarchy
    item = event()
    from exchangelib.properties import ParentFolderId
    item.parent_folder_id = ParentFolderId(id=parent) if parent else None
    selected.get = Mock(return_value=item)
    save = Mock()
    monkeypatch.setattr(CalendarItem, "save", save)
    with pytest.raises(ToolExecutionError, match="does not belong"):
        run(tool_instance(calendar.UpdateAppointmentTool, account), calendar_id="secondary", item_id=item.id, subject="Changed")
    assert item.subject == "Original"
    save.assert_not_called()


def test_read_only_calendar_error_has_no_fallback(hierarchy):
    account, _, selected, _, _ = hierarchy
    selected.get = Mock(side_effect=ToolExecutionError("ErrorAccessDenied"))
    with pytest.raises(ToolExecutionError, match="ErrorAccessDenied"):
        run(tool_instance(calendar.UpdateAppointmentTool, account), calendar_id="secondary", item_id="item", subject="Changed")


@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01"])
def test_create_secondary_preserves_all_day_dates(monkeypatch, day):
    from exchangelib import Account
    from exchangelib.folders import Calendar
    from test_calendar_draft_fixes import VERSION
    account = Mock(spec=Account)
    account.version = VERSION
    default = Mock(spec=Calendar)
    selected = Mock(spec=Calendar)
    selected.account = account
    selected.id = "secondary"
    account.calendar = default
    monkeypatch.setattr(selection, "discover_calendars", lambda account: [selected])
    saved = []
    def save(item, **kwargs):
        item.clean(version=VERSION)
        item.id = "created"
        saved.append(item)
    monkeypatch.setattr(CalendarItem, "save", save)
    tool = tool_instance(calendar.CreateAppointmentTool, account)
    tool.validate_input = lambda cls, **kw: cls(**kw)
    from datetime import date, timedelta
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    result = run(tool, calendar_id="secondary", subject="All day", start_time=day + "T00:00:00",
                 end_time=end + "T00:00:00", timezone="America/New_York", is_all_day=True)
    assert saved[0].folder is selected
    assert str(saved[0].start.date()) == day
    assert str(saved[0].end.date()) == end
    assert result["calendar_id"] == "secondary"


def test_hidden_system_calendar_is_excluded(hierarchy):
    hierarchy[4].find_folders.return_value = [folder("logging", "Calendar Logging")]
    assert selection.discover_calendars(hierarchy[0]) == []


def test_secondary_update_saves_selected_item(hierarchy, monkeypatch):
    from exchangelib.properties import ParentFolderId
    account, _, selected, _, _ = hierarchy
    item = event()
    item.parent_folder_id = ParentFolderId(id="secondary")
    selected.get = Mock(return_value=item)
    save = Mock()
    monkeypatch.setattr(CalendarItem, "save", save)
    run(tool_instance(calendar.UpdateAppointmentTool, account), calendar_id="secondary", item_id=item.id, subject="Changed")
    save.assert_called_once_with(update_fields=["subject"])


def test_real_generic_folder_is_calendar_and_ipm_root_is_distinguished(monkeypatch):
    from exchangelib.folders import Folder, Calendar, MsgFolderRoot, Root
    from test_calendar_draft_fixes import offline_account
    account = offline_account()
    generic = Folder(root=Root(account=account), id="secondary", name="Work", folder_class="IPF.Appointment")
    captured = []
    def find(collection, **kwargs):
        captured.extend(collection.folders)
        return iter([generic])
    monkeypatch.setattr(selection.FolderCollection, "find_folders", find)
    result = selection.discover_calendars(account)
    assert isinstance(result[0], Calendar)
    assert result[0].id == "secondary" and callable(result[0].view)
    assert isinstance(captured[0], MsgFolderRoot)
    assert captured[0].to_id().id == "msgfolderroot"
    assert captured[0].to_id().mailbox.email_address == account.primary_smtp_address


def test_denied_default_resolution_does_not_traverse_root(monkeypatch):
    from exchangelib.errors import ErrorAccessDenied
    from exchangelib.folders import Calendar, MsgFolderRoot, Root
    from unittest.mock import PropertyMock
    from test_calendar_draft_fixes import offline_account

    account = offline_account()
    secondary = Calendar(root=Root(account=account), id="secondary", name="Work",
                         folder_class="IPF.Appointment")
    discovery = Mock()
    resolution = Mock()
    traversal = PropertyMock(side_effect=AssertionError("root cache traversal"))
    monkeypatch.setattr(Root, "_folders_map", traversal)

    def find(collection, **kwargs):
        discovery(**kwargs)
        assert len(collection.folders) == 1
        assert isinstance(collection.folders[0], MsgFolderRoot)
        return iter([secondary])

    def get(collection, **kwargs):
        resolution()
        assert len(collection.folders) == 1
        distinguished = collection.folders[0].to_id()
        assert distinguished.id == "calendar"
        assert distinguished.mailbox.email_address == account.primary_smtp_address
        return iter([ErrorAccessDenied("default calendar denied")])

    monkeypatch.setattr(selection.FolderCollection, "find_folders", find)
    monkeypatch.setattr(selection.FolderCollection, "get_folders", get)
    result = run(tool_instance(selection.ListCalendarsTool, account))
    assert result["calendars"] == [{
        "calendar_id": "secondary", "name": "Work",
        "parent_folder_id": None, "is_default": None,
    }]
    discovery.assert_called_once_with(depth="Deep")
    resolution.assert_called_once_with()
    traversal.assert_not_called()


def test_non_access_denied_default_resolution_still_raises(hierarchy, monkeypatch):
    from exchangelib.errors import ErrorFolderNotFound

    error = ErrorFolderNotFound("default calendar missing")
    monkeypatch.setattr(selection.Calendar, "resolve", Mock(side_effect=error))
    with pytest.raises(ToolExecutionError, match="ErrorFolderNotFound: default calendar missing") as caught:
        run(tool_instance(selection.ListCalendarsTool, hierarchy[0]))
    assert caught.value.__cause__ is error
