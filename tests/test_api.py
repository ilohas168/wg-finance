"""Tests for the phone app's JSON API (api/index.py) against an in-memory sheet."""

from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api.index as api
from app.services import auth, ledger

RECEIPT_COLS = ["Receipt_ID", "Date", "Store", "Paid_By", "Header_Discounts", "Grand_Total", "Shared_Total", "Notes"]
ITEM_COLS = ["Receipt_ID", "Date", "Store", "Paid_By", "Product_Name", "Category", "Qty",
             "Unit_Price", "Discount", "Line_Total", "Split_Type", "Beneficiary"]


class FakeSheet:
    def __init__(self):
        self.receipts: list[dict] = []
        self.items: list[dict] = []
        self.settlements: list[dict] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(ledger, "get_all_receipts", lambda: pd.DataFrame(self.receipts, columns=RECEIPT_COLS).astype(str))
        monkeypatch.setattr(ledger, "get_receipt_items", lambda receipt_id=None: pd.DataFrame(self.items, columns=ITEM_COLS).astype(str))
        monkeypatch.setattr(
            ledger, "get_settlements",
            lambda: pd.DataFrame(self.settlements, columns=["Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"]),
        )
        monkeypatch.setattr(ledger, "save_receipt", self.save)
        monkeypatch.setattr(ledger, "update_receipt", self.update)
        monkeypatch.setattr(ledger, "delete_receipt", self.delete)
        monkeypatch.setattr(ledger, "append_settlement", self.settle)

    def save(self, receipt, items):
        self.receipts.append(receipt)
        self.items += [{**i, "Receipt_ID": receipt["Receipt_ID"]} for i in items]
        return receipt["Receipt_ID"]

    def update(self, rid, receipt, items):
        self.delete(rid)
        self.save(receipt, items)

    def delete(self, rid):
        before = (len(self.receipts), len(self.items))
        self.receipts = [r for r in self.receipts if r["Receipt_ID"] != rid]
        self.items = [i for i in self.items if i["Receipt_ID"] != rid]
        return before[0] - len(self.receipts), before[1] - len(self.items)

    def settle(self, frm, to, amount, method):
        self.settlements.append(
            {"Settlement_ID": f"SET-{len(self.settlements)}", "Date": "2026-10-20",
             "From_Roommate": frm, "To_Roommate": to, "Amount": amount, "Method": method}
        )
        return self.settlements[-1]["Settlement_ID"]


@pytest.fixture
def sheet(monkeypatch):
    fake = FakeSheet()
    fake.install(monkeypatch)
    api._invalidate()
    return fake


@pytest.fixture
def client(sheet, monkeypatch):
    monkeypatch.setattr(auth, "verify_user", lambda u, p: ("ok", "Shin") if p == "pw" else ("bad_creds", None))
    c = TestClient(api.app)
    token = c.post("/api/login", json={"username": "Shin", "password": "pw"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def test_login_rejects_wrong_password(sheet, monkeypatch):
    monkeypatch.setattr(auth, "verify_user", lambda u, p: ("bad_creds", None))
    response = TestClient(api.app).post("/api/login", json={"username": "x", "password": "y"})
    assert response.status_code == 401


def test_busy_login_is_a_503_with_a_friendly_message(sheet, monkeypatch):
    monkeypatch.setattr(auth, "verify_user", lambda u, p: ("busy", "Google Sheets is busy right now."))
    response = TestClient(api.app).post("/api/login", json={"username": "x", "password": "y"})
    assert response.status_code == 503
    assert "busy" in response.json()["detail"]


def test_state_requires_login(sheet):
    assert TestClient(api.app).get("/api/state").status_code == 401
    assert TestClient(api.app, headers={"Authorization": "Bearer forged.token"}).get("/api/state").status_code == 401


def test_cutlery_without_receipt_is_split_three_ways(client, sheet):
    today = date.today().isoformat()
    response = client.post(
        "/api/receipts",
        json={
            "date": today, "store": "Cutlery — plates, forks, knives", "payer": "Shin",
            "total": 52, "manual": True,
            "items": [{"name": "Cutlery", "category": "Household", "line_total": 52, "beneficiary": "ALL"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["id"].startswith("MAN-")

    state = client.get("/api/state").json()
    receipt = state["receipts"][0]
    assert receipt["manual"] and receipt["payer"] == "Shin" and receipt["total"] == 52
    assert sum(receipt["shares"].values()) == pytest.approx(52)
    assert state["balances"] == {"Shin": 34.66, "Fabian": -17.33, "Pierre": -17.33}
    assert {"from": "Fabian", "to": "Shin", "amount": 17.33} in state["settle_up"]
    assert state["me"] == "Shin"
    month = state["periods"][today[:7]]
    assert month["people"]["Shin"] == {"paid": 52.0, "share": 17.34, "receipts_paid": 1}
    assert month["categories"] == [{"name": "Household", "amount": 52.0}]


def test_edit_keeps_printed_total_and_flags_mismatch(client, sheet):
    rid = client.post(
        "/api/receipts",
        json={"date": "2026-10-03", "store": "Migros", "payer": "Fabian", "total": 30,
              "items": [{"name": "A", "line_total": 20, "beneficiary": "AB"},
                        {"name": "B", "line_total": 10, "beneficiary": "ALL"}]},
    ).json()["id"]

    client.put(
        f"/api/receipts/{rid}",
        json={"date": "2026-10-04", "store": "Migros", "payer": "Pierre", "total": 31,
              "items": [{"name": "A", "line_total": 20, "beneficiary": "AB"},
                        {"name": "B", "line_total": 10, "beneficiary": "ALL"}]},
    )
    receipt = client.get("/api/state").json()["receipts"][0]
    assert receipt["id"] == rid and receipt["payer"] == "Pierre" and receipt["date"] == "2026-10-04"
    assert receipt["total"] == 31 and receipt["items_total"] == 30 and receipt["mismatch"]
    assert receipt["shares"] == pytest.approx({"Shin": 13.34, "Fabian": 13.33, "Pierre": 3.33}, abs=0.011)


def test_settlement_squares_balances(client, sheet):
    client.post("/api/receipts", json={"date": "2026-10-03", "store": "Coop", "payer": "Shin", "total": 30,
                                       "items": [{"line_total": 30}]})
    client.post("/api/settlements", json={"from_roommate": "Fabian", "to_roommate": "Shin", "amount": 10})
    client.post("/api/settlements", json={"from_roommate": "Pierre", "to_roommate": "Shin", "amount": 10})
    state = client.get("/api/state").json()
    assert state["balances"] == {"Shin": 0, "Fabian": 0, "Pierre": 0}
    assert state["settle_up"] == []
    assert state["balance_parts"]["Shin"]["settlements_received"] == 20


def test_delete_removes_receipt(client, sheet):
    rid = client.post("/api/receipts", json={"date": "2026-10-03", "store": "Coop", "payer": "Shin",
                                             "total": 3, "items": [{"line_total": 3}]}).json()["id"]
    assert client.delete(f"/api/receipts/{rid}").json() == {"receipts": 1, "items": 1}
    assert client.get("/api/state").json()["receipts"] == []


def test_unknown_payer_is_rejected(client):
    response = client.post("/api/receipts", json={"date": "2026-10-03", "store": "X", "payer": "Bob",
                                                  "total": 1, "items": [{"line_total": 1}]})
    assert response.status_code == 400


def test_sheet_quota_error_becomes_503(client, monkeypatch):
    def busy():
        raise ledger.SheetsBusyError("busy")

    monkeypatch.setattr(ledger, "get_all_receipts", busy)
    api._invalidate()
    response = client.get("/api/state")
    assert response.status_code == 503


def test_routes_work_with_or_without_the_api_prefix(client):
    # Vercel's service rewrite may or may not strip /api before the request
    # reaches the function; both forms must reach the same handlers.
    assert client.get("/api/health").json()["app"] == "wg-finance-web"
    assert client.get("/health").json()["app"] == "wg-finance-web"
    assert client.get("/state").status_code == 200


def test_phone_app_files_are_served_and_unknown_api_paths_404(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>WG Finance</title>" in page.text
    assert client.get("/receipts/anything").headers["content-type"].startswith("text/html")
    assert client.get("/manifest.webmanifest").headers["content-type"] == "application/manifest+json"
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/api/nope").status_code == 404
    assert page.headers["cache-control"] == "no-cache"
    assert "s-maxage" in client.get("/styles.css").headers["cache-control"]
