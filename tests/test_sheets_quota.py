"""Google Sheets quota handling: one connection, clear "busy" login errors."""

from types import SimpleNamespace

import gspread
import pytest

from app.services import auth, ledger


def _api_error(status: int) -> gspread.exceptions.APIError:
    response = SimpleNamespace(
        status_code=status,
        json=lambda: {"error": {"code": status, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
        text="Quota exceeded",
    )
    return gspread.exceptions.APIError(response)


@pytest.fixture(autouse=True)
def _clear_spreadsheet_cache():
    ledger._SPREADSHEET_CACHE.clear()
    yield
    ledger._SPREADSHEET_CACHE.clear()


def test_rate_limit_errors_are_recognised():
    assert ledger.is_rate_limited(_api_error(429))
    assert not ledger.is_rate_limited(RuntimeError("boom"))


def test_spreadsheet_is_opened_once_and_reused(monkeypatch):
    opened = []

    class Client:
        def open_by_key(self, key):
            opened.append(key)
            return SimpleNamespace(key=key)

    monkeypatch.setattr(ledger, "_get_sheet_id", lambda: "sheet-1")
    monkeypatch.setattr(ledger, "_get_gspread_client", lambda: Client())

    first = ledger._open_sheet()
    second = ledger._open_sheet()

    assert first is second
    assert opened == ["sheet-1"]


class _UsersSheet:
    def __init__(self, values=None, error=None):
        self.values, self.error, self.reads = values, error, 0

    def worksheet(self, name):
        return self

    def get_all_values(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.values


def test_login_reads_the_users_sheet_once(monkeypatch):
    from werkzeug.security import generate_password_hash

    sheet = _UsersSheet(
        [["Username", "PasswordHash", "Name"], ["fabian", generate_password_hash("pw"), "Fabian"]]
    )
    monkeypatch.setattr(ledger, "_open_sheet", lambda: sheet)

    assert auth.verify_user(" Fabian ", "pw") == (auth.RESULT_OK, "Fabian")
    assert sheet.reads == 1


def test_quota_exhaustion_is_reported_as_busy_not_as_bad_secrets(monkeypatch):
    sheet = _UsersSheet(error=_api_error(429))
    monkeypatch.setattr(ledger, "_open_sheet", lambda: sheet)

    code, message = auth.verify_user("fabian", "pw")

    assert code == auth.RESULT_BUSY
    assert "try again" in message


def test_ledger_reads_raise_busy_instead_of_returning_empty(monkeypatch):
    sheet = _UsersSheet(error=_api_error(429))
    monkeypatch.setattr(ledger, "_open_sheet", lambda: sheet)

    with pytest.raises(ledger.SheetsBusyError):
        ledger.get_all_receipts()
