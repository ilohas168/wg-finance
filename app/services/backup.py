"""Weekly backup of the ledger into a separate Google Sheet.

Each backup is one tab named ``Backup YYYY-MM-DD`` in the backup spreadsheet
(``BACKUP_SHEET_ID``, default: the "wg-finance-backups" sheet). The tab holds
a full copy of the Receipts, Receipt_Items and Settlements tabs, one block
after another, each starting with a ``### <tab name>`` row followed by that
tab's header and rows exactly as they are in the ledger. To restore, copy a
block back into the matching ledger tab.

The backup sheet must be owned by a person and shared with the app's service
account as Editor: service accounts have no Drive storage of their own, so
they cannot create the copy themselves.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import gspread

logger = logging.getLogger(__name__)

# The "wg-finance-backups" sheet in the owner's Drive, shared with the bot.
_DEFAULT_BACKUP_SHEET_ID = "1tSNh4Blmfsulr4TLhDmff76Gn0f3JMu5cnWQCKSL6rM"
BACKUP_TABS = ("Receipts", "Receipt_Items", "Settlements")
TAB_PREFIX = "Backup "
KEEP_BACKUPS = 8  # about two months of weekly backups
MIN_DAYS_BETWEEN = 6  # scheduled runs skip if a backup is newer than this


def backup_sheet_id() -> str:
    return os.environ.get("BACKUP_SHEET_ID", "").strip() or _DEFAULT_BACKUP_SHEET_ID


def _backup_date(title: str) -> Optional[date]:
    if not title.startswith(TAB_PREFIX):
        return None
    try:
        return datetime.strptime(title[len(TAB_PREFIX):].strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def list_backups(target: Any) -> list[str]:
    """Backup tab names in the target spreadsheet, newest first."""
    dated = [(d, ws.title) for ws in target.worksheets() if (d := _backup_date(ws.title))]
    return [title for _, title in sorted(dated, reverse=True)]


def build_backup_rows(source: Any) -> tuple[list[list[str]], dict[str, int]]:
    """All ledger tabs as one block of rows, plus the row count per tab."""
    rows: list[list[str]] = []
    counts: dict[str, int] = {}
    for tab in BACKUP_TABS:
        try:
            values = source.worksheet(tab).get_all_values()
        except gspread.exceptions.WorksheetNotFound:
            values = []
        counts[tab] = max(len(values) - 1, 0)  # minus the header row
        rows.append([f"### {tab}", f"{counts[tab]} rows"])
        rows.extend([str(cell) for cell in row] for row in values)
        rows.append([])
    width = max(len(row) for row in rows)
    return [row + [""] * (width - len(row)) for row in rows], counts


def run_backup(
    source: Any,
    target: Any,
    *,
    force: bool = False,
    today: Optional[date] = None,
    keep: int = KEEP_BACKUPS,
) -> dict[str, Any]:
    """Copy the ledger into a new ``Backup <date>`` tab and prune old ones.

    Without ``force`` (the weekly schedule) nothing happens when the newest
    backup is less than ``MIN_DAYS_BETWEEN`` days old, so calling it more
    often than weekly is harmless. With ``force`` (the "Back up now" button)
    today's backup is (re)written regardless.
    """
    today = today or datetime.now(timezone.utc).date()
    existing = list_backups(target)
    newest = _backup_date(existing[0]) if existing else None
    if not force and newest and today - newest < timedelta(days=MIN_DAYS_BETWEEN):
        return {"created": None, "skipped": True, "latest": existing[0], "backups": existing}

    rows, counts = build_backup_rows(source)
    title = f"{TAB_PREFIX}{today:%Y-%m-%d}"
    worksheets = {ws.title: ws for ws in target.worksheets()}
    new_ws = target.add_worksheet(title=f"{title} (writing)", rows=len(rows) + 5, cols=len(rows[0]))
    new_ws.update(values=rows, range_name="A1", value_input_option="RAW")
    # Only replace an older tab of the same day once the new copy is written.
    if title in worksheets:
        target.del_worksheet(worksheets[title])
    new_ws.update_title(title)

    backups = list_backups(target)
    for old in backups[keep:]:
        target.del_worksheet(target.worksheet(old))
    logger.info("backup: wrote %s (%s), kept %d", title, counts, min(len(backups), keep))
    return {"created": title, "skipped": False, "counts": counts, "backups": backups[:keep]}
