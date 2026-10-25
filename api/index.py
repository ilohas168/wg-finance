"""JSON API for the WG Finance phone app (deployed as a Vercel Python function).

The phone app in ``public/`` talks only to this API. All money maths comes
from ``app.services.ledger_math`` (integer cents, tested), storage is the
existing Google Sheet via ``app.services.ledger``, logins use the Sheet's
``Users`` tab via ``app.services.auth``, and receipt photos are read by the
existing Groq parser.

Run locally (also serves the app from ``public/``)::

    uvicorn api.index:app --reload --port 8000
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.services import ledger  # noqa: E402
from app.services.accounting import round_shares_to_cents  # noqa: E402
from app.services.reporting import parse_receipt_dates  # noqa: E402
from app.services.ledger_math import (  # noqa: E402
    BENEFICIARY_LABELS,
    CODE_TO_ROOMMATE,
    ROOMMATE_CODES,
    ROOMMATES,
    ReceiptBreakdown,
    beneficiary_code,
    category_totals_cents,
    compute_balances,
    items_for_receipt,
    line_total_cents,
    money_to_cents,
    receipt_breakdowns,
    roommate_code,
    settle_up,
    summarise_period,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="WG Finance API", docs_url=None, redoc_url=None)

CATEGORIES = ["Food", "Drink", "Toiletries", "Household", "General"]
TOKEN_DAYS = 90
# Used only for local development without any secrets configured.
_DEV_SECRET = secrets.token_bytes(32)


# --------------------------------------------------------------------------- #
# Login tokens                                                                 #
# --------------------------------------------------------------------------- #

def _token_secret() -> bytes:
    """Signing key for login tokens; identical on every server instance.

    ``APP_SECRET`` wins. Otherwise derive it from secrets the deployment
    already has, so no extra setting is required to get started.
    """
    explicit = os.environ.get("APP_SECRET", "")
    if explicit:
        return explicit.encode()
    material = "".join(
        os.environ.get(name, "")
        for name in ("GOOGLE_CREDENTIALS_JSON_B64", "GOOGLE_CREDENTIALS_JSON", "GROQ_API_KEY")
    )
    if material:
        return hashlib.sha256(b"wg-finance-login:" + material.encode()).digest()
    # Local development without secrets: tokens last until the server restarts.
    return _DEV_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(username: str, name: str) -> str:
    payload = _b64(
        json.dumps(
            {"u": username, "n": name, "exp": int(time.time()) + TOKEN_DAYS * 86400},
            separators=(",", ":"),
        ).encode()
    )
    signature = _b64(hmac.new(_token_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_token(token: str) -> Optional[dict[str, Any]]:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(_token_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        return data if data.get("exp", 0) > time.time() else None
    except Exception:
        return None


def current_user(authorization: str = Header(default="")) -> dict[str, Any]:
    """FastAPI dependency: the logged-in user from ``Authorization: Bearer``."""
    token = authorization.removeprefix("Bearer ").strip()
    data = read_token(token) if token else None
    if not data:
        raise HTTPException(status_code=401, detail="Please log in again.")
    return data


# --------------------------------------------------------------------------- #
# Sheet access with a short cache (all users share one Google quota)           #
# --------------------------------------------------------------------------- #

_CACHE_SECONDS = 30
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def _read_sheets(fresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with _cache_lock:
        if not fresh and _cache["data"] is not None and time.time() - _cache["at"] < _CACHE_SECONDS:
            return _cache["data"]
    data = (ledger.get_all_receipts(), ledger.get_receipt_items(), ledger.get_settlements())
    with _cache_lock:
        _cache.update(at=time.time(), data=data)
    return data


def _invalidate() -> None:
    with _cache_lock:
        _cache.update(at=0.0, data=None)


@app.exception_handler(ledger.SheetsBusyError)
async def _busy_handler(_request, _exc):
    return JSONResponse(
        status_code=503,
        content={"detail": "Google Sheets is busy (too many requests). Try again in a minute."},
    )


# --------------------------------------------------------------------------- #
# Read model                                                                   #
# --------------------------------------------------------------------------- #

def _num(value: Any, default: float = 0.0) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(number) else float(number)


def _item_json(item: pd.Series) -> dict[str, Any]:
    qty = _num(item.get("Qty", 1), 1.0) or 1.0
    return {
        "name": str(item.get("Product_Name", "") or ""),
        "category": str(item.get("Category", "") or "General").strip() or "General",
        "qty": qty,
        "line_total": line_total_cents(item) / 100,
        "beneficiary": beneficiary_code(item.get("Beneficiary", "ALL")),
    }


def _receipt_json(b: ReceiptBreakdown, receipt: pd.Series, items: pd.DataFrame) -> dict[str, Any]:
    shares = round_shares_to_cents(b.exact_shares, b.allocated_cents)
    return {
        "id": b.receipt_id,
        "date": b.date.date().isoformat() if pd.notna(b.date) else None,
        "store": b.store,
        "payer": b.payer,
        "raw_payer": b.raw_payer,
        "total": b.paid_cents / 100,
        "items_total": b.allocated_cents / 100,
        "header_discount": money_to_cents(receipt.get("Header_Discounts", 0)) / 100,
        "notes": str(receipt.get("Notes", "") or ""),
        "manual": b.receipt_id.startswith("MAN-"),
        "mismatch": abs(b.difference_cents) > 1,
        "shares": {CODE_TO_ROOMMATE[c]: shares[c] / 100 for c in "ABC"},
        "items": [_item_json(item) for _, item in items.iterrows()],
    }


def _period_json(breakdowns: list[ReceiptBreakdown], items_df: pd.DataFrame) -> dict[str, Any]:
    summary = summarise_period(breakdowns)
    categories = category_totals_cents(breakdowns, items_df)
    stores: dict[str, int] = {}
    for b in breakdowns:
        stores[b.store] = stores.get(b.store, 0) + b.paid_cents
    return {
        "receipts": summary.receipt_count,
        "total_paid": summary.total_paid / 100,
        "total_share": summary.total_share / 100,
        "people": {
            name: {
                "paid": summary.paid[code] / 100,
                "share": summary.share[code] / 100,
                "receipts_paid": summary.receipts_paid[code],
            }
            for name, code in ROOMMATE_CODES.items()
        },
        "categories": sorted(
            ({"name": k, "amount": v / 100} for k, v in categories.items() if v > 0),
            key=lambda c: -c["amount"],
        ),
        "stores": sorted(
            ({"name": k, "amount": v / 100} for k, v in stores.items() if v > 0),
            key=lambda s: -s["amount"],
        )[:8],
        "mismatched": summary.mismatched_ids,
        "unknown_payer": summary.unknown_payer_ids,
    }


def build_state(
    receipts_df: pd.DataFrame, items_df: pd.DataFrame, settlements_df: pd.DataFrame
) -> dict[str, Any]:
    """Everything the phone app shows, computed once on the server."""
    breakdowns = receipt_breakdowns(receipts_df, items_df)
    rows = {
        str(r.get("Receipt_ID", "")).strip(): r for _, r in receipts_df.iterrows()
    } if "Receipt_ID" in receipts_df.columns else {}
    receipts = [
        _receipt_json(b, rows.get(b.receipt_id, pd.Series(dtype=object)), items_for_receipt(items_df, b.receipt_id))
        for b in breakdowns
    ]
    receipts.sort(key=lambda r: (r["date"] or "", r["id"]), reverse=True)

    balances = compute_balances(breakdowns, settlements_df)
    known = summarise_period([b for b in breakdowns if b.payer])
    sent = {c: 0 for c in "ABC"}
    received = {c: 0 for c in "ABC"}
    settlements = []
    for _, s in settlements_df.iterrows():
        frm, to = roommate_code(s.get("From_Roommate")), roommate_code(s.get("To_Roommate"))
        amount = money_to_cents(s.get("Amount", 0))
        if frm and to and frm != to and amount > 0:
            sent[frm] += amount
            received[to] += amount
        parsed = parse_receipt_dates(pd.Series([s.get("Date", "")])).iloc[0]
        settlements.append(
            {
                "id": str(s.get("Settlement_ID", "")),
                "date": parsed.date().isoformat() if pd.notna(parsed) else None,
                "from": CODE_TO_ROOMMATE.get(frm or "", str(s.get("From_Roommate", ""))),
                "to": CODE_TO_ROOMMATE.get(to or "", str(s.get("To_Roommate", ""))),
                "amount": amount / 100,
                "method": str(s.get("Method", "") or ""),
            }
        )
    settlements.sort(key=lambda s: s["date"] or "", reverse=True)

    months = sorted(
        {b.date.to_period("M") for b in breakdowns if pd.notna(b.date)}
        | {pd.Timestamp(date.today()).to_period("M")}
    )
    periods = {
        str(month): _period_json(
            [b for b in breakdowns if pd.notna(b.date) and b.date.to_period("M") == month], items_df
        )
        for month in months
    }
    periods["all"] = _period_json(breakdowns, items_df)

    return {
        "roommates": ROOMMATES,
        "categories": CATEGORIES,
        "beneficiaries": BENEFICIARY_LABELS,
        "receipts": receipts,
        "settlements": settlements,
        "balances": {CODE_TO_ROOMMATE[c]: balances[c] / 100 for c in "ABC"},
        "balance_parts": {
            CODE_TO_ROOMMATE[c]: {
                "paid": known.paid[c] / 100,
                "share": known.share[c] / 100,
                "settlements_paid": sent[c] / 100,
                "settlements_received": received[c] / 100,
            }
            for c in "ABC"
        },
        "unallocated": sum(balances.values()) / 100,
        "settle_up": [
            {"from": s["From"], "to": s["To"], "amount": s["Amount (CHF)"]}
            for s in settle_up(balances)
        ],
        "periods": periods,
    }


# --------------------------------------------------------------------------- #
# Write models                                                                 #
# --------------------------------------------------------------------------- #

class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class ItemIn(BaseModel):
    name: str = ""
    category: str = "General"
    qty: float = Field(default=1.0, gt=0)
    line_total: float
    beneficiary: str = "ALL"


class ReceiptIn(BaseModel):
    date: date
    store: str = Field(min_length=1)
    payer: str
    total: float
    header_discount: float = 0.0
    notes: str = ""
    manual: bool = False
    items: list[ItemIn] = Field(min_length=1)


class SettlementIn(BaseModel):
    from_roommate: str
    to_roommate: str
    amount: float = Field(gt=0)
    method: str = "Twint"


def _roommate_or_400(name: str) -> str:
    code = roommate_code(name)
    if not code:
        raise HTTPException(status_code=400, detail=f"Unknown roommate: {name}")
    return CODE_TO_ROOMMATE[code]


def _sheet_rows(body: ReceiptIn) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payer = _roommate_or_400(body.payer)
    day = body.date.isoformat()
    items = []
    shared_cents = 0
    for item in body.items:
        code = beneficiary_code(item.beneficiary)
        split_type = "Shared" if code == "ALL" or len(code) > 1 else "Private"
        cents = money_to_cents(item.line_total)
        if split_type == "Shared":
            shared_cents += cents
        items.append(
            {
                "Date": day,
                "Store": body.store.strip(),
                "Paid_By": payer,
                "Product_Name": item.name.strip() or body.store.strip(),
                "Category": item.category if item.category in CATEGORIES else "General",
                "Qty": float(item.qty),
                "Unit_Price": round(cents / 100 / item.qty, 4),
                "Discount": 0.0,
                "Line_Total": cents / 100,
                "Split_Type": split_type,
                "Beneficiary": code,
            }
        )
    notes = body.notes.strip()
    if body.manual and not notes.startswith("Manual entry"):
        notes = "Manual entry (no receipt)" + (f": {notes}" if notes else "")
    receipt = {
        "Date": day,
        "Store": body.store.strip(),
        "Paid_By": payer,
        "Header_Discounts": money_to_cents(body.header_discount) / 100,
        # The printed total is what the payer paid; it is stored as entered.
        "Grand_Total": money_to_cents(body.total) / 100,
        "Shared_Total": shared_cents / 100,
        "Notes": notes,
    }
    return receipt, items


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/login")
def login(body: LoginIn) -> dict[str, Any]:
    from app.services.auth import RESULT_BUSY, RESULT_OK, verify_user

    code, payload = verify_user(body.username, body.password)
    if code == RESULT_OK and payload:
        name = CODE_TO_ROOMMATE.get(roommate_code(payload) or "", payload)
        return {"token": make_token(body.username.strip().lower(), name), "name": name}
    if code == RESULT_BUSY:
        raise HTTPException(status_code=503, detail=payload)
    if code == "bad_creds":
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    raise HTTPException(status_code=500, detail="Could not reach the user list in Google Sheets.")


@app.post("/api/password")
def change_password(body: PasswordIn, user: dict = Depends(current_user)) -> dict[str, str]:
    from app.services.auth import change_password as change, verify_user, RESULT_OK

    code, _ = verify_user(user["u"], body.old_password)
    if code != RESULT_OK:
        raise HTTPException(status_code=400, detail="Your current password is not correct.")
    ok, message = change(user["u"], body.old_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message}


@app.get("/api/state")
def state(fresh: bool = Query(False), user: dict = Depends(current_user)) -> dict[str, Any]:
    data = build_state(*_read_sheets(fresh=fresh))
    data["me"] = user["n"] if user["n"] in ROOMMATES else None
    return data


@app.post("/api/parse")
async def parse(file: UploadFile = File(...), user: dict = Depends(current_user)) -> dict[str, Any]:
    from app.services.vision_line_items import parse_itemized_receipt

    image = await file.read()
    if not image:
        raise HTTPException(status_code=400, detail="The photo was empty.")
    try:
        parsed = parse_itemized_receipt(image)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Could not read this receipt: {exc}")
    except Exception as exc:  # network/model errors
        logger.exception("Receipt parsing failed")
        raise HTTPException(status_code=502, detail=f"The receipt reader failed: {exc}")
    parsed_date = parse_receipt_dates(pd.Series([parsed.date])).iloc[0]
    return {
        "store": parsed.merchant,
        "date": parsed_date.date().isoformat() if pd.notna(parsed_date) else date.today().isoformat(),
        "total": parsed.total_amount,
        "header_discount": parsed.header_discounts,
        "items": [
            {
                "name": item.name,
                "category": item.category if item.category in CATEGORIES else "General",
                "qty": item.qty,
                "line_total": item.price,
                "beneficiary": item.beneficiary,
            }
            for item in parsed.items
        ],
    }


@app.post("/api/receipts")
def create_receipt(body: ReceiptIn, user: dict = Depends(current_user)) -> dict[str, str]:
    receipt, items = _sheet_rows(body)
    prefix = "MAN" if body.manual else "REC"
    receipt["Receipt_ID"] = f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    receipt_id = ledger.save_receipt(receipt, items)
    _invalidate()
    return {"id": receipt_id}


@app.put("/api/receipts/{receipt_id}")
def update_receipt(receipt_id: str, body: ReceiptIn, user: dict = Depends(current_user)) -> dict[str, str]:
    receipt, items = _sheet_rows(body)
    receipt["Receipt_ID"] = receipt_id
    ledger.update_receipt(receipt_id, receipt, items)
    _invalidate()
    return {"id": receipt_id}


@app.delete("/api/receipts/{receipt_id}")
def delete_receipt(receipt_id: str, user: dict = Depends(current_user)) -> dict[str, int]:
    deleted_receipts, deleted_items = ledger.delete_receipt(receipt_id)
    _invalidate()
    return {"receipts": deleted_receipts, "items": deleted_items}


@app.post("/api/settlements")
def add_settlement(body: SettlementIn, user: dict = Depends(current_user)) -> dict[str, str]:
    frm, to = _roommate_or_400(body.from_roommate), _roommate_or_400(body.to_roommate)
    if frm == to:
        raise HTTPException(status_code=400, detail="Choose two different roommates.")
    settlement_id = ledger.append_settlement(frm, to, money_to_cents(body.amount) / 100, body.method)
    _invalidate()
    return {"id": settlement_id}


# Local development: serve the phone app too. On Vercel, public/ is served
# by the CDN and only /api/* reaches this function.
_PUBLIC = ROOT / "public"
if _PUBLIC.is_dir() and not os.environ.get("VERCEL"):

    @app.get("/{path:path}", include_in_schema=False)
    def _static(path: str):
        target = (_PUBLIC / path).resolve()
        if path and target.is_file() and _PUBLIC in target.parents:
            return FileResponse(target)
        return FileResponse(_PUBLIC / "index.html")
