# Exchange calendar selection

Call `list_calendars` with optional `target_mailbox`. The response contains
`mailbox` and `calendars`, each with `calendar_id`, `name`, `parent_folder_id`,
and `is_default`. Names may repeat. Store the mailbox together with the opaque
calendar ID; never select by name alone.

Pass `calendar_id` to `get_calendar`, `create_appointment`, or
`update_appointment`, together with the same `target_mailbox` used for discovery.
For example, `get_calendar` accepts:

```json
{"calendar_id": "opaque-folder-id", "start_date": "2026-11-01", "days_ahead": 7}
```

Omitting `calendar_id` retains the default calendar. `update_appointment` without
`calendar_id` now verifies that the item belongs to the default calendar. An explicit null, empty,
unknown, deleted, non-calendar, or other-mailbox ID fails. An explicit target
never falls back. Responses include the selected `calendar_id`. Existing date,
timezone, recurrence, and update-scope arguments retain their meanings.
Updates verify the item's parent-folder ID before any mutation, including after
resolving a recurrence occurrence. Exchange remains authoritative for permissions;
a discoverable calendar can still reject reads or writes. No permission elevation
or read-only write probe is performed. Authentication behavior is unchanged.
`delete_appointment` and `respond_to_meeting` take no calendar selector.
`check_availability` covers the default calendar only. Gmail calendars are outside EWS scope.

## Discovery and restricted-folder diagnosis

Discovery issues deep FindFolder under the distinguished `msgfolderroot` (IPM
subtree), selecting visible `IPF.Appointment` folders. It does not use `.children`,
`.walk()`, or the mailbox-root cache, which can traverse non-IPM system folders.
System calendar names excluded by the existing folder visibility filter remain
excluded. Discovery errors fail the call; incomplete results are not success.
If resolving the distinguished default calendar raises `ErrorAccessDenied` after
discovery succeeds, the discovered calendars are returned with `is_default: null`
for every entry. No root-cache fallback is attempted. Other default-resolution
errors still fail the call.

The existing generic `find_folder` starts from `account.root` by default; its
recursive `.children` traversal can reach restricted non-IPM folders. This path
and the startup root-tree connectivity check remain unchanged.

A non-IPM policy error on a root walk does not establish that normal calendars
are inaccessible. Compare an approved read-only IPM discovery with the failing
root request. An IPM discovery failure still needs administrator diagnosis:
application access policy and Exchange folder/delegate permissions can both deny
access. Retain the EWS error class/code and request scope; do not bypass policy,
retry against system roots, or infer the cause solely from an access-denied error.
