"""User authentication backed by a Google Sheets ``Users`` worksheet.

No external auth providers (Auth0, Clerk).  The sheet is treated as the
identity store — password hashing is handled by werkzeug's ``generate_password_hash``
and ``check_password_hash``.

If the worksheet does not exist or has no data rows it is seeded with default
users so the app works out-of-the-box.
"""

from __future__ import annotations

import logging

from werkzeug.security import generate_password_hash, check_password_hash  # type: ignore[import]

logger = logging.getLogger(__name__)

# Temporary default passwords — users should change these on first login.
_DEFAULT_PASSWORDS = {
    "shin": "changeme",
    "fabian": "changeme",
    "pierre": "changeme",
}


def _ensure_users_sheet() -> None:
    """Create the ``Users`` worksheet and seed default users if empty."""
    from app.services.ledger import _open_sheet

    client = _open_sheet()

    # Try to get existing worksheet; create if it doesn't exist.
    try:
        ws = client.worksheet("Users")
    except Exception:
        ws = client.add_worksheet(
            title="Users", rows=50, cols=4,
        )
        ws.insert_row(["Username", "PasswordHash", "Name"], idx=1)

    values = ws.get_all_values()

    # If only headers (or empty), seed defaults.
    if len(values) <= 1:
        rows_to_add = []
        for username, password in _DEFAULT_PASSWORDS.items():
            rows_to_add.append([
                username,
                generate_password_hash(password),
                username.title(),  # "Shin", "Fabian", "Pierre"
            ])
        if rows_to_add:
            ws.append_rows(rows_to_add)
            logger.info("Seeded %d default users into Users sheet.", len(rows_to_add))


# Initialize on import (only runs when module is loaded in-process).
_ensure_users_sheet()


def verify_user(username: str, password: str) -> str | None:
    """Verify credentials against the ``Users`` sheet.

    Returns the display *Name* on success, or ``None`` on failure.
    """
    from app.services.ledger import _open_sheet

    client = _open_sheet()

    try:
        ws = client.worksheet("Users")
    except Exception:
        return None

    values = ws.get_all_values()
    if len(values) < 2:
        return None

    # Locate column indices from headers.
    headers = [str(h).strip().lower() for h in values[0]]
    uname_idx = headers.index("username") if "username" in headers else 0
    pw_idx = headers.index("passwordhash") if "passwordhash" in headers else 1

    username_lower = username.strip().lower()
    name_col = min(pw_idx + 1, len(values[0]) - 1) if len(values[0]) > pw_idx + 1 else uname_idx

    for row in values[1:]:
        if len(row) <= max(uname_idx, pw_idx):
            continue
        if str(row[uname_idx]).strip().lower() != username_lower:
            continue
        stored_hash = str(row[pw_idx])
        if check_password_hash(stored_hash, password):
            # Return display name (last column).
            return str(row[name_col]) if len(row) > name_col else username.title()
    return None


def change_password(username: str, old_password: str, new_password: str) -> bool:
    """Update a user's password in the ``Users`` sheet.

    Returns ``True`` on success, ``False`` on failure.
    """
    from app.services.ledger import _open_sheet

    client = _open_sheet()

    try:
        ws = client.worksheet("Users")
    except Exception:
        return False

    values = ws.get_all_values()
    if len(values) < 2:
        return False

    headers = [str(h).strip().lower() for h in values[0]]
    uname_idx = headers.index("username") if "username" in headers else 0
    pw_idx = headers.index("passwordhash") if "passwordhash" in headers else 1

    username_lower = username.strip().lower()

    # Find the row (1-indexed for gspread operations).
    for i, row in enumerate(values[1:], start=2):
        if len(row) > uname_idx and str(row[uname_idx]).strip().lower() == username_lower:
            new_hash = generate_password_hash(new_password)
            ws.update_cell(i, pw_idx + 1, new_hash)
            return True

    return False
