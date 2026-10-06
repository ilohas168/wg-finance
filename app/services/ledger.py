"""Ledger engine — split calculation + Google Sheets persistence.

Handles the three core operations:
1. ``calculate_split``  — compute per-person bearings for one item.
2. ``save_receipt``  — write receipts to new 3-sheet model.
3. ``compute_current_balances``  — backward-compat stub (legacy Summary Ledger; returns empty).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from google.oauth2.service_account import Credentials
import gspread
import pandas as pd
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

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _unescape_private_key(info: dict) -> dict:
    """Replace literal ``\\n`` in *info[\"private_key\"]* with real newlines.

    Streamlit Cloud serialises secrets as JSON; a newline inside the
    service-account ``private_key`` becomes the two-character sequence
    ``\\n`` which the Google library cannot parse.  We fix it up here.
    """
    if "private_key" in info:
        raw = str(info["private_key"])
        if "\\n" in raw:
            info = dict(info)  # shallow copy to avoid mutating the original
            info["private_key"] = raw.replace("\\n", "\n")
    return info


# ------------------------------------------------------------------ #
# Google Sheets client factory                                         #
# ------------------------------------------------------------------ #

def _get_gspread_client() -> GSpreadClient:
    """Authenticate and return a gspread Client from service account JSON."""

    # -- 1. Service-account file path ----------------------------------- #
    cred_path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
    if cred_path:
        expanded = os.path.expanduser(cred_path)
        if os.path.isfile(expanded):
            try:
                creds = Credentials.from_service_account_file(
                    expanded, scopes=_SCOPES,
                )
                return gspread.authorize(creds)
            except Exception:
                logger.exception(
                    "Failed to load service-account file: %s", expanded,
                )

    # -- 2. Inline JSON env var ----------------------------------------- #
    cred_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if cred_json and cred_json.startswith("{"):
        try:
            info = json.loads(cred_json)
            info = _unescape_private_key(info)
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
            return gspread.authorize(creds)
        except Exception:
            logger.exception(
                "Failed to load GOOGLE_CREDENTIALS_JSON (inline JSON). "
                "Check that the private-key contains real newlines (not \\n)."
            )

    # -- 3. Streamlit secrets (dict form, e.g. ``gcp_service_account``) -#
    try:
        import streamlit as st  # type: ignore[import-not-found]
        secret = getattr(st, "secrets", {}).get("gcp_service_account", {})
        if isinstance(secret, dict):
            info = _unescape_private_key(dict(secret))
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
            return gspread.authorize(creds)
    except ImportError:
        pass  # not in a Streamlit context — skip.
    except Exception:
        logger.exception("Failed to load ``gcp_service_account`` from Streamlit secrets.")

    raise RuntimeError(
        "Neither GOOGLE_CREDENTIALS_JSON (file path or JSON blob), a "
        "GOOGLE_CREDENTIALS_FILE, nor ``st.secrets[\"gcp_service_account\"]`` "
        "was found. See app/services/ledger.py docstring."
    )


# ------------------------------------------------------------------ #
# Re-export pure helpers from models for backwards compat.             #
# (calculate_split was originally here; it lives in app/models now.)   #
# ------------------------------------------------------------------ #
from app.models import split_ratios, calculate_split  # noqa: F401


# ------------------------------------------------------------------ #
# New headers for the 3 sheets
# ------------------------------------------------------------------ #

_RECEIPT_HEADERS = [
    "Receipt_ID", "Date", "Store", "Paid_By", "Header_Discounts",
    "Grand_Total", "Shared_Total", "Notes"
]
_LINE_ITEMS_HEADERS_V2 = [
    "Receipt_ID", "Date", "Store", "Paid_By", "Product_Name",
    "Category", "Qty", "Unit_Price", "Discount", "Line_Total",
    "Split_Type", "Beneficiary"
]
_SETTLEMENT_HEADERS = [
    "Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"
]


# ------------------------------------------------------------------ #
# Sheet ID resolution (env → Streamlit secrets → known default)       #
# ------------------------------------------------------------------ #

_KNOWN_SHEET_ID = "18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0"


def _get_sheet_id() -> str:
    """Resolve the Google Sheet ID from multiple sources.

    Priority (highest to lowest):
      1. ``st.secrets["GOOGLE_SHEET_ID"]``   (when running inside Streamlit)
      2. ``GOOGLE_SHEET_ID`` environment variable
      3. Hard-coded known-good default
    """
    try:
        import streamlit as st  # type: ignore[import-not-found]
        val = getattr(st, "secrets", {}).get("GOOGLE_SHEET_ID")
        if val:
            return str(val)
    except (ImportError, AttributeError):
        pass

    val = os.environ.get("GOOGLE_SHEET_ID", "")
    if val:
        return str(val)

    return _KNOWN_SHEET_ID


# ------------------------------------------------------------------ #
# New Google Sheets helpers                                            #
# ------------------------------------------------------------------ #

def _open_sheet() -> gspread.Spreadsheet:
    """Open the sharehouse ledger spreadsheet by ``GOOGLE_SHEET_ID``."""
    sheet_id = _get_sheet_id()
    if not sheet_id:
        raise RuntimeError(
            "Set GOOGLE_SHEET_ID environment variable to the target Sheet ID."
        )
    try:
        client = _get_gspread_client()
    except Exception:
        logger.exception("Failed to create Google Sheets client.")
        raise
    try:
        return client.open_by_key(sheet_id)
    except Exception:
        logger.exception(
            "open_by_key(%r) failed — verify GOOGLE_SHEET_ID and service-account permissions.",
            sheet_id,
        )
        raise


def _ensure_all_worksheets() -> dict[str, gspread.Worksheet]:
    """Ensure all required worksheets exist and return a dict of them."""
    spreadsheet = _open_sheet()

    # Get existing worksheet names
    existing_names = [ws.title for ws in spreadsheet.worksheets()]

    # Define required worksheets
    required_sheets = ["Receipts", "Receipt_Items", "Settlements"]

    # Create any missing worksheets
    worksheets = {}
    for sheet_name in required_sheets:
        if sheet_name not in existing_names:
            # Calculate buffer size - using header length + some extra
            if sheet_name == "Receipts":
                headers = _RECEIPT_HEADERS
            elif sheet_name == "Receipt_Items":
                headers = _LINE_ITEMS_HEADERS_V2
            else:  # Settlements
                headers = _SETTLEMENT_HEADERS

            worksheet = spreadsheet.add_worksheet(
                title=sheet_name,
                rows=101,
                cols=len(headers) + 5  # Adding buffer columns
            )
            worksheet.clear()  # remove the empty default row added by add_worksheet
            worksheet.insert_row(headers, idx=1)
            logger.info(f"Created new '{sheet_name}' worksheet with headers.")
        else:
            worksheet = spreadsheet.worksheet(sheet_name)

        worksheets[sheet_name] = worksheet

    return worksheets


def get_all_receipts() -> pd.DataFrame:
    """Read all receipts from the 'Receipts' sheet."""
    try:
        spreadsheet = _open_sheet()
        worksheet = spreadsheet.worksheet("Receipts")
        values = worksheet.get_all_values()

        if not values or len(values) < 2:
            # Return empty DataFrame with required columns
            return pd.DataFrame(columns=_RECEIPT_HEADERS)

        # First row is headers
        df = pd.DataFrame(values[1:], columns=values[0])
        return df
    except Exception as e:
        logger.error(f"Error reading receipts: {e}")
        # Return empty DataFrame with required columns
        return pd.DataFrame(columns=_RECEIPT_HEADERS)


def get_receipt_items(receipt_id: str = None) -> pd.DataFrame:
    """Read receipt items from the 'Receipt_Items' sheet."""
    try:
        spreadsheet = _open_sheet()
        worksheet = spreadsheet.worksheet("Receipt_Items")
        values = worksheet.get_all_values()

        if not values or len(values) < 2:
            # Return empty DataFrame with required columns
            return pd.DataFrame(columns=_LINE_ITEMS_HEADERS_V2)

        # First row is headers
        df = pd.DataFrame(values[1:], columns=values[0])

        if receipt_id:
            df = df[df["Receipt_ID"] == receipt_id]

        return df
    except Exception as e:
        logger.error(f"Error reading receipt items: {e}")
        # Return empty DataFrame with required columns
        return pd.DataFrame(columns=_LINE_ITEMS_HEADERS_V2)


def get_settlements() -> pd.DataFrame:
    """Read all settlements from the 'Settlements' sheet."""
    try:
        spreadsheet = _open_sheet()
        worksheet = spreadsheet.worksheet("Settlements")
        values = worksheet.get_all_values()

        if not values or len(values) < 2:
            # Return empty DataFrame with required columns
            return pd.DataFrame(columns=_SETTLEMENT_HEADERS)

        # First row is headers
        df = pd.DataFrame(values[1:], columns=values[0])
        return df
    except Exception as e:
        logger.error(f"Error reading settlements: {e}")
        # Return empty DataFrame with required columns
        return pd.DataFrame(columns=_SETTLEMENT_HEADERS)


def save_receipt(receipt_data: dict, items: list[dict]) -> str:
    """Save receipt data and line items to Google Sheets."""
    # Generate receipt_id if not provided
    receipt_id = receipt_data.get("Receipt_ID")
    if not receipt_id:
        date = datetime.now()
        receipt_id = f"REC-{date:%Y%m%d-%H%M%S}"
        receipt_data["Receipt_ID"] = receipt_id

    # Ensure all worksheets exist
    worksheets = _ensure_all_worksheets()

    # Write receipt data to "Receipts" sheet
    receipt_row = [
        receipt_data.get("Receipt_ID", ""),
        receipt_data.get("Date", ""),
        receipt_data.get("Store", ""),
        receipt_data.get("Paid_By", ""),
        receipt_data.get("Header_Discounts", ""),
        receipt_data.get("Grand_Total", 0),
        receipt_data.get("Shared_Total", 0),
        receipt_data.get("Notes", "")
    ]

    # Handle NaN values for Google Sheets
    receipt_row = [str(x) if isinstance(x, float) and pd.isna(x) else x for x in receipt_row]

    worksheets["Receipts"].append_row(receipt_row, value_input_option="USER_ENTERED")

    # Write item rows to "Receipt_Items" sheet
    items_rows = []
    for item in items:
        row = [
            receipt_id,
            item.get("Date", ""),
            item.get("Store", ""),
            item.get("Paid_By", ""),
            item.get("Product_Name", ""),
            item.get("Category", ""),
            int(item.get("Qty", 1)),
            float(item.get("Unit_Price", 0)),
            float(item.get("Discount", 0)),
            float(item.get("Line_Total", 0)),
            item.get("Split_Type", ""),
            item.get("Beneficiary", "")
        ]

        # Handle NaN values for Google Sheets
        row = [str(x) if isinstance(x, float) and pd.isna(x) else x for x in row]
        items_rows.append(row)

    if items_rows:
        worksheets["Receipt_Items"].append_rows(items_rows, value_input_option="USER_ENTERED")

    return receipt_id


def update_receipt(receipt_id: str, receipt_data: dict, items: list[dict]):
    """Update an existing receipt in Google Sheets."""
    # Get current data to find rows
    try:
        spreadsheet = _open_sheet()

        # Find the receipt row in "Receipts" sheet
        receipts_ws = spreadsheet.worksheet("Receipts")
        receipt_values = receipts_ws.get_all_values()

        if not receipt_values or len(receipt_values) < 2:
            raise ValueError(f"No receipts found to update for {receipt_id}")

        # Find receipt rows (skip header row)
        receipt_indices = []
        for i, row in enumerate(receipt_values[1:], start=2):  # Start from 2 since row index is 1-based
            if len(row) > 0 and row[0] == receipt_id:
                receipt_indices.append(i)

        # Delete existing rows from bottom to top to avoid shifting issues
        for idx in sorted(receipt_indices, reverse=True):
            receipts_ws.batch_update({'deletions': str(idx)})

        # Find item rows in "Receipt_Items" sheet
        items_ws = spreadsheet.worksheet("Receipt_Items")
        items_values = items_ws.get_all_values()

        if not items_values or len(items_values) < 2:
            raise ValueError(f"No receipt items found to update for {receipt_id}")

        # Find item rows
        item_indices = []
        for i, row in enumerate(items_values[1:], start=2):  # Start from 2 since row index is 1-based
            if len(row) > 0 and row[0] == receipt_id:
                item_indices.append(i)

        # Delete existing rows from bottom to top to avoid shifting issues
        for idx in sorted(item_indices, reverse=True):
            items_ws.batch_update({'deletions': str(idx)})

        # Append new data
        save_receipt(receipt_data, items)

    except Exception as e:
        logger.error(f"Error updating receipt {receipt_id}: {e}")
        raise


def append_settlement(from_roommate: str, to_roommate: str, amount: float, method: str = "Bank Transfer") -> str:
    """Append a settlement record to the 'Settlements' sheet."""
    # Generate settlement_id
    date = datetime.now()
    settlement_id = f"SET-{date:%Y%m%d-%H%M%S}"

    # Ensure all worksheets exist
    worksheets = _ensure_all_worksheets()

    # Append settlement data
    row = [
        settlement_id,
        date.strftime("%Y-%m-%d"),
        from_roommate,
        to_roommate,
        float(amount),
        method
    ]

    # Handle NaN values for Google Sheets
    row = [str(x) if isinstance(x, float) and pd.isna(x) else x for x in row]

    worksheets["Settlements"].append_row(row, value_input_option="USER_ENTERED")

    return settlement_id


# ------------------------------------------------------------------ #
# Legacy compat — Telegram bot /balance & /status commands            #
# ------------------------------------------------------------------ #

def compute_current_balances() -> Dict[str, float]:
    """Return current net balances for each roommate (legacy Summary Ledger).

    The new balance calculation has moved to the dashboard's Balances & Settlements
    tab. This stub is kept for backward-compat with the Telegram bot's /balance
    and /status commands which still reference this function. It returns empty
    balances since the old Summary Ledger sheet is no longer updated.
    """
    return {"Shin": 0.0, "Fabian": 0.0, "Pierre": 0.0}
