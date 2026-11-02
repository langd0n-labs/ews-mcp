"""Mailbox-scoped calendar discovery without non-IPM root traversal."""
from exchangelib.errors import ErrorAccessDenied
from exchangelib.folders import FolderCollection, Folder, Calendar, MsgFolderRoot, Root
from .base import BaseTool
from .folder_tools import is_user_visible_folder
from ..exceptions import ValidationError, ToolExecutionError
from ..utils import format_success_response

CALENDAR_ID_SCHEMA = {
    "type": "string", "minLength": 1,
    "description": "Opaque calendar folder ID from list_calendars in target_mailbox. Omit for default calendar. Names are not accepted.",
}


def discover_calendars(account):
    # children/walk use the root-wide cache, which can touch restricted non-IPM
    # folders. Issue FindFolder directly beneath the distinguished IPM root.
    ipm_root = MsgFolderRoot(root=Root(account=account), is_distinguished=True)
    folders = FolderCollection(account=account, folders=[ipm_root])
    result = []
    for folder in folders.find_folders(depth="Deep"):
        if isinstance(folder, Exception):
            raise folder
        if folder.folder_class == "IPF.Appointment" and is_user_visible_folder(folder):
            if isinstance(folder, Folder) and not isinstance(folder, Calendar):
                folder = Calendar(root=folder.root, id=folder.id, changekey=folder.changekey,
                                  name=folder.name, parent_folder_id=folder.parent_folder_id,
                                  folder_class=folder.folder_class)
            result.append(folder)
    return result


def select_calendar(account, kwargs):
    if "calendar_id" not in kwargs:
        return account.calendar
    calendar_id = kwargs["calendar_id"]
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        raise ValidationError("calendar_id must be a nonempty folder ID")
    matches = [folder for folder in discover_calendars(account) if folder.id == calendar_id]
    if len(matches) != 1:
        raise ValidationError("Calendar not found in selected mailbox's normal calendar hierarchy")
    return matches[0]


def verify_item_calendar(item, folder):
    # QuerySet.get(id=...) uses GetItem, which is not folder scoped.
    parent = item.parent_folder_id
    if parent is None or parent.id != folder.id:
        raise ValidationError("Appointment does not belong to the selected calendar")


class ListCalendarsTool(BaseTool):
    def get_schema(self):
        return {
            "name": "list_calendars",
            "description": "Discover Exchange calendars beneath the normal mailbox hierarchy. Duplicate names remain separate IDs. Permission errors are reported, not bypassed.",
            "inputSchema": {"type": "object", "properties": {
                "target_mailbox": {"type": "string", "description": "Mailbox with existing delegate/impersonation access"}
            }},
        }

    async def execute(self, **kwargs):
        try:
            account = self.get_account(kwargs.get("target_mailbox"))
            calendars = discover_calendars(account)
            # Account.calendar uses Root.get_default_folder(), whose fallback
            # builds a mailbox-wide folder cache when GetFolder is denied.
            # Resolve only the distinguished calendar; denied access leaves
            # default status unknown without traversing the root cache.
            root = Root(account=account)
            try:
                default_id = Calendar.resolve(
                    account=account, folder=Calendar(root=root, is_distinguished=True)
                ).id
            except ErrorAccessDenied:
                default_id = None
            return format_success_response("Calendars retrieved", mailbox=self.get_mailbox_info(kwargs.get("target_mailbox")),
                calendars=[{"calendar_id": f.id, "name": f.name,
                            "parent_folder_id": f.parent_folder_id.id if f.parent_folder_id else None,
                            "is_default": f.id == default_id if default_id is not None else None} for f in calendars])
        except Exception as exc:
            raise ToolExecutionError(f"Calendar discovery failed: {type(exc).__name__}: {exc}") from exc
