"""Streamlit dashboard for WG Sharehouse — upload, edit, balances, and reports.

Pages:
  1. **Upload Receipt** — upload a receipt image, parse with Groq
     vision, edit line-items in-place, then save to Google Sheets (new schema).
  2. **Past Receipts** — browse historical receipts, see roommate shares, edit or delete.
  3. **Balances & Settlements** — per-roommate balances, who-owes-whom matrix,
     settlement tracking.
  4. **Total Spendings** — monthly and all-time spending by roommate.
"""

from __future__ import annotations

import calendar
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from app.services.accounting import (
    recover_legacy_weighted_quantities,
    resolve_line_item_amounts,
    round_shares_to_cents,
)
from app.services.reporting import filter_receipts_for_period, parse_receipt_dates

# --------------------------------------------------------------------------- #
# Page config                                                                  #
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="WG Sharehouse Hub", layout="wide", page_icon="🏠")

# Theme-neutral styling: translucent borders and washes read correctly on both
# Streamlit's light and dark themes, so nothing here hard-codes a background.
st.markdown(
    """
    <style>
    .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }
    h1 { font-weight: 750; letter-spacing: -0.02em; }
    h2, h3 { font-weight: 650; letter-spacing: -0.01em; }
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128, 128, 128, 0.22);
        border-radius: 14px;
        padding: 0.85rem 1rem;
    }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; font-weight: 700; }
    .wg-hero {
        border-radius: 18px;
        padding: 1.4rem 1.6rem;
        margin-bottom: 1.2rem;
        background: linear-gradient(120deg, rgba(42,120,214,0.16), rgba(27,175,122,0.10) 60%, rgba(235,104,52,0.10));
        border: 1px solid rgba(128, 128, 128, 0.18);
    }
    .wg-hero .wg-eyebrow { font-size: 0.78rem; font-weight: 650; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.65; }
    .wg-hero .wg-title { font-size: 1.9rem; font-weight: 750; letter-spacing: -0.02em; margin: 0.15rem 0 0.25rem; }
    .wg-hero .wg-sub { font-size: 1rem; opacity: 0.8; }
    .wg-grid { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 1rem; }
    .wg-card {
        border: 1px solid rgba(128, 128, 128, 0.22);
        border-radius: 14px;
        padding: 0.9rem 1rem;
        background: rgba(128, 128, 128, 0.04);
    }
    .wg-card .wg-label { font-size: 0.8rem; opacity: 0.7; font-weight: 550; }
    .wg-card .wg-value { font-size: 1.55rem; font-weight: 750; letter-spacing: -0.01em; margin-top: 0.15rem; }
    .wg-card .wg-note { font-size: 0.8rem; opacity: 0.7; margin-top: 0.2rem; }
    .wg-person { border-left: 5px solid var(--wg-accent); }
    .wg-person-head { display: flex; align-items: center; gap: 0.6rem; }
    .wg-avatar {
        width: 2.1rem; height: 2.1rem; border-radius: 50%;
        display: inline-flex; align-items: center; justify-content: center;
        font-weight: 750; color: #fff; background: var(--wg-accent); flex-shrink: 0;
    }
    .wg-pill {
        display: inline-block; font-size: 0.75rem; font-weight: 650;
        padding: 0.12rem 0.55rem; border-radius: 999px; margin-top: 0.35rem;
        border: 1px solid rgba(128, 128, 128, 0.3);
    }
    .wg-pill-good { color: #0ca30c; border-color: rgba(12,163,12,0.45); background: rgba(12,163,12,0.10); }
    .wg-pill-bad { color: #d03b3b; border-color: rgba(208,59,59,0.45); background: rgba(208,59,59,0.10); }
    .wg-transfer { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }
    .wg-transfer .wg-who { font-weight: 600; }
    .wg-transfer .wg-arrow { opacity: 0.55; margin: 0 0.35rem; }
    .wg-transfer .wg-amount { font-size: 1.2rem; font-weight: 750; font-variant-numeric: tabular-nums; }
    .wg-muted { opacity: 0.7; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session-state helpers                                                        #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Named roommates — A=Shin, B=Fabian, C=Pierre                               #
# --------------------------------------------------------------------------- #

_DEFAULT_ROOMMATES = ["Shin", "Fabian", "Pierre"]
_ROOMMATE_INITIALS = {"Shin": "A", "Fabian": "B", "Pierre": "C"}

# Display labels for beneficiary values (mapped from internal codes A/B/C/AB/BC/AC/ALL).
_BENEFICIARY_LABELS: dict[str, str] = {
    "ALL": "All (Shin, Fabian, Pierre)",
    "A": "Shin",
    "B": "Fabian",
    "C": "Pierre",
    "AB": "Shin & Fabian",
    "BC": "Fabian & Pierre",
    "AC": "Shin & Pierre",
}

# Reverse mapping: display label → internal beneficiary code.
_LABEL_TO_BENEFICIARY = {v: k for k, v in _BENEFICIARY_LABELS.items()}


def _convert_items_to_sheets(items_df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame with *display* labels to sheets-compatible records."""
    records = []
    for _, row in items_df.iterrows():
        r = dict(row)
        ben = str(r.get("Beneficiary", "ALL"))
        if ben in _LABEL_TO_BENEFICIARY:
            r["Beneficiary"] = _LABEL_TO_BENEFICIARY[ben]
        records.append(r)
    return records


def _init_session() -> None:
    """Ensure all required keys exist in ``st.session_state``."""
    defaults = {
        "selected_user": "Shin",
        "current_tab": "Overview",
        "raw_items_df": None,
        "parsed_dict": None,
        "has_parsed": False,
        "save_status": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_session()

# --------------------------------------------------------------------------- #
# Sidebar                                                                      #
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.markdown("## 🏠 WG Sharehouse Hub")
    st.caption("Shared receipts, fair splits, and who owes whom.")

    # ------------------------------------------------------------------ #
    # Login / logout                                                       #
    # ------------------------------------------------------------------ #
    logged_in = st.session_state.get("logged_in_user")

    if not logged_in:
        with st.form("login_form"):
            st.subheader("Login")
            _uname = st.text_input("Username", key="login_username")
            _pw = st.text_input("Password", type="password", key="login_password")
            if st.form_submit_button("Login"):
                from app.services.auth import (  # noqa: E402
                    verify_user,
                    RESULT_OK,
                    RESULT_BAD_CREDS,
                    RESULT_DB_ERROR,
                )

                result_code, payload = verify_user(_uname, _pw)
                if result_code == RESULT_OK and payload:
                    st.session_state.logged_in_user = _uname.strip().lower()
                    st.session_state.logged_in_name = payload
                    st.success(f"Welcome, {payload}!")
                elif result_code == RESULT_DB_ERROR:
                    st.error(
                        "Unable to connect to Google Sheets authentication table. "
                        "Please check **Streamlit Cloud Secrets** (Settings → Secrets) and ensure:\n"
                        "- ``GOOGLE_CREDENTIALS_JSON`` contains the full service-account JSON on one line, or\n"
                        "- ``gcp_service_account`` is a TOML dict (not a raw string).\n"
                        "Check app logs for the specific parse error.",
                    )
                else:
                    # RESULT_BAD_CREDS — wrong username or password.
                    st.error("Invalid credentials.")
        with st.expander("Forgot password?"):
            st.caption("Contact Shin to reset your password via the Google Sheet.")
    else:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.success(f"Logged in as **{st.session_state.logged_in_name}**")
        with c2:
            if st.button("Logout", width="stretch"):
                del st.session_state.logged_in_user
                del st.session_state.logged_in_name
                st.rerun()
        with st.expander("Change password"):
            with st.form("change_pw_form"):
                _old = st.text_input("Current password", type="password", key="old_pw")
                _new = st.text_input("New password", type="password", key="new_pw")
                _confirm = st.text_input("Confirm new password", type="password", key="confirm_pw")
                if st.form_submit_button("Update"):
                    if _new != _confirm:
                        st.error("Passwords don't match.")
                    else:
                        from app.services.auth import change_password  # noqa: E402

                        ok, msg = change_password(logged_in, _old, _new)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

    st.divider()
    selected_user = st.radio(
        "Viewing as",
        options=_DEFAULT_ROOMMATES,
        index=_DEFAULT_ROOMMATES.index(st.session_state.selected_user) if st.session_state.selected_user in _DEFAULT_ROOMMATES else 0,
        key="selected_user",
    )

    # Guest-mode hint.
    if not logged_in:
        st.info("Viewing as guest — login to upload receipts.")

    st.divider()

    # Dynamic tab list — Tab 1 only visible when logged in.
    if st.session_state.get("current_tab") == "Edit History":
        st.session_state["current_tab"] = "Past Receipts"
    if st.session_state.get("current_tab") == "Parent Reports":
        st.session_state["current_tab"] = "Total Spendings"
    if st.session_state.get("current_tab") == "View History":
        st.session_state["current_tab"] = "Overview"
    if logged_in:
        tabs = ["Overview", "Upload Receipt", "Past Receipts", "Balances & Settlements", "Total Spendings"]
    else:
        tabs = ["Overview", "Balances & Settlements", "Total Spendings"]
    if st.session_state.get("current_tab") not in tabs:
        st.session_state["current_tab"] = tabs[0]

    _tab_icons = {
        "Overview": "✨",
        "Upload Receipt": "📸",
        "Past Receipts": "🧾",
        "Balances & Settlements": "⚖️",
        "Total Spendings": "📊",
    }
    selected_tab = st.radio(
        "Page",
        options=tabs,
        format_func=lambda tab: f"{_tab_icons.get(tab, '')}  {tab}",
        key="current_tab",
    )

# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #


def _beneficiary_code(value: Any) -> str:
    """Normalize a beneficiary display label or code to A/B/C/AB/BC/AC/ALL."""
    code = _LABEL_TO_BENEFICIARY.get(str(value), str(value))
    return code if code in {"A", "B", "C", "AB", "BC", "AC", "ALL"} else "ALL"


def _roommate_code(value: Any) -> Optional[str]:
    """Resolve a roommate name or code to the internal A/B/C code."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip().casefold()
    for roommate, code in _ROOMMATE_INITIALS.items():
        if text in {roommate.casefold(), code.casefold()}:
            return code
    return None


def _money_to_cents(value: Any) -> int:
    """Convert a sheet value to integer cents with normal currency rounding."""
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite():
            return 0
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _line_total_cents(item: Any) -> int:
    """Read a saved line total, deriving it only when the cell is blank."""
    line_total = item.get("Line_Total", "")
    if line_total is not None and str(line_total).strip() != "":
        return _money_to_cents(line_total)
    try:
        amount = (
            Decimal(str(item.get("Qty", 1)))
            * Decimal(str(item.get("Unit_Price", 0)))
            - Decimal(str(item.get("Discount", 0)))
        )
        return _money_to_cents(amount)
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _beneficiary_codes(value: Any) -> list[str]:
    """Return every roommate code responsible for an item's cost."""
    code = _beneficiary_code(value)
    return ["A", "B", "C"] if code == "ALL" else [part for part in "ABC" if part in code]


def _receipt_spending_exact(
    items_df: pd.DataFrame, header_discount_cents: int = 0
) -> tuple[dict[str, Decimal], int]:
    """Return exact cent shares and the receipt's net item total."""
    exact_spending = {code: Decimal(0) for code in "ABC"}
    if items_df.empty:
        return exact_spending, 0
    item_total_cents = 0
    for _, item in items_df.iterrows():
        amount = _line_total_cents(item)
        item_total_cents += amount
        codes = _beneficiary_codes(item.get("Beneficiary", "ALL"))
        exact_share = Decimal(amount) / len(codes)
        for code in codes:
            exact_spending[code] += exact_share

    # Apportion any receipt-wide discount by positive item spending before
    # rounding, so row-level remainders do not accumulate against one person.
    if header_discount_cents != 0:
        weights = {
            code: max(Decimal(0), amount)
            for code, amount in exact_spending.items()
        }
        weight_total = sum(weights.values())
        if not weight_total:
            weights = {code: Decimal(1) for code in "ABC"}
            weight_total = Decimal(len(weights))
        for code in "ABC":
            exact_spending[code] -= (
                Decimal(header_discount_cents) * weights[code] / weight_total
            )

    return exact_spending, item_total_cents - header_discount_cents


def _receipt_spending_cents(
    items_df: pd.DataFrame, header_discount_cents: int = 0
) -> dict[str, int]:
    """Allocate a receipt's net cost among its selected beneficiaries."""
    exact_spending, target_total_cents = _receipt_spending_exact(
        items_df, header_discount_cents
    )
    return round_shares_to_cents(exact_spending, target_total_cents)


def _calculate_balance_cents(
    receipts_df: pd.DataFrame,
    items_df: pd.DataFrame,
    settlements_df: pd.DataFrame,
    default_payer: str,
) -> dict[str, int]:
    """Compute net debts/credits using payer credits and beneficiary shares."""
    balances = {code: 0 for code in "ABC"}
    exact_spending = {code: Decimal(0) for code in "ABC"}
    allocated_total_cents = 0
    if not receipts_df.empty and "Receipt_ID" in receipts_df.columns:
        for _, receipt in receipts_df.iterrows():
            receipt_id = str(receipt.get("Receipt_ID", "")).strip()
            if not receipt_id or items_df.empty or "Receipt_ID" not in items_df.columns:
                continue
            receipt_items = items_df[
                items_df["Receipt_ID"].astype(str).str.strip() == receipt_id
            ]
            payer = _roommate_code(receipt.get("Paid_By")) or _roommate_code(default_payer)
            if not payer:
                continue
            header_discount = _money_to_cents(receipt.get("Header_Discounts", 0))
            receipt_exact_spending, receipt_allocated_total = _receipt_spending_exact(
                receipt_items, header_discount
            )
            allocated_total_cents += receipt_allocated_total
            for code, share in receipt_exact_spending.items():
                exact_spending[code] += share
            receipt_total = receipt.get("Grand_Total", "")
            paid_cents = (
                _money_to_cents(receipt_total)
                if receipt_total is not None and str(receipt_total).strip()
                else receipt_allocated_total
            )
            balances[payer] += paid_cents

    # Round cumulative spending once, avoiding a cent of bias per receipt.
    total_spending = round_shares_to_cents(exact_spending, allocated_total_cents)
    for code, share in total_spending.items():
        balances[code] -= share

    if not settlements_df.empty:
        for _, settlement in settlements_df.iterrows():
            sender = _roommate_code(settlement.get("From_Roommate"))
            receiver = _roommate_code(settlement.get("To_Roommate"))
            amount = _money_to_cents(settlement.get("Amount", 0))
            if sender and receiver and sender != receiver and amount > 0:
                # Positive means owed; paying a debt raises the sender's balance
                # toward zero and lowers the recipient's credit toward zero.
                balances[sender] += amount
                balances[receiver] -= amount
    return balances


def _split_type_for_beneficiary(value: Any) -> str:
    """Derive the legacy Shared/Private sheet field from the sole allocation."""
    code = _beneficiary_code(value)
    return "Shared" if code == "ALL" or len(code) > 1 else "Private"


def _clean_df_for_sheets(df: pd.DataFrame) -> pd.DataFrame:
    """Fill NaN values for Google Sheets compatibility.

    - String / object columns → ""
    - Numeric columns → 0
    - Convert Beneficiary display labels back to internal codes.
    """
    df = df.copy()
    # Fill string/object cols with ""
    str_cols = df.select_dtypes(include=["object"]).columns.tolist()
    for col in str_cols:
        df[col] = df[col].fillna("").astype(str)
    # Fill numeric cols with 0
    num_cols = df.select_dtypes(include=["number"]).columns.tolist()
    for col in num_cols:
        df[col] = df[col].fillna(0).astype(float)
    # Convert Beneficiary display labels back to internal codes.
    if "Beneficiary" in df.columns:
        df["Beneficiary"] = df["Beneficiary"].apply(_beneficiary_code)
        # Keep the existing Google Sheets column populated for older balance
        # calculations, while deriving it from the single beneficiary choice.
        df["Split_Type"] = df["Beneficiary"].apply(_split_type_for_beneficiary)
    return df


def _compute_line_totals(df: pd.DataFrame) -> pd.Series:
    """Compute Line_Total = Qty * Unit_Price - Discount."""
    qty = df.get("Qty", pd.Series([1] * len(df)))
    price = df.get("Unit_Price", pd.Series([0] * len(df)))
    discount = df.get("Discount", pd.Series([0] * len(df)))
    return (qty.astype(float)) * (price.astype(float)) - (discount.astype(float))


def _prepare_receipt_items_for_editing(items: pd.DataFrame) -> pd.DataFrame:
    """Normalize saved line items for the Past Receipts editor and calculations."""
    source = items.copy()
    if "Line_Total" in source.columns:
        qty = pd.to_numeric(
            source["Qty"] if "Qty" in source.columns else pd.Series(1, index=source.index),
            errors="coerce",
        ).fillna(1)
        unit_price = pd.to_numeric(
            source["Unit_Price"]
            if "Unit_Price" in source.columns
            else pd.Series(0, index=source.index),
            errors="coerce",
        ).fillna(0)
        discount = pd.to_numeric(
            source["Discount"]
            if "Discount" in source.columns
            else pd.Series(0, index=source.index),
            errors="coerce",
        ).fillna(0)
        calculated_total = qty * unit_price - discount
        saved_total = pd.to_numeric(source["Line_Total"], errors="coerce")
        source["Line_Total"] = saved_total.where(
            saved_total.notna(), calculated_total
        )

    result = recover_legacy_weighted_quantities(source).reset_index(drop=True)
    if "Product_Name" in result.columns:
        result["Product_Name"] = result["Product_Name"].fillna("").astype(str)
    if "Category" in result.columns:
        categories = {"Food", "Drink", "Toiletries", "Household", "General"}
        result["Category"] = result["Category"].fillna("").astype(str).apply(
            lambda value: value if value in categories else "General"
        )
    if "Beneficiary" in result.columns:
        result["Beneficiary"] = result["Beneficiary"].apply(
            lambda value: _BENEFICIARY_LABELS[_beneficiary_code(value)]
        )
    if "Line_Total" not in result.columns:
        result["Line_Total"] = _compute_line_totals(result)
    return result


def _edit_line_items_inline(items: pd.DataFrame, editor_key: str) -> pd.DataFrame:
    """Render editable line-item rows and reconcile edited amounts."""
    view_columns = [
        "Product_Name",
        "Category",
        "Qty",
        "Unit_Price",
        "Discount",
        "Line_Total",
        "Beneficiary",
    ]
    visible_columns = [column for column in view_columns if column in items.columns]
    visible_items = items[visible_columns].copy().reset_index(drop=True)
    category_options = ["Food", "Drink", "Toiletries", "Household", "General"]
    beneficiary_options = list(_BENEFICIARY_LABELS.values())

    edited_visible = st.data_editor(
        visible_items,
        column_config={
            "Product_Name": st.column_config.TextColumn("Name", width="medium"),
            "Category": st.column_config.SelectboxColumn(
                "Category", options=category_options, width="small"
            ),
            "Qty": st.column_config.NumberColumn(
                "Qty", min_value=0.0, step=0.001, format="%.3f", width="small"
            ),
            "Unit_Price": st.column_config.NumberColumn(
                "Unit Price (CHF)", step=0.01, format="%.4f", width="small"
            ),
            "Discount": st.column_config.NumberColumn(
                "Discount (CHF)", step=0.01, format="%.2f", width="small"
            ),
            "Line_Total": st.column_config.NumberColumn(
                "Line Total (CHF)", step=0.01, format="%.2f", width="small"
            ),
            "Beneficiary": st.column_config.SelectboxColumn(
                "Who pays for this item",
                options=beneficiary_options,
                width="medium",
            ),
        },
        column_order=visible_columns,
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        key=editor_key,
    )

    updated = items.copy().reset_index(drop=True)
    for column in visible_columns:
        updated[column] = edited_visible[column].reset_index(drop=True)

    for column, default in (("Qty", 1.0), ("Unit_Price", 0.0), ("Discount", 0.0), ("Line_Total", 0.0)):
        if column in updated.columns:
            updated[column] = pd.to_numeric(updated[column], errors="coerce").fillna(default)

    def number_value(row: pd.Series, column: str, default: float) -> float:
        value = pd.to_numeric(row.get(column, default), errors="coerce")
        return default if pd.isna(value) else float(value)

    for index in range(len(updated)):
        original = items.reset_index(drop=True).iloc[index]
        edited = updated.iloc[index]
        original_qty = number_value(original, "Qty", 1.0)
        original_price = number_value(original, "Unit_Price", 0.0)
        original_discount = number_value(original, "Discount", 0.0)
        original_total = number_value(original, "Line_Total", 0.0)
        resolved_total, resolved_price = resolve_line_item_amounts(
            original_qty,
            original_price,
            original_discount,
            original_total,
            float(edited.get("Qty", original_qty)),
            float(edited.get("Unit_Price", original_price)),
            float(edited.get("Discount", original_discount)),
            float(edited.get("Line_Total", original_total)),
        )
        updated.at[index, "Line_Total"] = resolved_total
        updated.at[index, "Unit_Price"] = resolved_price

    if "Beneficiary" in updated.columns:
        updated["Split_Type"] = updated["Beneficiary"].apply(
            _split_type_for_beneficiary
        )
    return updated


def _receipt_items_for_view(df_all_items: pd.DataFrame, receipt_id: str) -> pd.DataFrame:
    """Use unsaved inline edits for visible totals, otherwise use saved rows."""
    pending_key = f"past_receipt_items_{receipt_id}"
    pending_items = st.session_state.get(pending_key)
    if isinstance(pending_items, pd.DataFrame):
        return pending_items.copy()
    if "Receipt_ID" not in df_all_items.columns:
        return df_all_items.iloc[0:0].copy()
    return df_all_items[
        df_all_items["Receipt_ID"].astype(str).str.strip() == receipt_id
    ].copy()


def _load_items_df(raw_dict: Optional[dict]) -> pd.DataFrame:
    """Build an editable DataFrame from the vision parser output dict."""
    if raw_dict is None or not raw_dict.get("items"):
        return pd.DataFrame(columns=["Product_Name", "Category", "Qty", "Unit_Price", "Discount", "Line_Total", "Split_Type", "Beneficiary"])

    rows = []
    for item in raw_dict.get("items", []):
        qty = max(float(item.get("qty", 1) or 1), 0.001)
        line_total = float(item.get("price", 0.0))
        # The parser reads the final receipt line total. Convert it to the
        # editable unit-price representation without multiplying it twice.
        unit_price = line_total / qty
        discount = 0.0
        beneficiary = _beneficiary_code(item.get("beneficiary", "ALL"))
        rows.append({
            "Product_Name": item.get("name", ""),
            "Category": item.get("category", "General"),
            "Qty": qty,
            "Unit_Price": unit_price,
            "Discount": discount,
            "Line_Total": line_total,
            "Split_Type": _split_type_for_beneficiary(beneficiary),
            "Beneficiary": beneficiary,
        })
    df = pd.DataFrame(rows)
    for col in ["Qty", "Unit_Price", "Discount", "Line_Total"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    return df


def _summarise_receipt(df: pd.DataFrame, header_discounts: float) -> Dict[str, float]:
    """Calculate shared/personal totals from the DataFrame."""
    if df.empty:
        return {
            "shared_total": 0.0,
            "per_roommate_share": 0.0,
            "personal_total": 0.0,
            "grand_total": -header_discounts,
        }

    beneficiary_codes = df["Beneficiary"].apply(_beneficiary_code)
    shared_mask = beneficiary_codes.apply(lambda code: code == "ALL" or len(code) > 1)
    private_mask = ~shared_mask

    shared_total = float(df.loc[shared_mask, "Line_Total"].sum()) if shared_mask.any() else 0.0
    personal_total = float(df.loc[private_mask, "Line_Total"].sum()) if private_mask.any() else 0.0
    grand_total = round(float(df["Line_Total"].sum()) - header_discounts, 2)

    # Per-roommate share of shared pool: divide by number of beneficiaries
    per_roommate_share = round(shared_total / 3.0, 2)

    return {
        "shared_total": round(shared_total, 2),
        "per_roommate_share": per_roommate_share,
        "personal_total": round(personal_total, 2),
        "grand_total": grand_total,
    }


# --------------------------------------------------------------------------- #
# Presentation helpers — cards, ledger frames, and charts                      #
# --------------------------------------------------------------------------- #

# One fixed colour per roommate so the same person reads the same everywhere.
_ROOMMATE_COLORS = {"Shin": "#2a78d6", "Fabian": "#eb6834", "Pierre": "#1baf7a"}
_CODE_TO_ROOMMATE = {code: name for name, code in _ROOMMATE_INITIALS.items()}
# Single-series charts (categories, stores) use a hue no roommate owns.
_SINGLE_SERIES_COLOR = "#6250d6"
_NEUTRAL_SERIES_COLOR = "#a3a29c"
_PLOTLY_CONFIG = {"displayModeBar": False}


def _chf(amount: float, signed: bool = False) -> str:
    """Format a franc amount, optionally with an explicit +/− sign."""
    if signed:
        sign = "+" if amount > 0.004 else "−" if amount < -0.004 else ""
        return f"{sign}CHF {abs(amount):,.2f}"
    return f"CHF {amount:,.2f}"


def _hero(eyebrow: str, title: str, subtitle: str) -> None:
    """Render the page header banner."""
    st.markdown(
        f"""
        <div class="wg-hero">
          <div class="wg-eyebrow">{escape(eyebrow)}</div>
          <div class="wg-title">{escape(title)}</div>
          <div class="wg-sub">{escape(subtitle)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _stat_cards(cards: list[tuple[str, str, str]]) -> None:
    """Render a responsive row of (label, value, note) stat tiles."""
    tiles = "".join(
        f"""<div class="wg-card"><div class="wg-label">{escape(label)}</div>
        <div class="wg-value">{escape(value)}</div>
        <div class="wg-note">{escape(note)}</div></div>"""
        for label, value, note in cards
    )
    st.markdown(f'<div class="wg-grid">{tiles}</div>', unsafe_allow_html=True)


def _balance_status(balance: float) -> tuple[str, str]:
    """Return a (label, pill CSS class) pair describing a balance."""
    if balance > 0.004:
        return "▲ Is owed", "wg-pill wg-pill-good"
    if balance < -0.004:
        return "▼ Owes", "wg-pill wg-pill-bad"
    return "● Settled", "wg-pill"


def _balance_cards(balances: dict[str, float], highlight: Optional[str] = None) -> None:
    """Render one card per roommate with avatar, amount, and status pill."""
    tiles = []
    for name in _DEFAULT_ROOMMATES:
        balance = balances.get(name, 0.0)
        label, pill_class = _balance_status(balance)
        you = " · you" if name == highlight else ""
        tiles.append(
            f"""<div class="wg-card wg-person" style="--wg-accent:{_ROOMMATE_COLORS[name]}">
            <div class="wg-person-head"><span class="wg-avatar">{escape(name[0])}</span>
            <div><div class="wg-label">{escape(name)}{you}</div>
            <div class="wg-value">{escape(_chf(abs(balance)))}</div></div></div>
            <span class="{pill_class}">{label}</span></div>"""
        )
    st.markdown(f'<div class="wg-grid">{"".join(tiles)}</div>', unsafe_allow_html=True)


def _settle_up_steps(balances_cents: dict[str, int]) -> list[dict[str, Any]]:
    """Greedily pair debtors with creditors into the fewest transfers."""
    debtors = [
        [name, -balances_cents[_ROOMMATE_INITIALS[name]]]
        for name in _DEFAULT_ROOMMATES
        if balances_cents[_ROOMMATE_INITIALS[name]] < 0
    ]
    creditors = [
        [name, balances_cents[_ROOMMATE_INITIALS[name]]]
        for name in _DEFAULT_ROOMMATES
        if balances_cents[_ROOMMATE_INITIALS[name]] > 0
    ]
    steps = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        amount_cents = min(debtors[debtor_index][1], creditors[creditor_index][1])
        steps.append(
            {
                "From": debtors[debtor_index][0],
                "To": creditors[creditor_index][0],
                "Amount (CHF)": amount_cents / 100,
            }
        )
        debtors[debtor_index][1] -= amount_cents
        creditors[creditor_index][1] -= amount_cents
        if debtors[debtor_index][1] == 0:
            debtor_index += 1
        if creditors[creditor_index][1] == 0:
            creditor_index += 1
    return steps


def _transfer_card(step: dict[str, Any]) -> None:
    """Render one suggested settlement transfer."""
    frm, to = step["From"], step["To"]
    st.markdown(
        f"""<div class="wg-card wg-transfer" style="margin-bottom:0.6rem">
        <div><span class="wg-who" style="color:{_ROOMMATE_COLORS[frm]}">{escape(frm)}</span>
        <span class="wg-arrow">pays →</span>
        <span class="wg-who" style="color:{_ROOMMATE_COLORS[to]}">{escape(to)}</span></div>
        <div class="wg-amount">{escape(_chf(step["Amount (CHF)"]))}</div></div>""",
        unsafe_allow_html=True,
    )


def _receipt_ledger_frame(
    df_receipts: pd.DataFrame, df_all_items: pd.DataFrame
) -> pd.DataFrame:
    """One row per receipt: parsed date, payer, total, and each roommate's share."""
    columns = ["Receipt_ID", "Date", "Store", "Paid_By", "Total", *_DEFAULT_ROOMMATES]
    if df_receipts.empty or "Receipt_ID" not in df_receipts.columns:
        return pd.DataFrame(columns=columns)
    dates = (
        parse_receipt_dates(df_receipts["Date"])
        if "Date" in df_receipts.columns
        else pd.Series(pd.NaT, index=df_receipts.index)
    )
    rows = []
    for (_, receipt), parsed_date in zip(df_receipts.iterrows(), dates):
        receipt_id = str(receipt.get("Receipt_ID", "")).strip()
        if not receipt_id:
            continue
        receipt_items = _receipt_items_for_view(df_all_items, receipt_id)
        shares, allocated_cents = _receipt_spending_exact(
            receipt_items, _money_to_cents(receipt.get("Header_Discounts", 0))
        )
        shares = round_shares_to_cents(shares, allocated_cents)
        total_value = receipt.get("Grand_Total", "")
        total_cents = (
            _money_to_cents(total_value)
            if total_value is not None and str(total_value).strip()
            else allocated_cents
        )
        payer_code = _roommate_code(receipt.get("Paid_By"))
        rows.append(
            {
                "Receipt_ID": receipt_id,
                "Date": parsed_date,
                "Store": str(receipt.get("Store", "") or "").strip() or "Unknown store",
                "Paid_By": _CODE_TO_ROOMMATE.get(payer_code, str(receipt.get("Paid_By", ""))),
                "Total": total_cents / 100,
                **{name: shares[code] / 100 for name, code in _ROOMMATE_INITIALS.items()},
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _category_totals(df_all_items: pd.DataFrame, receipt_ids: set[str]) -> pd.Series:
    """Sum line totals per item category for the given receipts."""
    if df_all_items.empty or "Receipt_ID" not in df_all_items.columns:
        return pd.Series(dtype=float)
    items = df_all_items[
        df_all_items["Receipt_ID"].astype(str).str.strip().isin(receipt_ids)
    ]
    if items.empty:
        return pd.Series(dtype=float)
    amounts = items.apply(_line_total_cents, axis=1) / 100
    categories = (
        items["Category"].fillna("").astype(str).str.strip().replace("", "General")
        if "Category" in items.columns
        else pd.Series("General", index=items.index)
    )
    totals = amounts.groupby(categories).sum()
    return totals[totals > 0].sort_values()


def _style_fig(fig: go.Figure, height: int = 320) -> go.Figure:
    """Apply the shared quiet chart chrome: hairline grid, no clutter."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=36, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text=""),
        hoverlabel=dict(font_size=13),
        bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.18)", zeroline=False)
    return fig


def _show_fig(fig: go.Figure) -> None:
    st.plotly_chart(fig, config=_PLOTLY_CONFIG)


def _balance_chart(balances: dict[str, float]) -> go.Figure:
    """Diverging horizontal bars: right of zero is owed, left of zero owes."""
    names = list(reversed(_DEFAULT_ROOMMATES))
    values = [balances.get(name, 0.0) for name in names]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker=dict(color=[_ROOMMATE_COLORS[n] for n in names], cornerradius=4),
            text=[_chf(v, signed=True) for v in values],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{text}<extra></extra>",
        )
    )
    limit = max([abs(v) for v in values] + [1.0]) * 1.35
    fig.add_vline(x=0, line_width=1, line_color="rgba(128,128,128,0.6)")
    fig.update_xaxes(range=[-limit, limit], showticklabels=False)
    _style_fig(fig, height=220)
    fig.update_yaxes(showgrid=False)
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8))
    return fig


def _monthly_chart(ledger: pd.DataFrame, months: int = 12) -> Optional[go.Figure]:
    """Stacked monthly bars of each roommate's allocated share."""
    dated = ledger.dropna(subset=["Date"])
    if dated.empty:
        return None
    period = dated["Date"].dt.to_period("M")
    end = max(period.max(), pd.Timestamp(date.today()).to_period("M"))
    start = max(period.min(), end - (months - 1))
    month_index = pd.period_range(start, end, freq="M")
    monthly = (
        dated.groupby(period)[_DEFAULT_ROOMMATES].sum().reindex(month_index, fill_value=0)
    )
    labels = [p.strftime("%b %Y") for p in month_index]
    fig = go.Figure()
    for name in _DEFAULT_ROOMMATES:
        fig.add_bar(
            x=labels,
            y=monthly[name],
            name=name,
            marker=dict(color=_ROOMMATE_COLORS[name]),
            hovertemplate=f"{name}: CHF %{{y:,.2f}}<extra>%{{x}}</extra>",
        )
    totals = monthly.sum(axis=1)
    fig.add_scatter(
        x=labels,
        y=totals,
        mode="text",
        text=[f"{t:,.0f}" if t else "" for t in totals],
        textposition="top center",
        showlegend=False,
        hoverinfo="skip",
    )
    fig.update_layout(barmode="stack", legend_traceorder="normal")
    fig.update_yaxes(title_text="CHF", rangemode="tozero")
    return _style_fig(fig, height=340)


def _hbar_chart(totals: pd.Series, height: Optional[int] = None) -> go.Figure:
    """Single-series horizontal bar chart for category or store totals."""
    fig = go.Figure(
        go.Bar(
            x=totals.values,
            y=[str(i) for i in totals.index],
            orientation="h",
            marker=dict(color=_SINGLE_SERIES_COLOR, cornerradius=4),
            text=[f"{v:,.2f}" for v in totals.values],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: CHF %{x:,.2f}<extra></extra>",
        )
    )
    fig.update_xaxes(showticklabels=False, range=[0, float(totals.max() or 1) * 1.25])
    _style_fig(fig, height=height or max(180, 44 * len(totals) + 40))
    fig.update_yaxes(showgrid=False)
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8))
    return fig


def _load_ledger_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load receipts, line items, and settlements, degrading to empty frames."""
    try:
        with st.spinner("Loading the ledger…"):
            from app.services.ledger import get_all_receipts, get_receipt_items, get_settlements  # type: ignore

            return get_all_receipts(), get_receipt_items(), get_settlements()
    except Exception as exc:
        st.error(f"Failed to load data: {exc}")
        return (
            pd.DataFrame(columns=["Receipt_ID", "Date", "Store", "Paid_By"]),
            pd.DataFrame(columns=["Receipt_ID", "Line_Total", "Beneficiary"]),
            pd.DataFrame(columns=["Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"]),
        )


# =========================================================================== #
# Overview                                                                     #
# =========================================================================== #

if selected_tab == "Overview":
    df_receipts, df_all_items, df_settlements = _load_ledger_data()
    today = date.today()
    you = selected_user

    ledger = _receipt_ledger_frame(df_receipts, df_all_items)
    if ledger.empty:
        _hero(today.strftime("%A, %d %B %Y"), f"Hi {you} 👋", "No receipts yet — upload one to get the ledger going.")
        st.stop()

    balances_cents = _calculate_balance_cents(df_receipts, df_all_items, df_settlements, you)
    balances = {name: balances_cents[code] / 100 for name, code in _ROOMMATE_INITIALS.items()}
    your_balance = balances[you]
    if your_balance > 0.004:
        mood = f"Your roommates owe you {_chf(your_balance)}."
    elif your_balance < -0.004:
        mood = f"You owe {_chf(abs(your_balance))} — see Settle up below."
    else:
        mood = "You're all square. Nice."
    _hero(today.strftime("%A, %d %B %Y"), f"Hi {you} 👋", mood)

    month_period = pd.Timestamp(today).to_period("M")
    ledger_period = ledger["Date"].dt.to_period("M")
    this_month = ledger[ledger_period == month_period]
    last_month = ledger[ledger_period == month_period - 1]
    this_total = float(this_month["Total"].sum())
    last_total = float(last_month["Total"].sum())
    last_name = (month_period - 1).strftime("%B")
    if last_total > 0:
        change = (this_total - last_total) / last_total * 100
        trend_note = f"{'▲' if change >= 0 else '▼'} {abs(change):.0f}% vs {last_name} ({_chf(last_total)})"
    else:
        trend_note = f"Nothing recorded in {last_name}"
    your_share = float(this_month[you].sum())
    share_pct = f"{your_share / this_total * 100:.0f}% of household spending" if this_total else "No spending yet this month"
    paid_by_you = int((this_month["Paid_By"] == you).sum())
    first_date = ledger["Date"].min()
    _stat_cards(
        [
            (f"Spent in {today:%B}", _chf(this_total), trend_note),
            ("Your share this month", _chf(your_share), share_pct),
            ("Receipts this month", f"{len(this_month)}", f"{paid_by_you} paid by you"),
            (
                "All-time spending",
                _chf(float(ledger["Total"].sum())),
                f"{len(ledger)} receipts since {first_date:%b %Y}" if pd.notna(first_date) else f"{len(ledger)} receipts",
            ),
        ]
    )

    bal_col, settle_col = st.columns([3, 2], gap="large")
    with bal_col:
        st.subheader("Balances")
        st.caption("Right of the line: others owe them. Left: they owe others.")
        _show_fig(_balance_chart(balances))
    with settle_col:
        st.subheader("Settle up")
        steps = _settle_up_steps(balances_cents)
        if steps:
            st.caption("The fewest transfers that square everyone up.")
            for step in steps:
                _transfer_card(step)
        else:
            st.success("Everyone is settled up! 🎉")

    st.subheader("Monthly spending")
    st.caption("Each bar is a month; segments show each roommate's share of the items bought.")
    monthly_fig = _monthly_chart(ledger)
    if monthly_fig is not None:
        _show_fig(monthly_fig)

    cat_col, recent_col = st.columns(2, gap="large")
    with cat_col:
        category_scope = this_month if not this_month.empty else ledger
        st.subheader("Where the money goes")
        st.caption(
            f"Item categories in {today:%B}." if not this_month.empty
            else "Item categories, all time (nothing recorded this month yet)."
        )
        category_totals = _category_totals(df_all_items, set(category_scope["Receipt_ID"]))
        if category_totals.empty:
            st.info("No categorised items yet.")
        else:
            _show_fig(_hbar_chart(category_totals))
    with recent_col:
        st.subheader("Recent receipts")
        st.caption("The latest purchases logged to the ledger.")
        recent = ledger.sort_values("Date", ascending=False, na_position="last").head(8)
        st.dataframe(
            recent[["Date", "Store", "Paid_By", "Total"]],
            hide_index=True,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD.MM.YYYY"),
                "Paid_By": st.column_config.TextColumn("Paid by"),
                "Total": st.column_config.NumberColumn("Total", format="CHF %.2f"),
            },
        )


# =========================================================================== #
# Tab 1 — Upload Receipt                                                     #
# =========================================================================== #

elif selected_tab == "Upload Receipt":
    st.title("📸 Upload Receipt")
    st.caption("Send a receipt photo, edit line-items, and save to the shared ledger.")

    upload_col, preview_col = st.columns([3, 1], gap="large")
    with upload_col:
        uploaded_file = st.file_uploader("Upload receipt image", type=["png", "jpg", "jpeg"], key="file_uploader")
        image_bytes: Optional[bytes] = uploaded_file.getvalue() if uploaded_file is not None else None
        parse_clicked = st.button(
            "✨ Parse Receipt", type="primary", key="parse_btn", disabled=image_bytes is None
        )
    with preview_col:
        if image_bytes:
            st.image(image_bytes, caption="Receipt preview", width="stretch")
        else:
            st.caption("📷 A sharp, flat photo with the totals visible parses best.")

    if parse_clicked and image_bytes:
        try:
            from app.services.vision_line_items import parse_itemized_receipt  # type: ignore

            raw_dict = parse_itemized_receipt(image_bytes)
            st.session_state.parsed_dict = raw_dict.model_dump()
            st.session_state.raw_items_df = _load_items_df(raw_dict.model_dump())
            st.session_state["upload_item_editor_id"] = uuid4().hex
            st.session_state.receipt_total_input = float(raw_dict.total_amount)
            st.session_state.header_discounts_input = float(raw_dict.header_discounts)
            st.session_state.has_parsed = True
            parsed_receipt_date = parse_receipt_dates(
                pd.Series([raw_dict.date])
            ).iloc[0]
            st.session_state["upload_receipt_date"] = (
                parsed_receipt_date.date()
                if pd.notna(parsed_receipt_date)
                else date.today()
            )
            st.info(f"Parsed receipt from **{raw_dict.merchant}** on **{raw_dict.date}** — {len(raw_dict.items)} items.")
        except Exception as exc:
            st.error(f"Failed to parse receipt: {exc}")

    # Edit area & summary.
    if st.session_state.has_parsed and st.session_state.raw_items_df is not None and not st.session_state.raw_items_df.empty:
        df = st.session_state.raw_items_df.copy()

        if "upload_receipt_date" not in st.session_state:
            parsed_receipt_date = parse_receipt_dates(
                pd.Series([st.session_state.parsed_dict.get("date", "")])
            ).iloc[0]
            st.session_state["upload_receipt_date"] = (
                parsed_receipt_date.date()
                if pd.notna(parsed_receipt_date)
                else date.today()
            )
        receipt_date = st.date_input(
            "Receipt date",
            key="upload_receipt_date",
        )
        st.caption(
            "Check this against the receipt before saving. Swiss dates use day.month.year; "
            "OCR can sometimes swap day and month."
        )

        st.subheader("Line-items")
        st.caption(
            "Edit the Line Total directly in the table. Changes update the allocation immediately."
        )
        # Display labels for the Beneficiary column.
        _ben_options = list(_BENEFICIARY_LABELS.values())  # e.g. ["All (Shin…)", "Shin", ...]

        if "Category" in df.columns:
            valid_categories = {"Food", "Drink", "Toiletries", "Household", "General"}
            df["Category"] = df["Category"].fillna("").astype(str).apply(
                lambda value: value if value in valid_categories else "General"
            )
        # Convert internal codes → display labels for the editor.
        if "Beneficiary" in df.columns:
            df["Beneficiary"] = df["Beneficiary"].apply(
                lambda value: _BENEFICIARY_LABELS[_beneficiary_code(value)]
            )

        editor_id = st.session_state.setdefault("upload_item_editor_id", uuid4().hex)
        editor_version = st.session_state.get("upload_item_editor_version", 0)
        edited_df = _edit_line_items_inline(
            df.copy(),
            f"upload_line_items_{editor_id}_{editor_version}",
        )
        visible_columns = [
            column
            for column in [
                "Product_Name",
                "Category",
                "Qty",
                "Unit_Price",
                "Discount",
                "Line_Total",
                "Beneficiary",
            ]
            if column in df.columns
        ]
        if not edited_df[visible_columns].equals(
            df[visible_columns].reset_index(drop=True)
        ):
            st.session_state.raw_items_df = edited_df.copy()
            st.session_state["upload_item_editor_version"] = editor_version + 1
            st.rerun()

        # Re-capture edits into session state.
        for col in ["Qty", "Unit_Price", "Discount"]:
            if col in edited_df.columns:
                edited_df[col] = pd.to_numeric(edited_df[col], errors="coerce").fillna(0)
        edited_df["Split_Type"] = edited_df["Beneficiary"].apply(_split_type_for_beneficiary)
        st.session_state.raw_items_df = edited_df

        # Summary metrics.
        if "header_discounts_input" not in st.session_state:
            st.session_state.header_discounts_input = float(
                st.session_state.parsed_dict.get("header_discounts", 0.0)
            )
        header_disc = st.number_input(
            "Header discounts (CHF; subtract from items total)",
            min_value=0.0,
            step=0.01,
            format="%.2f",
            key="header_discounts_input",
        )
        summary = _summarise_receipt(edited_df, header_disc)
        parsed_total = float(st.session_state.parsed_dict.get("total_amount", 0.0)) if st.session_state.parsed_dict else 0.0
        if "receipt_total_input" not in st.session_state:
            st.session_state.receipt_total_input = parsed_total or summary["grand_total"]
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Shared Total", f"CHF {summary['shared_total']:,.2f}")
        with m2:
            st.metric("Per-Roommate Share", f"CHF {summary['per_roommate_share']:,.2f}")
        with m3:
            st.metric("Personal Total", f"CHF {summary['personal_total']:,.2f}")
        with m4:
            receipt_total = st.number_input(
                "Grand total on receipt (CHF)",
                min_value=0.0,
                step=0.01,
                format="%.2f",
                key="receipt_total_input",
            )

        # Grand total validation — compare line items sum vs receipt total.
        computed_sum = float(edited_df["Line_Total"].sum())
        amount_from_items = computed_sum - header_disc
        discrepancy = abs(amount_from_items - receipt_total)

        st.divider()
        st.subheader("Validation")
        if discrepancy > 0.1:
            st.warning(
                f"Items total after header discounts (CHF {amount_from_items:,.2f}) differs from "
                f"receipt total (CHF {receipt_total:,.2f}). "
                f"Discrepancy: CHF {discrepancy:,.2f}. "
                f"Check the item rows or correct the editable receipt total."
            )
        else:
            st.info(
                f"Items total after header discounts (CHF {amount_from_items:,.2f}) matches receipt total "
                f"(CHF {receipt_total:,.2f}). ✓"
            )

        # Save.
        col_save, _ = st.columns([1, 5])
        with col_save:
            save_clicked = st.button("Save to Google Sheets", type="primary", key="save_btn")

        if save_clicked and edited_df is not None and not edited_df.empty:
            try:
                with st.spinner("Saving to Google Sheets…"):
                    clean_df = _clean_df_for_sheets(edited_df)
                    from app.services.ledger import save_receipt  # type: ignore

                    # Payer auto-assignment (Phase 5): logged-in user is payer.
                    _payer = (
                        st.session_state.logged_in_name
                        if st.session_state.get("logged_in_user")
                        else selected_user
                    )

                    receipt_data = {
                        "Receipt_ID": f"REC-{datetime.now():%Y%m%d-%H%M%S}",
                        "Date": receipt_date.isoformat(),
                        "Store": st.session_state.parsed_dict.get("merchant", ""),
                        "Paid_By": _payer,
                        "Header_Discounts": header_disc,
                        "Grand_Total": receipt_total,
                        "Shared_Total": summary["shared_total"],
                        "Notes": "",
                    }
                    items = clean_df.to_dict(orient="records")
                    rid = save_receipt(receipt_data, items)
                st.toast(f"Saved receipt **{rid}**", icon="✅")
                st.success(f"Receipt **{rid}** saved successfully.")
            except Exception as exc:
                st.error(f"Error saving to Sheets: {str(exc)}")

    elif st.session_state.has_parsed and (st.session_state.raw_items_df is None or st.session_state.raw_items_df.empty):
        st.warning("Parsed receipt returned no items. Please add them manually.")


# =========================================================================== #
# Tab 2 — Past Receipts                                                        #
# =========================================================================== #

elif selected_tab == "Past Receipts":
    st.title("🧾 Past Receipts")
    st.caption("Review receipt totals and each roommate's allocated spending; edit or delete receipts.")
    if st.session_state.pop("clear_edit_receipt_select", False):
        st.session_state.pop("edit_receipt_select", None)
    delete_message = st.session_state.pop("receipt_delete_message", None)
    if delete_message:
        st.success(delete_message)
    receipt_date_message = st.session_state.pop("receipt_date_message", None)
    if receipt_date_message:
        st.success(receipt_date_message)

    try:
        with st.spinner("Loading receipts…"):
            from app.services.ledger import get_all_receipts, get_receipt_items  # type: ignore
            df_receipts = get_all_receipts()
            df_all_items = get_receipt_items()
    except Exception as exc:
        st.error(f"Failed to load receipts: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store"])
        df_all_items = pd.DataFrame(columns=["Receipt_ID", "Line_Total", "Beneficiary"])

    if not df_receipts.empty and "Receipt_ID" in df_receipts.columns:
        st.subheader("Receipt totals and roommate spending")
        summary_rows = []
        for _, receipt in df_receipts.iterrows():
            rid = str(receipt.get("Receipt_ID", "")).strip()
            receipt_items = _receipt_items_for_view(df_all_items, rid)
            header_discount = _money_to_cents(receipt.get("Header_Discounts", 0))
            shares = _receipt_spending_cents(receipt_items, header_discount)
            receipt_total_cents = _money_to_cents(receipt.get("Grand_Total", 0))
            allocated_total_cents = sum(shares.values())
            summary_rows.append(
                {
                    "Receipt ID": rid,
                    "Date": receipt.get("Date", ""),
                    "Store": receipt.get("Store", ""),
                    "Paid By": receipt.get("Paid_By", ""),
                    "Receipt Total (CHF)": receipt_total_cents / 100,
                    "Allocated Total (CHF)": allocated_total_cents / 100,
                    "Difference (CHF)": (allocated_total_cents - receipt_total_cents) / 100,
                    "Shin Share (CHF)": shares["A"] / 100,
                    "Fabian Share (CHF)": shares["B"] / 100,
                    "Pierre Share (CHF)": shares["C"] / 100,
                }
            )
        if summary_rows:
            st.dataframe(
                pd.DataFrame(summary_rows),
                hide_index=True,
                width="stretch",
                column_config={
                    column: st.column_config.NumberColumn(column, format="%.2f")
                    for column in [
                        "Receipt Total (CHF)",
                        "Allocated Total (CHF)",
                        "Difference (CHF)",
                        "Shin Share (CHF)",
                        "Fabian Share (CHF)",
                        "Pierre Share (CHF)",
                    ]
                },
            )
            if any(
                abs(_money_to_cents(row["Receipt Total (CHF)"]) - _money_to_cents(row["Allocated Total (CHF)"])) > 1
                for row in summary_rows
            ):
                st.warning(
                    "At least one receipt's item total differs from its printed receipt total. "
                    "Review those line items in Past Receipts before relying on its split."
                )

        receipt_dates = (
            parse_receipt_dates(df_receipts["Date"])
            if "Date" in df_receipts.columns
            else pd.Series(pd.NaT, index=df_receipts.index)
        )
        receipt_labels = {}
        for (_, receipt), parsed_date in zip(df_receipts.iterrows(), receipt_dates):
            receipt_id = str(receipt.get("Receipt_ID", "")).strip()
            if not receipt_id:
                continue
            date_label = parsed_date.strftime("%d.%m.%Y") if pd.notna(parsed_date) else "Date unknown"
            receipt_labels[receipt_id] = f"{date_label} · {receipt_id}"

        receipt_ids = [str(r) for r in df_receipts["Receipt_ID"].tolist()]
        selected_rid = st.selectbox(
            "Select Receipt",
            options=receipt_ids,
            format_func=lambda receipt_id: receipt_labels.get(receipt_id, receipt_id),
            key="edit_receipt_select",
        )

        if selected_rid:
            try:
                df_items = df_all_items[
                    df_all_items["Receipt_ID"].astype(str).str.strip() == selected_rid
                ].copy() if "Receipt_ID" in df_all_items.columns else df_all_items.iloc[0:0].copy()
                items_state_key = f"past_receipt_items_{selected_rid}"
                if items_state_key in st.session_state:
                    df_items = st.session_state[items_state_key].copy()
                elif not df_items.empty:
                    df_items = _prepare_receipt_items_for_editing(df_items)
                    st.session_state[items_state_key] = df_items.copy()

                receipt_row = df_receipts[
                    df_receipts["Receipt_ID"].astype(str).str.strip() == selected_rid
                ]
                selected_receipt = receipt_row.iloc[0] if not receipt_row.empty else pd.Series(dtype=object)
                selected_shares = _receipt_spending_cents(
                    df_items,
                    _money_to_cents(selected_receipt.get("Header_Discounts", 0)),
                )
                share_cols = st.columns(5)
                with share_cols[0]:
                    st.metric(
                        "Receipt Total",
                        f"CHF {_money_to_cents(selected_receipt.get('Grand_Total', 0)) / 100:,.2f}",
                    )
                with share_cols[1]:
                    st.metric("Allocated Total", f"CHF {sum(selected_shares.values()) / 100:,.2f}")
                for col, name, code in zip(share_cols[2:], _DEFAULT_ROOMMATES, "ABC"):
                    with col:
                        st.metric(f"{name}'s Share", f"CHF {selected_shares[code] / 100:,.2f}")
                selected_difference = sum(selected_shares.values()) - _money_to_cents(
                    selected_receipt.get("Grand_Total", 0)
                )
                if abs(selected_difference) > 1:
                    st.warning(
                        "Allocated item shares differ from the receipt total by "
                        f"CHF {selected_difference / 100:,.2f}. Correct the line items before saving."
                    )

                current_date = parse_receipt_dates(
                    pd.Series([selected_receipt.get("Date", "")])
                ).iloc[0]
                date_default = (
                    current_date.date() if pd.notna(current_date) else date.today()
                )
                edited_receipt_date = st.date_input(
                    "Receipt date",
                    value=date_default,
                    key=f"receipt_date_{selected_rid}",
                )
                st.caption("This date determines which month appears in Total Spendings.")
                if st.button("Save receipt date", key=f"save_receipt_date_{selected_rid}"):
                    try:
                        with st.spinner("Updating receipt date…"):
                            from app.services.ledger import update_receipt_date  # type: ignore

                            update_receipt_date(
                                selected_rid,
                                edited_receipt_date.isoformat(),
                            )
                        st.session_state["receipt_date_message"] = (
                            f"Updated {selected_rid} to {edited_receipt_date.isoformat()}."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Error updating receipt date: {exc}")

                if not df_items.empty:
                    st.subheader("Line-items")
                    st.caption(
                        "Edit Line Total in the row. The allocated total updates immediately; "
                        "click Update Receipt to save."
                    )
                    editor_version_key = f"past_receipt_editor_version_{selected_rid}"
                    editor_version = st.session_state.get(editor_version_key, 0)
                    editor_key = (
                        f"past_receipt_line_items_{selected_rid}_{editor_version}"
                    )
                    edited_df = _edit_line_items_inline(
                        df_items,
                        editor_key,
                    )
                    visible_columns = [
                        column
                        for column in [
                            "Product_Name",
                            "Category",
                            "Qty",
                            "Unit_Price",
                            "Discount",
                            "Line_Total",
                            "Beneficiary",
                        ]
                        if column in df_items.columns
                    ]
                    if not edited_df[visible_columns].equals(
                        df_items[visible_columns].reset_index(drop=True)
                    ):
                        st.session_state[items_state_key] = edited_df.copy()
                        st.session_state[editor_version_key] = editor_version + 1
                        st.rerun()
                    st.session_state[items_state_key] = edited_df.copy()

                    col_update, _ = st.columns([1, 5])
                    with col_update:
                        update_clicked = st.button("Update Receipt", type="primary", key="update_btn")

                    if update_clicked and not edited_df.empty:
                        try:
                            with st.spinner("Updating receipt…"):
                                clean_df = _clean_df_for_sheets(edited_df)
                                from app.services.ledger import update_receipt  # type: ignore

                                receipt_data = {
                                    "Receipt_ID": selected_rid,
                                    "Date": df_receipts[df_receipts["Receipt_ID"] == selected_rid]["Date"].iloc[0] if selected_rid in df_receipts["Receipt_ID"].values else "",
                                    "Store": df_receipts[df_receipts["Receipt_ID"] == selected_rid]["Store"].iloc[0] if selected_rid in df_receipts["Store"].values else "",
                                    "Paid_By": selected_receipt.get("Paid_By", selected_user),
                                    "Header_Discounts": _money_to_cents(selected_receipt.get("Header_Discounts", 0)) / 100,
                                    "Grand_Total": _summarise_receipt(
                                        edited_df,
                                        _money_to_cents(selected_receipt.get("Header_Discounts", 0)) / 100,
                                    )["grand_total"],
                                    "Shared_Total": _summarise_receipt(
                                        edited_df,
                                        _money_to_cents(selected_receipt.get("Header_Discounts", 0)) / 100,
                                    )["shared_total"],
                                    "Notes": "",
                                }
                                items = clean_df.to_dict(orient="records")
                                update_receipt(selected_rid, receipt_data, items)
                            st.toast(f"Updated receipt **{selected_rid}**", icon="✅")
                            st.success(f"Receipt **{selected_rid}** updated successfully.")
                            st.session_state.pop(items_state_key, None)
                            st.session_state[editor_version_key] = editor_version + 1
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Error updating receipt: {str(exc)}")
                else:
                    st.info("No items found for this receipt.")

                st.divider()
                st.subheader("Delete receipt")
                st.caption("This also removes its line items and updates balances and reports.")
                if st.button("Delete this receipt", key=f"delete_receipt_{selected_rid}"):
                    st.session_state["confirm_delete_receipt_id"] = selected_rid
                    st.rerun()

                if st.session_state.get("confirm_delete_receipt_id") == selected_rid:
                    receipt_row = df_receipts[df_receipts["Receipt_ID"] == selected_rid]
                    receipt_store = (
                        str(receipt_row["Store"].iloc[0])
                        if not receipt_row.empty and "Store" in receipt_row.columns
                        else "this receipt"
                    )
                    st.warning(
                        f"Delete **{receipt_store}** ({selected_rid}) permanently? "
                        "The receipt and all its items will be removed."
                    )
                    confirm_col, cancel_col, _ = st.columns([1.5, 1, 5])
                    with confirm_col:
                        confirm_delete = st.button(
                            "Confirm delete",
                            type="primary",
                            key=f"confirm_delete_{selected_rid}",
                        )
                    with cancel_col:
                        cancel_delete = st.button(
                            "Cancel",
                            key=f"cancel_delete_{selected_rid}",
                        )

                    if confirm_delete:
                        try:
                            with st.spinner("Deleting receipt…"):
                                from app.services.ledger import delete_receipt  # type: ignore

                                _, deleted_items = delete_receipt(selected_rid)
                        except Exception as exc:
                            st.error(f"Error deleting receipt: {exc}")
                        else:
                            st.session_state.pop("confirm_delete_receipt_id", None)
                            st.session_state["clear_edit_receipt_select"] = True
                            st.session_state["receipt_delete_message"] = (
                                f"Deleted receipt {selected_rid} and "
                                f"{deleted_items} associated line item(s)."
                            )
                            st.rerun()
                    elif cancel_delete:
                        st.session_state.pop("confirm_delete_receipt_id", None)
                        st.rerun()
            except Exception as exc:
                st.error(f"Failed to load items: {exc}")
    else:
        st.info("No receipts found. Upload a receipt to get started.")


# =========================================================================== #
# Tab 3 — Balances & Settlements                                               #
# =========================================================================== #

elif selected_tab == "Balances & Settlements":
    st.title("⚖️ Balances & Settlements")
    st.caption("Positive means others owe that roommate; negative means they owe others. Settlements reduce both sides.")
    settlement_message = st.session_state.pop("settlement_message", None)
    if settlement_message:
        st.success(settlement_message)

    df_receipts, df_all_items, df_settlements = _load_ledger_data()

    if not df_receipts.empty and "Receipt_ID" in df_receipts.columns:
        balances_cents = _calculate_balance_cents(
            df_receipts, df_all_items, df_settlements, selected_user
        )
        balances = {
            roommate: balances_cents[_ROOMMATE_INITIALS[roommate]] / 100
            for roommate in _DEFAULT_ROOMMATES
        }
        unallocated_cents = sum(balances_cents.values())
        if unallocated_cents:
            st.warning(
                "Balances do not net to zero because at least one receipt's recorded total "
                "differs from its allocated item total. Review the totals in Past Receipts. "
                f"Unallocated difference: CHF {abs(unallocated_cents) / 100:,.2f}."
            )

        st.subheader("Current balances")
        _balance_cards(balances, highlight=selected_user)

        chart_col, steps_col = st.columns([3, 2], gap="large")
        with chart_col:
            st.caption("Right of the line: others owe them. Left: they owe others.")
            _show_fig(_balance_chart(balances))
        with steps_col:
            st.subheader("Who owes whom")
            owes_rows = _settle_up_steps(balances_cents)
            if owes_rows:
                st.caption("Suggested transfers — tap one to prefill the form below.")

                def _prefill_settlement(step: dict[str, Any]) -> None:
                    st.session_state["settlement_from"] = step["From"]
                    st.session_state["settlement_to"] = step["To"]
                    st.session_state["settlement_amount"] = float(step["Amount (CHF)"])

                for index, step in enumerate(owes_rows):
                    _transfer_card(step)
                    st.button(
                        f"Use {step['From']} → {step['To']}",
                        key=f"prefill_settlement_{index}",
                        on_click=_prefill_settlement,
                        args=(step,),
                    )
            else:
                st.success("Everyone is settled up! 🎉")

        # Settlement form
        st.divider()
        st.subheader("Log a settlement")
        st.caption("From is the roommate paying; To is the roommate receiving the payment.")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            frm_rm = st.selectbox("From (pays)", _DEFAULT_ROOMMATES, key="settlement_from")
        with c2:
            to_rm = st.selectbox("To (receives)", _DEFAULT_ROOMMATES, key="settlement_to")
        with c3:
            amt = st.number_input("Amount (CHF)", min_value=0.0, step=0.01, format="%.2f", key="settlement_amount")
        with c4:
            method = st.selectbox("Method", ["Bank Transfer", "Twint", "Cash"], key="settlement_method")

        if st.button("Log Settlement", type="primary", key="log_settlement_btn"):
            if frm_rm == to_rm:
                st.error("Choose two different roommates for a settlement.")
            else:
                try:
                    with st.spinner("Logging settlement…"):
                        from app.services.ledger import append_settlement  # type: ignore
                        sid = append_settlement(frm_rm, to_rm, amt, method)
                    st.session_state["settlement_message"] = f"Settlement {sid} logged."
                    st.rerun()
                except Exception as exc:
                    st.error(f"Error logging settlement: {str(exc)}")

        st.subheader("Settlement history")
        if df_settlements.empty:
            st.caption("No settlements logged yet.")
        else:
            history = df_settlements.copy()
            if "Amount" in history.columns:
                history["Amount"] = pd.to_numeric(history["Amount"], errors="coerce")
            if "Date" in history.columns:
                history["Date"] = parse_receipt_dates(history["Date"])
                history = history.sort_values("Date", ascending=False, na_position="last")
            history_columns = [
                column
                for column in ["Date", "From_Roommate", "To_Roommate", "Amount", "Method"]
                if column in history.columns
            ]
            st.dataframe(
                history[history_columns],
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="DD.MM.YYYY"),
                    "From_Roommate": st.column_config.TextColumn("From"),
                    "To_Roommate": st.column_config.TextColumn("To"),
                    "Amount": st.column_config.NumberColumn("Amount", format="CHF %.2f"),
                },
            )

    else:
        st.info("No receipts found. Upload a receipt to start tracking balances.")


# =========================================================================== #
# Tab 4 — Total Spendings                                                     #
# =========================================================================== #

elif selected_tab == "Total Spendings":
    st.title("📊 Total Spendings")
    st.caption(
        "Compare what each roommate paid with their share of spending for a month or all time."
    )

    try:
        with st.spinner("Loading data…"):
            from app.services.ledger import get_all_receipts, get_receipt_items  # type: ignore

            df_receipts = get_all_receipts()
            df_all_items = get_receipt_items()
    except Exception as exc:
        st.error(f"Failed to load spending data: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store"])
        df_all_items = pd.DataFrame(columns=["Receipt_ID", "Line_Total", "Beneficiary"])

    if not df_receipts.empty and "Date" in df_receipts.columns:
        receipt_dates = parse_receipt_dates(df_receipts["Date"])
        available_years = sorted(
            {int(year) for year in receipt_dates.dt.year.dropna().unique()},
            reverse=True,
        )
        month_options = ["All-time"] + [
            calendar.month_name[month] for month in range(1, 13)
        ]
        month_col, year_col = st.columns(2)
        with month_col:
            selected_month = st.selectbox(
                "Month",
                month_options,
                key="total_spendings_month",
            )

        df_period = df_receipts.copy()
        period_slug = "all-time"
        if selected_month != "All-time":
            years = sorted(
                set(available_years) | {date.today().year},
                reverse=True,
            )
            default_year = available_years[0] if available_years else date.today().year
            with year_col:
                selected_year = st.selectbox(
                    "Year",
                    years,
                    index=years.index(default_year),
                    key="total_spendings_year",
                )
            month_number = list(calendar.month_name).index(selected_month)
            df_period = filter_receipts_for_period(
                df_receipts,
                month=month_number,
                year=selected_year,
            )
            period_slug = f"{selected_month.lower()}-{selected_year}"
            st.caption(f"Showing {selected_month} {selected_year} only.")
        else:
            st.caption("Showing receipts from every year.")

        paid_cents_by_person = {code: 0 for code in "ABC"}
        receipts_paid_by_person = {code: 0 for code in "ABC"}
        exact_allocated_cents = {code: Decimal(0) for code in "ABC"}
        allocated_total_cents = 0
        detail_rows = []

        for _, receipt in df_period.iterrows():
            receipt_id = str(receipt.get("Receipt_ID", "")).strip()
            receipt_items = _receipt_items_for_view(df_all_items, receipt_id)

            exact_shares, receipt_allocated_cents = _receipt_spending_exact(
                receipt_items,
                _money_to_cents(receipt.get("Header_Discounts", 0)),
            )
            allocated_total_cents += receipt_allocated_cents
            for code, share in exact_shares.items():
                exact_allocated_cents[code] += share

            receipt_total_value = receipt.get("Grand_Total", "")
            receipt_total_cents = (
                _money_to_cents(receipt_total_value)
                if receipt_total_value is not None and str(receipt_total_value).strip()
                else receipt_allocated_cents
            )
            payer_code = _roommate_code(receipt.get("Paid_By"))
            if payer_code:
                paid_cents_by_person[payer_code] += receipt_total_cents
                receipts_paid_by_person[payer_code] += 1

            detail_rows.append(
                {
                    "Receipt ID": receipt_id,
                    "Date": receipt.get("Date", ""),
                    "Store": receipt.get("Store", ""),
                    "Paid By": receipt.get("Paid_By", ""),
                    "Receipt Total (CHF)": receipt_total_cents / 100,
                }
            )

        allocated_cents_by_person = round_shares_to_cents(
            exact_allocated_cents,
            allocated_total_cents,
        )
        summary_rows = []
        for roommate, code in _ROOMMATE_INITIALS.items():
            summary_rows.append(
                {
                    "Roommate": roommate,
                    "Paid for receipts (CHF)": paid_cents_by_person[code] / 100,
                    "Allocated share (CHF)": allocated_cents_by_person[code] / 100,
                    "Receipts paid": receipts_paid_by_person[code],
                }
            )
        summary_rows.append(
            {
                "Roommate": "Collective total",
                "Paid for receipts (CHF)": sum(paid_cents_by_person.values()) / 100,
                "Allocated share (CHF)": sum(allocated_cents_by_person.values()) / 100,
                "Receipts paid": len(df_period),
            }
        )

        st.subheader("Spending by roommate")
        st.caption(
            "Paid for receipts is what they paid at checkout; allocated share is their portion of the items."
        )
        st.dataframe(
            pd.DataFrame(summary_rows),
            hide_index=True,
            width="stretch",
            column_config={
                column: st.column_config.NumberColumn(column, format="%.2f")
                for column in [
                    "Paid for receipts (CHF)",
                    "Allocated share (CHF)",
                ]
            },
        )

        period_ids = set(df_period["Receipt_ID"].astype(str).str.strip())
        paid_col, cat_col = st.columns(2, gap="large")
        with paid_col:
            st.markdown("**Paid at checkout vs. share of items**")
            fig_paid = go.Figure()
            fig_paid.add_bar(
                x=_DEFAULT_ROOMMATES,
                y=[paid_cents_by_person[_ROOMMATE_INITIALS[n]] / 100 for n in _DEFAULT_ROOMMATES],
                name="Paid at checkout",
                marker=dict(color=_SINGLE_SERIES_COLOR, cornerradius=4),
                hovertemplate="%{x} paid CHF %{y:,.2f}<extra></extra>",
            )
            fig_paid.add_bar(
                x=_DEFAULT_ROOMMATES,
                y=[allocated_cents_by_person[_ROOMMATE_INITIALS[n]] / 100 for n in _DEFAULT_ROOMMATES],
                name="Share of items",
                marker=dict(color=_NEUTRAL_SERIES_COLOR, cornerradius=4),
                hovertemplate="%{x}'s share CHF %{y:,.2f}<extra></extra>",
            )
            fig_paid.update_layout(barmode="group", bargroupgap=0.08)
            fig_paid.update_yaxes(title_text="CHF", rangemode="tozero")
            _show_fig(_style_fig(fig_paid))
        with cat_col:
            st.markdown("**Spending by category**")
            period_categories = _category_totals(df_all_items, period_ids)
            if period_categories.empty:
                st.info("No categorised items in this period.")
            else:
                _show_fig(_hbar_chart(period_categories, height=320))

        if not df_period.empty and "Store" in df_period.columns:
            store_totals = (
                pd.DataFrame(detail_rows)
                .assign(Store=lambda d: d["Store"].astype(str).str.strip().replace("", "Unknown store"))
                .groupby("Store")["Receipt Total (CHF)"]
                .sum()
                .sort_values()
                .tail(8)
            )
            store_totals = store_totals[store_totals > 0]
            if not store_totals.empty:
                st.markdown("**Top stores**")
                _show_fig(_hbar_chart(store_totals))

        st.subheader("Receipts in selected period")
        details_df = pd.DataFrame(detail_rows)
        if not details_df.empty:
            st.dataframe(
                details_df,
                hide_index=True,
                width="stretch",
                column_config={
                    column: st.column_config.NumberColumn(column, format="%.2f")
                    for column in ["Receipt Total (CHF)"]
                },
            )
            st.download_button(
                label="Download CSV",
                data=details_df.to_csv(index=False).encode("utf-8"),
                file_name=f"wg-finance-total-spendings-{period_slug}.csv",
                mime="text/csv",
            )
        else:
            st.info("No receipts found for this period.")
    else:
        st.info("No receipts found. Upload a receipt to see total spendings.")
