"""Ledger engine — split calculation + Google Sheets persistence.

Handles the three core operations:
1. ``calculate_split``  — compute per-person bearings for one item.
2. ``append_transactions``  — write line-items to Sheet 1 (Transactions).
3. ``update_summary``  — update running balances in Sheet 2 (Summary Ledger).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from google.oauth2.service_account import Credentials
import gspread
from gspread.client import Client as GSpreadClient
from pydantic import ValidationError

from app.models import (
    LedgerEntry,
    ReceiptData,
    RoommateBalance,
    SharehouseLedgerState,
    SplitType,
    compute_bearings,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Google Sheets client factory                                         #
# ------------------------------------------------------------------ #

def _get_gspread_client() -> GSpreadClient:
    """Authenticate and return a gspread Client from service account JSON."""
    cred_val = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

    if cred_val and os.path.isfile(os.path.expanduser(cred_val)):
        creds = Credentials.from_service_account_file(
            os.path.expanduser(cred_val), scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return gspread.authorize(creds)

    # Fallback: inline JSON env var.
    if cred_val and cred_val.startswith("{"):
        import json
        info = json.loads(cred_val)
        creds = Credentials.from_service_account_info(info, scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        return gspread.authorize(creds)

    raise RuntimeError(
        "Neither GOOGLE_CREDENTIALS_JSON (file path or JSON blob) nor a "
        "service-account key was found. Set GOOGLE_CREDENTIALS_JSON to the path "
        "of your service-account JSON file (e.g. credentials.json)."
    )


# ------------------------------------------------------------------ #
# Re-export pure helpers from models for backwards compat.             #
# (calculate_split was originally here; it lives in app/models now.)   #
# ------------------------------------------------------------------ #
from app.models import split_ratios, calculate_split  # noqa: F401


# ------------------------------------------------------------------ #
# Line-item worksheet helper                                           #
# ------------------------------------------------------------------ #

_LINE_ITEMS_HEADERS: list[str] = [
    "Date", "Merchant", "Payer", "Product Name", "Price", "Qty", "Category", "Is Shared",
]


def _ensure_receipt_items_worksheet() -> gspread.Worksheet:
    """Open or create the ``Receipt_Items`` worksheet with headers."""
    spreadsheet = _open_sheet()
    existing_names = [ws.title for ws in spreadsheet.worksheets()]
    if "Receipt_Items" not in existing_names:
        worksheet = spreadsheet.add_worksheet(
            title="Receipt_Items", rows=1, cols=len(_LINE_ITEMS_HEADERS),
        )
        worksheet.clear()  # remove the empty default row added by add_worksheet
        worksheet.insert_row(_LINE_ITEMS_HEADERS, idx=1)
        logger.info("Created new 'Receipt_Items' worksheet with headers.")
        return worksheet

    worksheet = spreadsheet.worksheet("Receipt_Items")

    # Ensure headers exist even if someone deleted them.
    header_row = worksheet.row_values(1)
    if not header_row or header_row[0] != _LINE_ITEMS_HEADERS[0]:
        worksheet.clear()
        worksheet.insert_row(_LINE_ITEMS_HEADERS, idx=1)
        logger.info("Re-created 'Receipt_Items' headers.")

    return worksheet


def append_line_items(merchant: str, date: str, payer: str, items: list[dict]) -> int:
    """Append itemised line-item rows to the ``Receipt_Items`` sheet.

    Parameters
    ----------
    merchant :
        Store / vendor name.
    date :
        Purchase date (YYYY-MM-DD).
    payer :
        Roommate label who paid the receipt.
    items :
        List of dicts with keys: ``name``, ``price``, ``qty``, ``category``, ``is_shared``.

    Returns
    -------
    int
        Number of rows appended.
    """
    if not items:
        return 0

    worksheet = _ensure_receipt_items_worksheet()

    values = [
        [
            date,
            merchant,
            payer,
            item.get("name", ""),
            float(item.get("price", 0)),
            int(item.get("qty", 1)),
            item.get("category", ""),
            str(item.get("is_shared", True)),
        ]
        for item in items
    ]

    worksheet.append_rows(values, value_input="USER_ENTERED")
    logger.info("Appended %d Receipt_Items rows (merchant=%s)", len(items), merchant)
    return len(items)


# ------------------------------------------------------------------ #
# Google Sheets updaters                                               #
# ------------------------------------------------------------------ #

def _open_sheet() -> gspread.Spreadsheet:
    """Open the sharehouse ledger spreadsheet by ``GOOGLE_SHEET_ID``."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise RuntimeError(
            "Set GOOGLE_SHEET_ID environment variable to the target Sheet ID."
        )
    client = _get_gspread_client()
    return client.open_by_key(sheet_id)


async def append_transactions(entries: Sequence[LedgerEntry]) -> int:
    """Append one or more LedgerEntry rows to the 'Transactions' sheet.

    Parameters
    ----------
    entries :
        Parsed line-items ready to write.

    Returns
    -------
    int
        Number of rows successfully appended.
    """
    if not entries:
        return 0

    spreadsheet = _open_sheet()
    worksheet = spreadsheet.worksheet("Transactions")

    # Header row must already exist; we append values below it.
    header = [
        "date", "payer_phone", "payer_name", "merchant",
        "item_name", "price", "split_category",
        "A_bears", "B_bears", "C_bears",
    ]

    # gspread's append_all expects rows aligned to existing headers.
    values = [header] + [
        [
            entry.date,
            entry.payer_phone,
            entry.payer_name,
            entry.merchant,
            entry.item_name,
            entry.price,
            entry.split_category.value,
            entry.a_bears,
            entry.b_bears,
            entry.c_bears,
        ]
        for entry in entries
    ]

    # Use append_values to bulk-append rows.
    worksheet.append_rows(values, value_input="USER_ENTERED")
    logger.info("Appended %d transaction rows.", len(entries))
    return len(entries)


async def update_summary(
    date: str,
    payer_label: str,
    bearings: Dict[str, float],
    grand_total: float,
) -> SharehouseLedgerState:
    """Update running balances in the 'Summary Ledger' sheet.

    Standard sharehouse model::

        Balance = (total you paid for others) − (total others owe for you)

    When A pays $30 and items split evenly:
      - A's balance goes up by 30 (they are owed $10 each by B & C).
      - But tracking per-bearing is cleaner; net change = grand_total
        redistributed according to bearings.

    Parameters
    ----------
    date :
        Transaction date YYYY-MM-DD.
    payer_label :
        Roommate label ("A", "B", or "C") who paid the bill.
    bearings :
        Per-person bearing dict from ``calculate_split``.
    grand_total :
        Total amount of this transaction.

    Returns
    -------
    SharehouseLedgerState
        Updated balances after this transaction.
    """
    spreadsheet = _open_sheet()
    worksheet = spreadsheet.worksheet("Summary Ledger")

    # Read current row (most recent balance update).
    all_values = worksheet.get_all_values()
    if all_values:
        latest = all_values[-1]
        prev_a = float(latest[1]) if len(latest) > 1 else 0.0
        prev_b = float(latest[2]) if len(latest) > 2 else 0.0
        prev_c = float(latest[3]) if len(latest) > 3 else 0.0
    else:
        prev_a = prev_b = prev_c = 0.0

    # Net balance update: each person's bearing is what they *owe*.
    # The payer's effective recovery = grand_total − their own bearing.
    new_a = round(prev_a - bearings["A"] + (grand_total if payer_label == "A" else 0), 2)
    new_b = round(prev_b - bearings["B"] + (grand_total if payer_label == "B" else 0), 2)
    new_c = round(prev_c - bearings["C"] + (grand_total if payer_label == "C" else 0), 2)

    worksheet.append_row([date, "split_update", new_a, new_b, new_c])
    logger.info("Updated summary ledger: A=%.2f B=%.2f C=%.2f", new_a, new_b, new_c)

    return SharehouseLedgerState(
        balances=[
            RoommateBalance(name="Person A", phone="", balance=new_a),
            RoommateBalance(name="Person B", phone="", balance=new_b),
            RoommateBalance(name="Person C", phone="", balance=new_c),
        ],
    )


async def process_receipt(
    receipt: ReceiptData,
    payer_phone: str,
    roommate_map: Dict[str, str],
) -> SharehouseLedgerState:
    """End-to-end pipeline: parse bearings → write sheet → return balances.

    Parameters
    ----------
    receipt :
        Parsed ``ReceiptData`` from the vision service.
    payer_phone :
        WhatsApp sender phone number (with protocol prefix).
    roommate_map :
        Mapping of phone number prefix → roommate label.

    Returns
    -------
    SharehouseLedgerState
    """
    # Map phone to roommate label.
    payer_label = ""
    for pattern, name in roommate_map.items():
        if pattern in payer_phone:
            payer_label = name
            break
    if not payer_label:
        raise ValueError(f"Unknown payer phone: {payer_phone}")

    # Build LedgerEntry rows.
    entries = compute_bearings(receipt.items)
    for e in entries:
        e.date = receipt.date
        e.payer_phone = payer_phone
        e.payer_name = payer_label
        e.merchant = receipt.merchant  # type: ignore[assignment]

    # Write to Google Sheets.
    await append_transactions(entries)

    # Return current state (caller formats reply).
    return SharehouseLedgerState(
        balances=[
            RoommateBalance(name="Person A", phone="", balance=0.0),
            RoommateBalance(name="Person B", phone="", balance=0.0),
            RoommateBalance(name="Person C", phone="", balance=0.0),
        ],
    )


def compute_current_balances() -> Dict[str, float]:
    """Return the latest net balances for each roommate from Summary Ledger.

    Reads Sheet 2 (Summary Ledger) and extracts the most recent row's
    A_balance, B_balance, C_balance columns.
    Falls back to $0.00 when the sheet is empty or unavailable.
    """
    balances: Dict[str, float] = {
        "Person A": 0.0, "Person B": 0.0, "Person C": 0.0,
    }
    try:
        spreadsheet = _open_sheet()
        worksheet = spreadsheet.worksheet("Summary Ledger")
        all_values = worksheet.get_all_values()
        if not all_values:
            return balances

        # Summary Ledger columns: Roommate | Total Paid | Total Owed | Net Balance
        for row in reversed(all_values):
            if len(row) >= 4 and row[3]:  # column D = Net Balance
                name = row[0]
                try:
                    balances[name] = float(row[3])
                except (ValueError, TypeError):
                    pass
                break  # only the last data row matters
    except Exception:
        logger.exception("compute_current_balances failed")
    return balances
