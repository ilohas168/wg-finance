# WG Finance — Relational 3-Sheet Architecture Handoff

## Overview
WG Finance is a FastAPI Telegram bot + Streamlit dashboard that parses receipt photos via Groq Llama 3.2 Vision, categorizes line-items with `split_type` (Shared/Private) and `beneficiary`, and updates a relational Google Sheet model.

**Repository:** `ilohas168/wg-finance`  
**Live app:** Streamlit Cloud — auto-deploys from `main`

---

## 1. Relational Google Sheets Model (3 worksheets)

The old flat "Transactions / Summary Ledger" sheets remain as audit trail. The new dashboard writes to:

| Sheet | Headers | Notes |
|---|---|---|
| **Receipts** | `Receipt_ID`, `Date`, `Store`, `Paid_By`, `Header_Discounts`, `Grand_Total`, `Shared_Total`, `Notes` | One row per receipt. Auto-generated ID format: `REC-YYYYMMDD-HHMMSS`. |
| **Receipt_Items** | `Receipt_ID`, `Date`, `Store`, `Paid_By`, `Product_Name`, `Category`, `Qty`, `Unit_Price`, `Discount`, `Line_Total`, `Split_Type`, `Beneficiary` | One row per line-item. `Line_Total = Qty × Unit_Price − Discount`. `Split_Type ∈ {Shared, Private}`. `Beneficiary ∈ {A, B, C, AB, BC, AC, ALL}`. |
| **Settlements** | `Settlement_ID`, `Date`, `From_Roommate`, `To_Roommate`, `Amount`, `Method` | Between-roommate payments. Auto-generated ID: `SET-YYYYMMDD-HHMMSS`. |

### Balance Calculation Formula

For each line-item row in a receipt where payer = X:

1. **Payer out-of-pocket credit:** `Balance_X += Line_Total`
2. **Shared item (Split_Type == "Shared"):**  
   `n_beneficiaries = len(beneficiary_str)`  (3 for ALL, 2 for AB/BC/AC, 1 for single letter)  
   For each beneficiary Y ≠ X: `Balance_Y -= Line_Total / n_beneficiaries`
3. **Private item (Split_Type == "Private"):**  
   For the specific beneficiary Y: `Balance_Y -= Line_Total`

**Settlements:**
- `Balance_From -= amount`  (sender's balance goes down)
- `Balance_To += amount`    (receiver's balance goes up)

**Interpretation:**  
Balance > 0 → others owe this person money.  
Balance < 0 → this person owes others money.

### Ledger Service API (`app/services/ledger.py`)

| Function | Signature | Purpose |
|---|---|---|
| `_ensure_all_worksheets()` | `() -> dict[str, gspread.Worksheet]` | Create Receipts / Receipt_Items / Settlements sheets if missing (with headers). |
| `get_all_receipts()` | `() -> pd.DataFrame` | Read Receipts sheet → DataFrame. Empty DF on error. |
| `get_receipt_items(receipt_id)` | `(str) -> pd.DataFrame` | Read Receipt_Items sheet, optionally filtered by Receipt_ID. |
| `get_settlements()` | `() -> pd.DataFrame` | Read Settlements sheet → DataFrame. |
| `save_receipt(receipt_data, items)` | `(dict, list[dict]) -> str` | Append header to Receipts + rows to Receipt_Items. Returns receipt_id. |
| `update_receipt(receipt_id, data, items)` | `(str, dict, list[dict])` | Find + delete old matching rows (bottom→top), re-append corrected data. |
| `append_settlement(frm, to, amt, method)` | `(str, str, float, str) -> str` | Append row to Settlements sheet. Returns settlement_id. |

### Legacy Functions (removed in refactor)
- `append_transactions()` — wrote to old Transactions sheet
- `_ensure_receipt_items_worksheet()` / `append_line_items()` — old Receipt_Items format
- `update_summary()` / `process_receipt()` — Telegram-specific, not used by dashboard
- `compute_current_balances()` — legacy Summary Ledger reader (kept as compat stub returning zero balances)

---

## 2. LineItem & ItemizedReceipt Schemas (`app/services/vision_line_items.py`)

### LineItem (Pydantic BaseModel)
| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Descriptive item name |
| `price` | float | required | Unit price (positive for charges, negative for discounts). **Kept as `price`, NOT renamed to `unit_price`.** |
| `qty` | int | 1 | Quantity |
| `category` | str | "General" | One of Food / Drink / Toiletries / Household / General |
| `discount` | float | 0.0 | Line-level discount amount (positive for Rabatt/Aktion) |
| `split_type` | Literal["Shared", "Private"] | "Shared" | Who bears this item |
| `beneficiary` | str | "ALL" | One of A, B, C, AB, BC, AC, ALL |

### ItemizedReceipt (Pydantic BaseModel)
| Field | Type | Default | Description |
|---|---|---|---|
| `receipt_id` | str | "" | Auto-generated REC-YYYYMMDD-HHMMSS |
| `paid_by` | str | "Roommate 1" | Who paid this receipt (from dashboard sidebar) |
| `header_discounts` | float | 0.0 | Receipt-level subtotal discount |
| `merchant` | str | required | Store/merchant name |
| `date` | str | required | YYYY-MM-DD |
| `total_amount` | float | required | Grand total from receipt footer |
| `items` | List[LineItem] | required | Extracted line-items |

### `_SYSTEM_PROMPT` (Groq system message)
- **IMPORTANT:** Was previously defined as parenthesized implicit string concatenation with blank lines between blocks. This caused a Python 3.13 `SyntaxError: invalid syntax` because implicit concat inside parentheses breaks when blank lines separate adjacent strings on this platform.
- **Fix applied:** Converted to a single triple-quoted raw string (`"""..."""`) — one continuous assignment, no gaps. See commit `41f5b29`.

### Vision Parser Image Compression (`_compress_image()` helper)
- **Purpose:** Prevents Groq BadRequestError 413 "REQUEST ENTITY TOO LARGE" from large camera photos.
- Logic: Load with PIL → convert to RGB → if max(width, height) > 1600, downscale preserving aspect ratio using `Image.Resampling.LANCZOS` → export as JPEG at quality=85 + optimize=True → return compressed bytes.

---

## 3. Streamlit Dashboard (`dashboard.py`, root of repo)

### Sidebar
- Date range picker (defaults to month start → today)
- "Viewing as" roommate selector (radio button: Roommate 1 / 2 / 3)

### Tab 1 — Upload Receipt
- File uploader + camera_input → `parse_itemized_receipt(image_bytes)` returns `ItemizedReceipt` with new fields
- Line items in `st.data_editor` with column config:
  - `Category`: **SelectboxColumn** (was incorrectly `DropdownColumn` before fix)
  - `Split_Type`: SelectboxColumn (Shared / Private)
  - `Beneficiary`: SelectboxColumn (ALL, A, B, C, AB, BC, AC)
- Line_Total computed server-side: `Qty × Unit_Price − Discount`
- Summary metrics: Shared Total, Per-Roommate Share, Personal Total, Grand Total
- "Save to Google Sheets" → calls `ledger.save_receipt(receipt_data, items)`

### Tab 2 — Edit History
- Dropdown of all receipt_ids from `get_all_receipts()`
- On selection → loads items via `get_receipt_items(receipt_id)` into `st.data_editor` (same columns as Tab 1)
- "Update Receipt" button → calls `ledger.update_receipt(id, data, items)`

### Tab 3 — Balances & Settlements
- Computes per-roommate balances from Receipts + Receipt_Items sheets using the formula in §1
- Who-owes-whom matrix (cross-tab of positive/negative balance diffs)
- Settlement logging form: From/To (selectbox), Amount (number input), Method (Bank Transfer / Twint / Cash)

### Tab 4 — Parent Reports
- Filter receipts by date range + selected roommate
- "Total Spent" metric for the filtered set
- Displayed as `st.dataframe` with CSV download button

**Streamlit UI Fix applied:** `st.column_config.DropdownColumn` replaced with `st.column_config.SelectboxColumn` (the former doesn't exist in Streamlit). See commit `c137322`.

---

## 4. Credentials & Security

### Service Account Key
- **File:** `wg-finance-bot-6112de07abed.json` (project root)
- **Git ignored:** ✅ via `.gitignore` line 6 (`*.json` pattern). Verify: `git check-ignore -v wg-finance-bot-6112de07abed.json`

### Google Sheet
- **ID:** `18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0` (verified active with test phrase "koreyomeru?" in Sheet1)
- Has old sheets: `Sheet1`, `Transactions`, `Summary Ledger`
- New sheets auto-created on first `save_receipt()` call

### Streamlit Cloud Deployment Requirements
The app runs on Streamlit Community Cloud from the `main` branch. Credentials must be configured as **Streamlit secrets** (not environment variables), since the ephemeral cloud runtime has no local credential files:

In Streamlit Cloud dashboard settings, add these secrets:
```
GOOGLE_SHEET_ID = 18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0
GOOGLE_CREDENTIALS_JSON = <full JSON blob of service account key>
GROQ_API_KEY = gsk-...
TELEGRAM_BOT_TOKEN = 123456:AAFxxy...
```

In `dashboard.py`, these are accessed via `st.secrets["GOOGLE_SHEET_ID"]` and `st.secrets["GOOGLE_CREDENTIALS_JSON"]`.

### Local Development (`.env`)
| Variable | Description | Example |
|---|---|---|
| `GROQ_API_KEY` | Groq API key | `gsk-...` |
| `TELEGRAM_BOT_TOKEN` | BotFather token | `123456:AAFxxy...` |
| `GOOGLE_SHEET_ID` | Google Sheet ID | `18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0` |
| `GOOGLE_CREDENTIALS_JSON` | File path or JSON blob | `wg-finance-bot-6112de07abed.json` |

---

## 5. Test Suite

**65 tests across 4 files — all passing.** No new tests added for ledger/dashboard (Streamlit-heavy; tested live).

| File | Tests | Coverage |
|---|---|---|
| `test_groq.py` | 14 | Vision parser JSON validation, model fallback chain, markdown stripping, client validation |
| `test_ledger.py` | 18 | Split ratios, calculate_split dollar math, compute_bearings Chain with LedgerEntry creation |
| `test_telegram.py` | 16 | Telegram Update payload parsing, webhook routing for /balance, /status, /help, photo message routing |
| `test_vision.py` | 17 | ReceiptItem/ReceiptData Pydantic validation, negative prices, structured output round-trip |

**Run tests:** `python3 -m pytest tests/ -v`

### Important Test Fix (from Phase 2 refactor)
The import in `tests/test_ledger.py` was updated because `calculate_split` and `split_ratios` were moved to `app/models`:
```python
# Before:
from app.services.ledger import calculate_split

# After:
from app.models import calculate_split
```

---

## 6. Pending Issue — GOOGLE_SHEET_ID Not Loading in Streamlit Cloud

### Error Message
> "Error saving to Sheets: Set GOOGLE_SHEET_ID environment variable to the target Sheet ID."

### Root Cause
`GOOGLE_SHEET_ID` is loaded via `os.environ.get("GOOGLE_SHEET_ID", "")` in both:
- `app/services/ledger.py:_open_sheet()` (line 95) — **this needs fixing**
- `dashboard.py:SHEET_ID` (line 48) — also uses `os.environ`

In Streamlit Cloud, credentials are available via **`st.secrets`**, not `os.environ`. The current code falls back to empty string when the env var is missing, and `_open_sheet()` raises a RuntimeError.

### What Needs to Be Done
Update both loaders to check `st.secrets` first, then `os.environ`, with known default fallback:
```python
def _get_sheet_id():
    import streamlit as st  # lazy import for non-Streamlit contexts
    val = getattr(st, 'secrets', {}).get('GOOGLE_SHEET_ID') or os.environ.get('GOOGLE_SHEET_ID')
    return val or '18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0'
```

---

## 7. Git History (Relevant)

| Commit | Description |
|---|---|
| `c137322` | fix: use SelectboxColumn + downscale vision images to avoid 413 error |
| `41f5b29` | fix: triple-quoted _SYSTEM_PROMPT string (Python 3.13 syntax) |
| `93cdd6d` | chore: add *.json to .gitignore for credentials |
| `adff388` | feat: relational 3-sheet ledger, edit history, roommate balances |
| `bebc86e` | fix: remove hardcoded demo data from Product Analytics page |

---

## 8. Run Commands

```bash
# Development server (local)
uvicorn app.main:app --reload

# Streamlit dashboard (local)
streamlit run dashboard.py --server.port 8501

# Register Telegram webhook
python -c "from dotenv import load_dotenv; load_dotenv(); import asyncio; from app.main import startup; asyncio.run(startup())"

# Run tests
pytest -v

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
