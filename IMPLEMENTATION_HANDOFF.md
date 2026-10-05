# WG Finance — Relational Sheets + Balances Dashboard Handoff

## Context

This is a **major architectural refactor** of the WG Finance Telegram/Streamlit expense tracker. The app currently has a flat sheet structure that doesn't support: editing historical receipts, tracking balances between roommates, or filtering by roommate. The user wants to migrate to a relational Google Sheets model + rebuild the dashboard with settlements/balances.

## What Was Planned (Phase 1–4 Plan)

A detailed implementation plan was written and approved at `/Users/shin/.claude/plans/validated-squishing-zebra.md`. It covers:
- **Phase 1**: Add `discount`, `split_type`, `beneficiary` to LineItem; add `receipt_id`, `paid_by`, `header_discounts` to ItemizedReceipt; update vision system prompt
- **Phase 2**: Migrate to 3-sheet model (Receipts, Receipt_Items, Settlements); new read/write methods with pandas DataFrame returns
- **Phase 3**: Dashboard rebuild — sidebar date range + roommate picker, 4 tabs (Upload, Edit History, Balances & Settlements, Parent Reports)
- **Phase 4**: NaN handling, st.spinner/toast/error feedback

## Files to Modify (Exactly 3)

### 1. `app/services/vision_line_items.py` (~200 lines)

**Current schemas:**
```python
class LineItem(BaseModel):
    name: str = Field(description="Descriptive item name.")
    price: float = Field(...)  # Unit price — keep as-is
    qty: int = Field(default=1, ...)
    category: str = Field(default="General", ...)

class ItemizedReceipt(BaseModel):
    merchant: str = Field(...)
    date: str = Field(...)
    total_amount: float = Field(...)  # Grand total
    items: List[LineItem]
```

**Changes needed:**
1. On LineItem, add AFTER `price` (rename `price` → `unit_price` to avoid confusion):
   - `unit_price: float = Field(...)` — rename existing `price`
   - `discount: float = Field(default=0.0)` — line-level discount
   - `split_type: Literal["Shared", "Private"] = Field(default="Shared")`
   - `beneficiary: str = Field(default="ALL")` — one of "A","B","C","AB","BC","AC","ALL"

2. On ItemizedReceipt, add BEFORE `items`:
   - `receipt_id: str = Field(default="")` — auto-generate as `REC-YYYYMMDD-HHMMSS`
   - `paid_by: str = Field(default="Roommate 1")` — from sidebar selection
   - `header_discounts: float = Field(default=0.0)`

3. Update `_SYSTEM_PROMPT` — add:
   - Default `split_type` to `"Shared"`, `beneficiary` to `"ALL"`
   - Line_Total formula: `(Qty × Unit_Price) − Discount`
   - All items must get a non-null category from Food/Drink/Toiletries/Household/General

### 2. `app/services/ledger.py` (~350 lines → ~400 lines)

**Delete these functions (old pattern):**
- `append_transactions()` — writes to obsolete Transactions sheet
- `_ensure_receipt_items_worksheet()`, `append_line_items()` — old Receipt_Items format
- `update_summary()`, `process_receipt()` — Telegram-specific, unused by dashboard

**Keep (unchanged):**
- `_get_gspread_client()` — shared helper
- `_open_sheet()` — shared helper

**New functions to add:**

```python
_LINE_ITEMS_HEADERS_V2 = [
    "Receipt_ID", "Date", "Store", "Paid_By", "Product_Name",
    "Category", "Qty", "Unit_Price", "Discount", "Line_Total",
    "Split_Type", "Beneficiary"
]
_RECEIPT_HEADERS = [
    "Receipt_ID", "Date", "Store", "Paid_By", "Header_Discounts",
    "Grand_Total", "Shared_Total", "Notes"
]
_SETTLEMENT_HEADERS = [
    "Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"
]

def _ensure_all_worksheets() -> dict[str, gspread.Worksheet]:
    """Create all 3 sheets if they don't exist. Returns {name: worksheet}."""
    # Check each sheet title in spreadsheet.worksheets()
    # Create missing ones with add_worksheet(title=..., rows=1, cols=N)
    # Clear default row and insert headers via clear() + insert_row(headers, idx=1)

def get_all_receipts() -> pd.DataFrame:
    """Read Receipts sheet → pandas DataFrame. Return empty DF on error."""

def get_receipt_items(receipt_id: str = None) -> pd.DataFrame:
    """Same but filter by Receipt_ID column if provided."""

def get_settlements() -> pd.DataFrame:
    """Read Settlements sheet → DataFrame."""

def save_receipt(receipt_data: dict, items: list[dict]) -> str:
    """Write header to Receipts + all items to Receipt_Items. Return receipt_id."""
    # Generate receipt_id = f"REC-{date:%Y%m%d-%H%M%S}" if not provided
    # Append both sheets

def update_receipt(receipt_id: str, receipt_data: dict, items: list[dict]):
    """Find + delete all rows matching receipt_id in both sheets, re-append corrected."""
    # Find row index in Receipts where col A == receipt_id
    # Delete that range from both sheets (gspread batch_update with dimension=ROWS)
    # Re-append corrected data

def append_settlement(from_roommate: str, to_roommate: str, amount: float, method: str) -> str:
    """Generate SET-{date} ID, append to Settlements sheet. Return settlement_id."""
```

### 3. `dashboard.py` — complete rewrite (~400 lines → ~500 lines)

**Sidebar (replace current radio):**
```python
with st.sidebar:
    st.title("WG Sharehouse Hub")
    date_range = st.date_input("Date Range", value=(today-30, today))
    selected_user = st.radio("Viewing as", ["Roommate 1", "Roommate 2", "Roommate 3"], key="selected_user")
```

**Tab 1 — Upload Receipt:**
Same flow (upload/camera → parse → edit), but map to new schema:
- `st.data_editor` columns: Product_Name, Category, Qty, Unit_Price, Discount, Split_Type, Beneficiary
- Line_Total is **computed**: `Qty * Unit_Price - Discount` (display only in editor or as a calculated column)
- Metrics row: Grand_Total = sum of all Line_Totals + header_discounts, Shared_Total = share between roommates

**Tab 2 — Edit History:**
- Dropdown from `get_all_receipts()` for receipt_id selection
- On select → `items = get_receipt_items(receipt_id)` → display in `st.data_editor`
- "Update Receipt" button → `ledger.update_receipt(id, data, items)` → toast

**Tab 3 — Balances & Settlements:**
Balance formula per roommate X:
```
balance_X = (total out-of-pocket spent by X)
           - (X's share of shared pool)
           + (settlements sent by X)
           - (settlements received by X)
where:
  "out-of-pocket" = sum(item.price for item where paid_by=X and split_type="Shared")
  "shared pool" = sum(all Shared items across all receipts in date range) / N_roommates
```

- Display "Who owes who?" matrix as a formatted table
- Settlement form via `st.form()`: From (radio), To (radio), Amount (number_input), Method (text_input → "Twint"/"Bank Transfer")

**Tab 4 — Parent Reports:**
- Filter all receipts by date range + selected roommates
- Show total metric per roommate: sum of X's balance
- `st.dataframe(filtered_df, use_container_width=True)`
- Download CSV button: `st.download_button("Download CSV", data=df.to_csv())`

## Test Strategy

**65 existing tests** across 4 files should all pass unchanged because no test depends on VisionLineItem schemas, Sheet structure, or dashboard code.

Run: `python3 -m pytest tests/ -v` after implementation to confirm.

## Current Branch State

```
Commit bebc86e: fix: remove hardcoded demo data from Product Analytics page
Branch: main → origin/main (GitHub: ilohas168/wg-finance)
Streamlit Cloud app: deploying automatically from main branch
Local Streamlit running on ports 8501 and 8502
```

## Current .env Secrets (DO NOT PUSH)
- `GROQ_API_KEY` — real API key
- `TELEGRAM_BOT_TOKEN` — real bot token
- `GOOGLE_SHEET_ID` — real Sheet ID
- `GOOGLE_CREDENTIALS_JSON` → credentials.json path

## Execution Order for Next Session
1. Vision schema changes (`vision_line_items.py`) — 15 min
2. Ledger rewrite (`ledger.py`) — 40 min
3. Dashboard rebuild (`dashboard.py`) — 60 min (biggest file)
4. `pytest` verification — 5 min

## Key Constraints & Gotchas
- **No `pytest-asyncio`**: project uses custom `asyncio_run()` helper in tests
- **No conftest.py**: all fixtures inline in test files
- **Streamlit Cloud**: `GOOGLE_CREDENTIALS_JSON` must be a JSON blob (not file path) since Streamlit runs in ephemeral env
- **nest_asyncio** is already installed for async inside Streamlit
- **gspread row deletion pattern**: get_all_values → filter out rows → worksheet.clear() → re-append. There's no native delete_rows().
- **test_ledger.py import fix after Phase 2**: `from app.services.ledger import calculate_split` must change to `from app.models import calculate_split` (we remove the re-export)

## Balance Calculation Formula (use exactly this)
```
For each receipt's items:
  payer += sum(all Line_Totals for this receipt)       # out-of-pocket credit
  
  For each roommate X in ["A","B","C"]:
    If X != payer AND split_type == "Shared":
      X -= (Line_Total / N_beneficiaries)              # shared share they didn't pay for
    
    If X != payer AND split_type == "Private" AND X in beneficiary:
      X -= Line_Total                                   # private item paid on their behalf

For each settlement row:
  From_Roommate -= amount                              # sender's balance decreases
  To_Roommate += amount                                # receiver's balance increases

Balance > 0 means "others owe this person money"
```

## Legacy Data Migration Strategy
**DO NOT attempt automatic data migration.** Read both old and new sheets in parallel during transition. The dashboard should compute balances from new Receipts + Settlements sheets only. Old Sheets (Transactions, Summary Ledger) remain untouched as audit trail. The balance formula is fundamentally different and cannot be mapped to the old format losslessly.

## Ready Prompt for Next Session

```
Continue the WG Finance architectural refactor. Here is the handoff:

/Users/shin/projects/personal-apps/wg-finance/IMPLEMENTATION_HANDOFF.md

And the approved plan at /Users/shin/.claude/plans/validated-squishing-zebra.md

Start with Phase 1: Update LineItem and ItemizedReceipt schemas in vision_line_items.py.
Add discount, split_type (Literal["Shared","Private"]), beneficiary fields to LineItem.
Add receipt_id, paid_by, header_discounts fields to ItemizedReceipt.
Rename current `price` field to `unit_price`.

Then proceed through all phases (1→4) sequentially. Run pytest after implementation.
```
