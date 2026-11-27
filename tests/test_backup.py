"""Tests for the weekly ledger backup (app/services/backup.py and its API)."""

from datetime import date
from types import SimpleNamespace

import gspread
import pytest
from fastapi.testclient import TestClient

import api.index as api
from app.services import auth, backup, ledger


class FakeWorksheet:
    def __init__(self, title, values=None):
        self.title, self.values = title, [list(r) for r in (values or [])]

    def get_all_values(self):
        return [list(r) for r in self.values]

    def update(self, values, range_name="A1", value_input_option=None):
        assert range_name == "A1" and value_input_option == "RAW"
        self.values = [list(r) for r in values]

    def update_title(self, title):
        self.title = title


class FakeSpreadsheet:
    def __init__(self, tabs):
        self.tabs = [FakeWorksheet(t, v) for t, v in tabs.items()]

    def worksheets(self):
        return list(self.tabs)

    def worksheet(self, title):
        for ws in self.tabs:
            if ws.title == title:
                return ws
        raise gspread.exceptions.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols):
        assert title not in [w.title for w in self.tabs]
        ws = FakeWorksheet(title)
        self.tabs.append(ws)
        return ws

    def del_worksheet(self, ws):
        self.tabs.remove(ws)

    def titles(self):
        return [w.title for w in self.tabs]


def ledger_sheet():
    return FakeSpreadsheet({
        "Receipts": [["Receipt_ID", "Date", "Store"], ["R1", "2026-11-07", "Migros"], ["R2", "2026-11-20", "Coop"]],
        "Receipt_Items": [["Receipt_ID", "Product_Name", "Line_Total"], ["R1", "Milch", "1.60"]],
        "Settlements": [["Settlement_ID", "From_Roommate", "To_Roommate", "Amount"]],
        "Users": [["Username", "PasswordHash"], ["shin", "secret-hash"]],
    })


def test_backup_copies_every_ledger_tab_into_one_dated_tab():
    target = FakeSpreadsheet({"Sheet1": []})

    result = backup.run_backup(ledger_sheet(), target, today=date(2026, 11, 30))

    assert result["created"] == "Backup 2026-11-30"
    assert result["counts"] == {"Receipts": 2, "Receipt_Items": 1, "Settlements": 0}
    rows = target.worksheet("Backup 2026-11-30").values
    assert rows[0][:2] == ["### Receipts", "2 rows"]
    assert rows[1][:3] == ["Receipt_ID", "Date", "Store"]
    assert rows[3][:3] == ["R2", "2026-11-20", "Coop"]
    assert ["### Receipt_Items", "1 rows"] == rows[5][:2]
    assert ["R1", "Milch", "1.60"] == rows[7][:3]
    assert any(r[0] == "### Settlements" for r in rows)
    # Password hashes are not copied into the backup.
    assert not any("secret-hash" in cell for r in rows for cell in r)
    assert len({len(r) for r in rows}) == 1  # rectangular for the Sheets API


def test_scheduled_run_skips_when_last_backup_is_recent():
    target = FakeSpreadsheet({"Backup 2026-11-27": [["old"]]})

    skipped = backup.run_backup(ledger_sheet(), target, today=date(2026, 11, 30))
    weekly = backup.run_backup(ledger_sheet(), target, today=date(2026, 12, 3))

    assert skipped["skipped"] and skipped["created"] is None
    assert weekly["created"] == "Backup 2026-12-03"
    assert target.titles() == ["Backup 2026-11-27", "Backup 2026-12-03"]


def test_back_up_now_rewrites_todays_backup():
    target = FakeSpreadsheet({"Backup 2026-11-30": [["stale"]]})

    result = backup.run_backup(ledger_sheet(), target, force=True, today=date(2026, 11, 30))

    assert result["created"] == "Backup 2026-11-30"
    assert target.titles() == ["Backup 2026-11-30"]
    assert target.worksheet("Backup 2026-11-30").values[0][0] == "### Receipts"


def test_only_the_newest_eight_backups_are_kept():
    old = {f"Backup 2026-{m:02d}-01": [["x"]] for m in range(1, 11)}  # ten older backups
    target = FakeSpreadsheet({"Sheet1": [], **old})

    result = backup.run_backup(ledger_sheet(), target, today=date(2026, 11, 30))

    kept = backup.list_backups(target)
    assert len(kept) == 8 and kept[0] == "Backup 2026-11-30" and kept[-1] == "Backup 2026-04-01"
    assert result["backups"] == kept
    assert "Sheet1" in target.titles()  # other tabs are left alone


def test_missing_ledger_tab_is_backed_up_as_empty():
    source = FakeSpreadsheet({"Receipts": [["Receipt_ID"]]})
    rows, counts = backup.build_backup_rows(source)
    assert counts == {"Receipts": 0, "Receipt_Items": 0, "Settlements": 0}
    assert [r[0] for r in rows if r[0].startswith("###")] == ["### Receipts", "### Receipt_Items", "### Settlements"]


@pytest.fixture
def sheets(monkeypatch):
    source, target = ledger_sheet(), FakeSpreadsheet({"Sheet1": []})
    monkeypatch.setattr(ledger, "_open_sheet", lambda: source)
    monkeypatch.setattr(ledger, "_get_gspread_client", lambda: SimpleNamespace(open_by_key=lambda key: target))
    return target


def test_cron_endpoint_needs_no_login_and_returns_no_ledger_data(sheets):
    response = TestClient(api.app).get("/api/cron/backup")
    assert response.status_code == 200
    body = response.json()
    assert body["created"].startswith("Backup ") and body["skipped"] is False
    assert set(body) == {"created", "skipped", "latest"}
    # A second call the same week does nothing.
    assert TestClient(api.app).get("/api/cron/backup").json()["skipped"] is True


def test_back_up_now_and_status_require_login(sheets, monkeypatch):
    assert TestClient(api.app).post("/api/backup").status_code == 401
    monkeypatch.setattr(auth, "verify_user", lambda u, p: ("ok", "Shin"))
    client = TestClient(api.app)
    token = client.post("/api/login", json={"username": "shin", "password": "pw"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    made = client.post("/api/backup").json()
    status = client.get("/api/backup").json()

    assert made["counts"]["Receipts"] == 2
    assert status["backups"] == [made["created"]]
    assert status["sheet_url"].startswith("https://docs.google.com/spreadsheets/d/")


def test_unshared_backup_sheet_gives_a_clear_message(monkeypatch):
    def refuse(key):
        raise gspread.exceptions.SpreadsheetNotFound("not shared")

    monkeypatch.setattr(ledger, "_open_sheet", ledger_sheet)
    monkeypatch.setattr(ledger, "_get_gspread_client", lambda: SimpleNamespace(open_by_key=refuse))
    response = TestClient(api.app).get("/api/cron/backup")
    assert response.status_code == 503
    assert "shared" in response.json()["detail"]
