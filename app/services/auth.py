"""User authentication backed by a Google Sheets ``Users`` worksheet.

No external auth providers (Auth0, Clerk).  The sheet is treated as the
identity store — password hashing is handled by werkzeug's ``generate_password_hash``
and ``check_password_hash``.

If the worksheet does not exist or has no data rows it is seeded with default
users so the app works out-of-the-box.

**Important:** This module must never execute network calls or Google Sheets API
requests at top-level import time.  Sheet initialisation happens lazily inside
each public function, wrapped in try/except to avoid crashing on import.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from werkzeug.security import generate_password_hash, check_password_hash  # type: ignore[import]

logger = logging.getLogger(__name__)

# Result codes returned by verify_user().
RESULT_OK = "ok"            # credentials valid — returns display name.
RESULT_BAD_CREDS = "bad_creds"   # user not found or wrong password.
RESULT_DB_ERROR = "db_error"     # couldn't connect / read Users sheet.

# Temporary default passwords — users should change these on first login.
_DEFAULT_PASSWORDS = {
    "shin": "changeme",
    "fabian": "changeme",
    "pierre": "changeme",
}


def _ensure_users_sheet() -> None:
    """Create the ``Users`` worksheet and seed default users if empty.

    Wrapped in try/except so a failed import does not crash the app.
    Uses lowercase usernames and title-cased display names.
    """
    from app.services.ledger import _open_sheet

    try:
        client = _open_sheet()
    except Exception:
        logger.warning("Could not open Google Sheet to seed Users worksheet.")
        return

    # Try to get existing worksheet; create if it doesn't exist.
    try:
        ws = client.worksheet("Users")
    except Exception:
        try:
            ws = client.add_worksheet(
                title="Users", rows=50, cols=4,
            )
            ws.insert_row(["Username", "PasswordHash", "Name"], idx=1)
        except Exception:
            logger.warning("Could not create Users worksheet.")
            return

    try:
        values = ws.get_all_values()
    except Exception:
        logger.warning("Could not read Users worksheet values.")
        return

    # If only headers (or empty), seed defaults.
    if len(values) <= 1:
        rows_to_add = []
        for username, password in _DEFAULT_PASSWORDS.items():
            try:
                user_lower = str(username).strip().lower()
                display_name = user_lower.title()
                hashed = generate_password_hash(password)
                rows_to_add.append([user_lower, hashed, display_name])
            except Exception:
                logger.warning("Could not hash password for %s.", username)
        if rows_to_add:
            try:
                ws.append_rows(rows_to_add)
                logger.info(
                    "Seeded %d default users into Users sheet.", len(rows_to_add),
                )
            except Exception:
                logger.warning("Could not append seeded rows to Users worksheet.")


def _get_users_df() -> Optional[List[list]]:
    """Read all rows from the ``Users`` worksheet.

    Returns a list-of-rows (each row is a list of strings) on success,
    or **``None``** when the sheet cannot be reached or an error occurs.
    **Never returns an empty DataFrame / list.**  Callers must distinguish
    "no connection" (``None``) from "no users found" (length < 2).
    """
    # Lazy import to avoid circular dependency and support standalone auth imports.
    from app.services.ledger import _open_sheet

    try:
        spreadsheet = _open_sheet()
    except Exception:
        logger.warning("Could not connect to Google Sheets for user lookup.")
        return None

    try:
        ws = spreadsheet.worksheet("Users")
    except Exception as exc:
        logger.warning("Users worksheet is unavailable: %s", exc)
        return None

    try:
        values = ws.get_all_values()
    except Exception:
        logger.warning("Could not read Users worksheet values.")
        return None

    # Empty or missing headers → treat as unreachable.
    if not values:
        logger.warning("Users worksheet returned no rows at all.")
        return None

    return values


def verify_user(username: str, password: str) -> Tuple[str, Optional[str]]:
    """Verify credentials against the ``Users`` sheet.

    Returns a ``(result_code, payload)`` pair:

    * ``(RESULT_OK, display_name)`` — login succeeded.
    * ``(RESULT_BAD_CREDS, None)``  — wrong username or password.
    * ``(RESULT_DB_ERROR, message)`` — couldn't reach the Users worksheet.

    Connection failures are **never** mistaken for bad credentials.
    """
    # Ensure the Users sheet is seeded before attempting login.
    _ensure_users_sheet()

    values = _get_users_df()
    if values is None:
        return RESULT_DB_ERROR, (
            "Unable to connect to Google Sheets authentication table. "
            "Please verify Streamlit Cloud Secrets."
        )

    if len(values) < 2:
        # Table has only headers or is empty — treat as no such user.
        return RESULT_BAD_CREDS, None

    # Locate columns from headers.
    headers = [str(h).strip().lower() for h in values[0]]
    uname_idx = headers.index("username") if "username" in headers else 0
    pw_idx = headers.index("passwordhash") if "passwordhash" in headers else 1

    username_lower = username.strip().lower()

    # Compare against every row.
    for row in values[1:]:
        if len(row) <= max(uname_idx, pw_idx):
            continue
        stored_user = str(row[uname_idx]).strip().lower()
        if stored_user != username_lower:
            continue

        # Username matched — now check the password.
        stored_hash = str(row[pw_idx])
        try:
            if check_password_hash(stored_hash, password):
                name_col = min(
                    pw_idx + 1, len(values[0]) - 1
                ) if len(values[0]) > pw_idx + 1 else uname_idx
                display_name = (
                    str(row[name_col]) if len(row) > name_col else username.title()
                )
                return RESULT_OK, display_name
        except Exception:
            logger.warning("Password verification failed for %s", username_lower)

    # No matching row found.
    return RESULT_BAD_CREDS, None


def change_password(username: str, old_password: str, new_password: str) -> Tuple[bool, str]:
    """Update a user's password in the ``Users`` sheet.

    Returns ``(success: bool, message: str)``.
    """
    _ensure_users_sheet()

    values = _get_users_df()
    if values is None:
        return False, "Unable to connect to Google Sheets authentication table."

    if len(values) < 2:
        return False, "No users found in the Authentication table."

    headers = [str(h).strip().lower() for h in values[0]]
    uname_idx = headers.index("username") if "username" in headers else 0
    pw_idx = headers.index("passwordhash") if "passwordhash" in headers else 1

    username_lower = username.strip().lower()

    # Locate and update the worksheet row for this user.
    try:
        from app.services.ledger import _open_sheet
        client = _open_sheet()
        ws = client.worksheet("Users")
    except Exception:
        return False, "Unable to connect to Google Sheets."

    # Find the row (1-indexed for gspread operations).
    for i, row in enumerate(values[1:], start=2):
        if len(row) > uname_idx and str(row[uname_idx]).strip().lower() == username_lower:
            try:
                new_hash = generate_password_hash(new_password)
                ws.update_cell(i, pw_idx + 1, new_hash)
                return True, "Password updated."
            except Exception:
                logger.warning("Could not update password for %s", username)
                return False, "Could not update password. Contact Shin."

    # Username not found in the sheet.
    return False, "Username not found in authentication table."
