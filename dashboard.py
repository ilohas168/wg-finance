"""Streamlit dashboard for WG Sharehouse — upload, edit, balances, and reports.

Pages (top tab bar; maths lives in ``app/services/ledger_math.py``):
  0. **Overview** — this month at a glance, balances, settle-up, trends.
  1. **Upload Receipt** — upload a receipt image, parse with Groq
     vision, edit line-items in-place, then save to Google Sheets (new schema).
  2. **Past Receipts** — browse historical receipts, see roommate shares, edit or delete.
  3. **Balances & Settlements** — per-roommate balances, who-owes-whom matrix,
     settlement tracking.
  4. **Total Spendings** — monthly and all-time spending by roommate.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any, Dict, Optional
from uuid import uuid4

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from app.services.accounting import (
    recover_legacy_weighted_quantities,
    resolve_line_item_amounts,
    round_shares_to_cents,
)
from app.services.ledger_math import (
    BENEFICIARY_LABELS,
    CODE_TO_ROOMMATE,
    LABEL_TO_BENEFICIARY,
    ROOMMATE_CODES,
    ROOMMATES,
    ReceiptBreakdown,
    beneficiary_code,
    category_totals_cents,
    compute_balances,
    money_to_cents,
    receipt_breakdowns,
    receipt_spending_cents,
    roommate_code,
    settle_up,
    summarise_period,
)
from app.services.reporting import parse_receipt_dates

# --------------------------------------------------------------------------- #
# Page config                                                                  #
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="WG Sharehouse Hub", layout="wide", page_icon="🏠")

# Theme-neutral styling: translucent borders and washes read correctly on both
# Streamlit's light and dark themes, so nothing here hard-codes a background.
st.markdown(
    """
    <style>
    .block-container { padding-top: 3.2rem; padding-bottom: 3rem; max-width: 1280px; }
    section[data-testid="stSidebar"], div[data-testid="stSidebarCollapsedControl"] { display: none; }
    .wg-brand { font-size: 1.35rem; font-weight: 750; letter-spacing: -0.01em; }
    .wg-tabbar-rule { border-bottom: 1px solid rgba(128, 128, 128, 0.22); margin: 0.1rem 0 1.2rem; }
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
    .wg-period { text-align: center; font-size: 1.35rem; font-weight: 750; }
    .wg-split { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-top: 0.7rem; }
    .wg-split .wg-value { font-size: 1.25rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session-state helpers                                                        #
# --------------------------------------------------------------------------- #

def _convert_items_to_sheets(items_df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame with *display* labels to sheets-compatible records."""
    records = []
    for _, row in items_df.iterrows():
        r = dict(row)
        ben = str(r.get("Beneficiary", "ALL"))
        if ben in LABEL_TO_BENEFICIARY:
            r["Beneficiary"] = LABEL_TO_BENEFICIARY[ben]
        records.append(r)
    return records


def _init_session() -> None:
    """Ensure all required keys exist in ``st.session_state``."""
    defaults = {
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
# Header: brand, account menu, and the top tab bar                             #
# --------------------------------------------------------------------------- #

logged_in = st.session_state.get("logged_in_user")
# The logged-in roommate, used for "you" and as the payer of uploads.
current_user: Optional[str] = (
    CODE_TO_ROOMMATE.get(roommate_code(st.session_state.get("logged_in_name")) or "")
    if logged_in
    else None
)

brand_col, account_col = st.columns([5, 1.3], vertical_alignment="center")
with brand_col:
    st.markdown(
        '<div class="wg-brand">🏠 WG Sharehouse Hub</div>'
        '<div class="wg-muted">Shared receipts, fair splits, and who owes whom.</div>',
        unsafe_allow_html=True,
    )
with account_col:
    if not logged_in:
        with st.popover("🔐 Log in", width="stretch"):
            with st.form("login_form", border=False):
                _uname = st.text_input("Username", key="login_username")
                _pw = st.text_input("Password", type="password", key="login_password")
                if st.form_submit_button("Log in", type="primary", width="stretch"):
                    from app.services.auth import (  # noqa: E402
                        verify_user,
                        RESULT_OK,
                        RESULT_DB_ERROR,
                    )

                    result_code, payload = verify_user(_uname, _pw)
                    if result_code == RESULT_OK and payload:
                        st.session_state.logged_in_user = _uname.strip().lower()
                        st.session_state.logged_in_name = payload
                        st.rerun()
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
            st.caption("Forgot your password? Ask Shin to reset it in the Google Sheet.")
    else:
        with st.popover(f"👤 {st.session_state.logged_in_name}", width="stretch"):
            if st.button("Log out", width="stretch"):
                del st.session_state.logged_in_user
                del st.session_state.logged_in_name
                st.rerun()
            with st.form("change_pw_form", border=False):
                st.markdown("**Change password**")
                _old = st.text_input("Current password", type="password", key="old_pw")
                _new = st.text_input("New password", type="password", key="new_pw")
                _confirm = st.text_input("Confirm new password", type="password", key="confirm_pw")
                if st.form_submit_button("Update", width="stretch"):
                    if _new != _confirm:
                        st.error("Passwords don't match.")
                    else:
                        from app.services.auth import change_password  # noqa: E402

                        ok, msg = change_password(logged_in, _old, _new)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

# Map tab names from older sessions onto the current ones.
_legacy_tabs = {
    "Edit History": "Past Receipts",
    "Parent Reports": "Total Spendings",
    "View History": "Overview",
}
if st.session_state.get("current_tab") in _legacy_tabs:
    st.session_state["current_tab"] = _legacy_tabs[st.session_state["current_tab"]]
# Uploading needs an account (it records the payer); everything else is open.
if logged_in:
    tabs = ["Overview", "Upload Receipt", "Past Receipts", "Balances & Settlements", "Total Spendings"]
else:
    tabs = ["Overview", "Past Receipts", "Balances & Settlements", "Total Spendings"]
if st.session_state.get("current_tab") not in tabs:
    st.session_state["current_tab"] = tabs[0]

_tab_labels = {
    "Overview": "✨ Overview",
    "Upload Receipt": "📸 Upload",
    "Past Receipts": "🧾 Receipts",
    "Balances & Settlements": "⚖️ Balances",
    "Total Spendings": "📊 Spendings",
}
selected_tab = st.segmented_control(
    "Page",
    options=tabs,
    format_func=lambda tab: _tab_labels.get(tab, tab),
    required=True,
    key="current_tab",
    label_visibility="collapsed",
    width="stretch",
)
st.markdown('<div class="wg-tabbar-rule"></div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #










def _split_type_for_beneficiary(value: Any) -> str:
    """Derive the legacy Shared/Private sheet field from the sole allocation."""
    code = beneficiary_code(value)
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
        df["Beneficiary"] = df["Beneficiary"].apply(beneficiary_code)
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
            lambda value: BENEFICIARY_LABELS[beneficiary_code(value)]
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
    beneficiary_options = list(BENEFICIARY_LABELS.values())

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
        height="content",
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
        beneficiary = beneficiary_code(item.get("beneficiary", "ALL"))
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

    beneficiary_codes = df["Beneficiary"].apply(beneficiary_code)
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
# Single-series charts (categories, stores) use a hue no roommate owns.
_SINGLE_SERIES_COLOR = "#6250d6"
_NEUTRAL_SERIES_COLOR = "#a3a29c"
# Charts are read-only: no zoom, pan, or scroll capture, so swiping or
# scrolling over a chart always scrolls the page (tooltips still work).
_PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": False, "doubleClick": False}


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
    for name in ROOMMATES:
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
        dragmode=False,
    )
    fig.update_xaxes(showgrid=False, zeroline=False, fixedrange=True)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.18)", zeroline=False, fixedrange=True)
    return fig


def _show_fig(fig: go.Figure) -> None:
    st.plotly_chart(fig, config=_PLOTLY_CONFIG)


def _balance_chart(balances: dict[str, float]) -> go.Figure:
    """Diverging horizontal bars: right of zero is owed, left of zero owes."""
    names = list(reversed(ROOMMATES))
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
    # Leave room on both sides for the outside value labels, even on phones.
    limit = max([abs(v) for v in values] + [1.0]) * 1.9
    fig.add_vline(x=0, line_width=1, line_color="rgba(128,128,128,0.6)")
    fig.update_xaxes(range=[-limit, limit], showticklabels=False)
    _style_fig(fig, height=220)
    fig.update_yaxes(showgrid=False)
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8))
    return fig


def _ledger_frame(breakdowns: list[ReceiptBreakdown]) -> pd.DataFrame:
    """One row per receipt for charts and tables, newest first."""
    rows = []
    for b in breakdowns:
        # Rounded per receipt so each row's shares add up to its items total.
        shares = round_shares_to_cents(b.exact_shares, b.allocated_cents)
        rows.append(
            {
                "Receipt_ID": b.receipt_id,
                "Date": b.date,
                # Payments entered without a receipt are marked so they stand out.
                "Store": f"✍️ {b.store}" if b.receipt_id.startswith("MAN-") else b.store,
                "Paid_By": b.payer or (f"⚠ {b.raw_payer}" if b.raw_payer else "⚠ unknown"),
                "Total": b.paid_cents / 100,
                "Items": b.allocated_cents / 100,
                **{name: shares[code] / 100 for name, code in ROOMMATE_CODES.items()},
            }
        )
    frame = pd.DataFrame(
        rows,
        columns=["Receipt_ID", "Date", "Store", "Paid_By", "Total", "Items", *ROOMMATES],
    )
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    return frame.sort_values(
        ["Date", "Receipt_ID"], ascending=False, na_position="last", kind="stable"
    ).reset_index(drop=True)


def _receipts_table(frame: pd.DataFrame, with_shares: bool = False, height: Any = "content") -> None:
    """Show receipts newest first with the date, store, payer, and total."""
    columns = ["Date", "Store", "Paid_By", "Total"]
    config = {
        "Date": st.column_config.DateColumn("Date", format="DD.MM.YYYY"),
        "Store": st.column_config.TextColumn("Store"),
        "Paid_By": st.column_config.TextColumn("Paid by"),
        "Total": st.column_config.NumberColumn("Receipt total", format="CHF %.2f"),
    }
    if with_shares:
        columns += ROOMMATES
        config.update(
            {
                name: st.column_config.NumberColumn(f"{name}'s share", format="CHF %.2f")
                for name in ROOMMATES
            }
        )
    st.dataframe(frame[columns], hide_index=True, column_config=config, height=height)


def _category_series(breakdowns: list[ReceiptBreakdown], df_all_items: pd.DataFrame) -> pd.Series:
    """Positive category totals in CHF, smallest first (for horizontal bars)."""
    totals = pd.Series(category_totals_cents(breakdowns, df_all_items), dtype=float) / 100
    return totals[totals > 0].sort_values()


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
        dated.groupby(period)[ROOMMATES].sum().reindex(month_index, fill_value=0)
    )
    labels = [p.strftime("%b %Y") for p in month_index]
    fig = go.Figure()
    for name in ROOMMATES:
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
    greeting = f"Hi {current_user} 👋" if current_user else "Hi there 👋"

    breakdowns = receipt_breakdowns(df_receipts, df_all_items)
    if not breakdowns:
        _hero(today.strftime("%A, %d %B %Y"), greeting, "No receipts yet — upload one to get the ledger going.")
        st.stop()

    ledger = _ledger_frame(breakdowns)
    balances_cents = compute_balances(breakdowns, df_settlements)
    balances = {name: balances_cents[code] / 100 for name, code in ROOMMATE_CODES.items()}
    steps = settle_up(balances_cents)
    if current_user:
        your_balance = balances[current_user]
        if your_balance > 0.004:
            mood = f"Your roommates owe you {_chf(your_balance)}."
        elif your_balance < -0.004:
            mood = f"You owe {_chf(abs(your_balance))} — see Settle up below."
        else:
            mood = "You're all square. Nice."
    elif steps:
        mood = " · ".join(f"{s['From']} owes {s['To']} {_chf(s['Amount (CHF)'])}" for s in steps)
    else:
        mood = "Everyone is square."
    _hero(today.strftime("%A, %d %B %Y"), greeting, mood)

    month_period = pd.Timestamp(today).to_period("M")
    this_month = [b for b in breakdowns if pd.notna(b.date) and b.date.to_period("M") == month_period]
    last_month = [b for b in breakdowns if pd.notna(b.date) and b.date.to_period("M") == month_period - 1]
    this_summary = summarise_period(this_month)
    last_summary = summarise_period(last_month)
    all_summary = summarise_period(breakdowns)
    this_total = this_summary.total_paid / 100
    last_total = last_summary.total_paid / 100
    last_name = (month_period - 1).strftime("%B")
    if last_total > 0:
        change = (this_total - last_total) / last_total * 100
        trend_note = f"{'▲' if change >= 0 else '▼'} {abs(change):.0f}% vs {last_name} ({_chf(last_total)})"
    else:
        trend_note = f"Nothing recorded in {last_name}"
    first_date = ledger["Date"].min()
    cards = [(f"Spent in {today:%B}", _chf(this_total), trend_note)]
    if current_user:
        code = ROOMMATE_CODES[current_user]
        your_share = this_summary.share[code] / 100
        share_pct = (
            f"{this_summary.share[code] / this_summary.total_share * 100:.0f}% of the items bought"
            if this_summary.total_share
            else "No spending yet this month"
        )
        cards.append(("Your share this month", _chf(your_share), share_pct))
        cards.append(
            ("Receipts this month", f"{this_summary.receipt_count}", f"{this_summary.receipts_paid[code]} paid by you")
        )
    else:
        cards.append(
            (
                "Per person this month",
                _chf(this_summary.total_share / 300),
                "Average share of the items bought",
            )
        )
        cards.append(("Receipts this month", f"{this_summary.receipt_count}", "Logged so far"))
    cards.append(
        (
            "All-time spending",
            _chf(all_summary.total_paid / 100),
            f"{all_summary.receipt_count} receipts since {first_date:%b %Y}"
            if pd.notna(first_date)
            else f"{all_summary.receipt_count} receipts",
        )
    )
    _stat_cards(cards)

    if all_summary.unknown_payer_ids:
        st.warning(
            f"{len(all_summary.unknown_payer_ids)} receipt(s) have no recognised payer and are "
            "left out of balances: " + ", ".join(all_summary.unknown_payer_ids[:5])
        )

    bal_col, settle_col = st.columns([3, 2], gap="large")
    with bal_col:
        st.subheader("Balances")
        st.caption("Right of the line: others owe them. Left: they owe others.")
        _show_fig(_balance_chart(balances))
    with settle_col:
        st.subheader("Settle up")
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
        category_scope = this_month or breakdowns
        st.subheader("Where the money goes")
        st.caption(
            f"Item categories in {today:%B}, after receipt discounts." if this_month
            else "Item categories, all time (nothing recorded this month yet)."
        )
        category_totals = _category_series(category_scope, df_all_items)
        if category_totals.empty:
            st.info("No categorised items yet.")
        else:
            _show_fig(_hbar_chart(category_totals))
    with recent_col:
        st.subheader("Recent receipts")
        st.caption("Newest first, with who paid at the till.")
        _receipts_table(ledger.head(8))


# =========================================================================== #
# Tab 1 — Upload Receipt                                                     #
# =========================================================================== #

elif selected_tab == "Upload Receipt":
    st.title("📸 Upload")
    upload_mode = st.segmented_control(
        "What are you adding?",
        ["📷 Scan a receipt", "✍️ Payment without receipt"],
        required=True,
        default="📷 Scan a receipt",
        key="upload_mode",
    )

    if upload_mode == "✍️ Payment without receipt":
        st.caption(
            "For things bought without a receipt (e.g. cutlery, a deposit, a Twint payment). "
            "It counts exactly like a receipt: the payer is credited and the amount is split."
        )
        manual_message = st.session_state.pop("manual_entry_message", None)
        if manual_message:
            st.success(manual_message)

        def _save_manual_entry() -> None:
            description = st.session_state.get("manual_description", "").strip()
            amount = float(st.session_state.get("manual_amount") or 0)
            if not description or amount <= 0:
                st.session_state["manual_entry_error"] = "Add a description and an amount above 0."
                return
            from app.services.ledger import save_receipt  # type: ignore
            from app.services.ledger_math import manual_entry_records

            receipt_data, items = manual_entry_records(
                description,
                amount,
                st.session_state["manual_date"].isoformat(),
                st.session_state["manual_payer"],
                st.session_state["manual_beneficiary"],
                st.session_state["manual_category"],
                st.session_state.get("manual_note", ""),
                entry_id=f"MAN-{datetime.now():%Y%m%d-%H%M%S}",
            )
            try:
                save_receipt(receipt_data, items)
            except Exception as exc:  # surfaced on the next run
                st.session_state["manual_entry_error"] = f"Error saving to Sheets: {exc}"
                return
            st.session_state["manual_entry_message"] = (
                f"Saved {_chf(receipt_data['Grand_Total'])} for “{description}”, "
                f"paid by {receipt_data['Paid_By']}."
            )
            for key in ("manual_description", "manual_amount", "manual_note"):
                st.session_state.pop(key, None)

        desc_col, amount_col = st.columns([2, 1])
        with desc_col:
            st.text_input(
                "What was it for?",
                placeholder="e.g. Cutlery — plates, forks, knives",
                key="manual_description",
            )
        with amount_col:
            manual_amount = st.number_input(
                "Amount (CHF)", min_value=0.0, step=0.05, format="%.2f", key="manual_amount"
            )
        date_col, payer_col, split_col, cat_col = st.columns(4)
        with date_col:
            st.date_input("Date", value=date.today(), key="manual_date", format="DD.MM.YYYY")
        with payer_col:
            st.selectbox(
                "Paid by",
                ROOMMATES,
                index=ROOMMATES.index(current_user) if current_user in ROOMMATES else 0,
                key="manual_payer",
            )
        with split_col:
            manual_beneficiary = st.selectbox(
                "Shared between", list(BENEFICIARY_LABELS.values()), key="manual_beneficiary"
            )
        with cat_col:
            st.selectbox(
                "Category",
                ["Household", "Food", "Drink", "Toiletries", "General"],
                key="manual_category",
            )
        st.text_input("Note (optional)", key="manual_note")

        if manual_amount > 0:
            people = [CODE_TO_ROOMMATE[c] for c in "ABC" if c in beneficiary_code(manual_beneficiary)] \
                if beneficiary_code(manual_beneficiary) != "ALL" else ROOMMATES
            preview = receipt_spending_cents(
                pd.DataFrame([{"Line_Total": manual_amount, "Beneficiary": manual_beneficiary}])
            )
            st.info(
                "Each person's share: "
                + " · ".join(f"{name} {_chf(preview[ROOMMATE_CODES[name]] / 100)}" for name in people)
            )
        st.button("Save payment", type="primary", key="manual_save", on_click=_save_manual_entry)
        manual_error = st.session_state.pop("manual_entry_error", None)
        if manual_error:
            st.error(manual_error)
        st.stop()

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
        _ben_options = list(BENEFICIARY_LABELS.values())  # e.g. ["All (Shin…)", "Shin", ...]

        if "Category" in df.columns:
            valid_categories = {"Food", "Drink", "Toiletries", "Household", "General"}
            df["Category"] = df["Category"].fillna("").astype(str).apply(
                lambda value: value if value in valid_categories else "General"
            )
        # Convert internal codes → display labels for the editor.
        if "Beneficiary" in df.columns:
            df["Beneficiary"] = df["Beneficiary"].apply(
                lambda value: BENEFICIARY_LABELS[beneficiary_code(value)]
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
        # Each roommate's share uses the same allocation as the balances:
        # line totals split among each item's beneficiaries, minus their part
        # of any header discount.
        upload_shares = receipt_spending_cents(
            edited_df, money_to_cents(header_disc)
        )
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Shared items", _chf(summary["shared_total"]))
        for col, name in zip([m2, m3, m4], ROOMMATES):
            with col:
                st.metric(f"{name}'s share", _chf(upload_shares[ROOMMATE_CODES[name]] / 100))
        total_col, payer_col = st.columns(2)
        with total_col:
            receipt_total = st.number_input(
                "Grand total on receipt (CHF)",
                min_value=0.0,
                step=0.01,
                format="%.2f",
                key="receipt_total_input",
            )
        with payer_col:
            upload_payer = st.selectbox(
                "Paid by",
                ROOMMATES,
                index=ROOMMATES.index(current_user) if current_user in ROOMMATES else 0,
                key="upload_paid_by",
                help="The roommate who paid at the till is credited with the receipt total.",
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

                    _payer = upload_payer

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

    breakdowns = receipt_breakdowns(df_receipts, df_all_items)
    if breakdowns:
        ledger = _ledger_frame(breakdowns)
        ledger["Difference"] = ledger["Items"] - ledger["Total"]
        ledger["Check"] = ledger["Difference"].abs().map(lambda d: "⚠" if d > 0.01 else "✓")
        st.subheader("All receipts")
        st.caption(
            "Newest first. Shares are each roommate's part of the items; ⚠ marks receipts "
            "whose items don't add up to the printed total."
        )
        shown = ledger
        if len(ledger) > 25 and not st.toggle(f"Show all {len(ledger)} receipts", key="receipts_show_all"):
            shown = ledger.head(25)
        st.dataframe(
            shown[["Check", "Date", "Store", "Paid_By", "Total", "Items", "Difference", *ROOMMATES]],
            hide_index=True,
            height="content",
            column_config={
                "Check": st.column_config.TextColumn("", width="small"),
                "Date": st.column_config.DateColumn("Date", format="DD.MM.YYYY"),
                "Paid_By": st.column_config.TextColumn("Paid by"),
                "Total": st.column_config.NumberColumn("Receipt total", format="CHF %.2f"),
                "Items": st.column_config.NumberColumn("Items total", format="CHF %.2f"),
                "Difference": st.column_config.NumberColumn("Difference", format="CHF %.2f"),
                **{
                    name: st.column_config.NumberColumn(f"{name}'s share", format="CHF %.2f")
                    for name in ROOMMATES
                },
            },
        )
        if (ledger["Check"] == "⚠").any():
            st.warning(
                f"{int((ledger['Check'] == '⚠').sum())} receipt(s) have items that differ from the "
                "printed receipt total. Open them below and correct the line items."
            )

        receipt_labels = {
            row.Receipt_ID: (
                f"{row.Date:%d.%m.%Y}" if pd.notna(row.Date) else "Date unknown"
            )
            + f" · {row.Store} · {_chf(row.Total)} · paid by {row.Paid_By}"
            for row in ledger.itertuples()
        }
        receipt_ids = ledger["Receipt_ID"].tolist()
        st.subheader("Receipt details" if not logged_in else "Edit a receipt")
        selected_rid = st.selectbox(
            "Select Receipt",
            options=receipt_ids,
            format_func=lambda receipt_id: receipt_labels.get(receipt_id, receipt_id),
            key="edit_receipt_select",
        )

        if selected_rid and not logged_in:
            guest_items = df_all_items[
                df_all_items["Receipt_ID"].astype(str).str.strip() == selected_rid
            ] if "Receipt_ID" in df_all_items.columns else df_all_items.iloc[0:0]
            if guest_items.empty:
                st.info("No items found for this receipt.")
            else:
                guest_view = _prepare_receipt_items_for_editing(guest_items)
                st.dataframe(
                    guest_view[[c for c in ["Product_Name", "Category", "Qty", "Line_Total", "Beneficiary"] if c in guest_view.columns]],
                    hide_index=True,
                    height="content",
                    column_config={
                        "Product_Name": st.column_config.TextColumn("Item"),
                        "Qty": st.column_config.NumberColumn("Qty", format="%.3g"),
                        "Line_Total": st.column_config.NumberColumn("Line total", format="CHF %.2f"),
                        "Beneficiary": st.column_config.TextColumn("For"),
                    },
                )
            st.caption("Log in to edit or delete receipts.")
            selected_rid = None

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
                selected_shares = receipt_spending_cents(
                    df_items,
                    money_to_cents(selected_receipt.get("Header_Discounts", 0)),
                )
                share_cols = st.columns(5)
                with share_cols[0]:
                    # The printed total is what the payer actually paid, so it is
                    # stored as entered and never derived from the line items.
                    printed_total = st.number_input(
                        "Printed receipt total (CHF)",
                        value=money_to_cents(selected_receipt.get("Grand_Total", 0)) / 100,
                        step=0.01,
                        format="%.2f",
                        key=f"receipt_total_{selected_rid}",
                        help="What the payer paid at the till. Saved with Update Receipt.",
                    )
                with share_cols[1]:
                    st.metric("Items total", _chf(sum(selected_shares.values()) / 100))
                for col, name, code in zip(share_cols[2:], ROOMMATES, "ABC"):
                    with col:
                        st.metric(f"{name}'s share", _chf(selected_shares[code] / 100))
                selected_difference = sum(selected_shares.values()) - money_to_cents(printed_total)
                if abs(selected_difference) > 1:
                    st.warning(
                        f"The items add up to {_chf(sum(selected_shares.values()) / 100)}, "
                        f"{_chf(abs(selected_difference) / 100)} "
                        f"{'more' if selected_difference > 0 else 'less'} than the printed total. "
                        "The payer is credited the printed total, so balances won't net to zero "
                        "until the line items (or a misread total) are corrected."
                    )
                else:
                    st.success("Items match the printed total. ✓")

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
                stored_payer = CODE_TO_ROOMMATE.get(
                    roommate_code(selected_receipt.get("Paid_By")) or ""
                )
                edited_payer = st.selectbox(
                    "Paid by",
                    ROOMMATES,
                    index=ROOMMATES.index(stored_payer) if stored_payer else 0,
                    key=f"receipt_payer_{selected_rid}",
                    help="Saved with Update Receipt below.",
                )
                if not stored_payer:
                    st.warning(
                        f"The sheet's payer value {selected_receipt.get('Paid_By', '')!r} isn't a "
                        "known roommate, so this receipt is left out of balances until you fix it."
                    )
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
                                    "Date": selected_receipt.get("Date", ""),
                                    "Store": selected_receipt.get("Store", ""),
                                    "Paid_By": edited_payer,
                                    "Header_Discounts": money_to_cents(selected_receipt.get("Header_Discounts", 0)) / 100,
                                    "Grand_Total": money_to_cents(printed_total) / 100,
                                    "Shared_Total": _summarise_receipt(
                                        edited_df,
                                        money_to_cents(selected_receipt.get("Header_Discounts", 0)) / 100,
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
        breakdowns = receipt_breakdowns(df_receipts, df_all_items)
        balances_cents = compute_balances(breakdowns, df_settlements)
        balances = {
            roommate: balances_cents[ROOMMATE_CODES[roommate]] / 100
            for roommate in ROOMMATES
        }
        known_summary = summarise_period([b for b in breakdowns if b.payer])
        unallocated_cents = sum(balances_cents.values())
        if unallocated_cents:
            st.warning(
                "Balances do not net to zero because at least one receipt's recorded total "
                "differs from its allocated item total. Review the ⚠ receipts in Receipts. "
                f"Unallocated difference: CHF {abs(unallocated_cents) / 100:,.2f}."
            )
        unknown_ids = summarise_period(breakdowns).unknown_payer_ids
        if unknown_ids:
            st.warning(
                f"{len(unknown_ids)} receipt(s) have no recognised payer and are left out: "
                + ", ".join(unknown_ids[:5])
            )

        st.subheader("Current balances")
        _balance_cards(balances, highlight=current_user)

        with st.expander("How is this calculated?"):
            sent = {code: 0 for code in "ABC"}
            received = {code: 0 for code in "ABC"}
            for _, settlement in df_settlements.iterrows():
                sender = roommate_code(settlement.get("From_Roommate"))
                receiver = roommate_code(settlement.get("To_Roommate"))
                amount = money_to_cents(settlement.get("Amount", 0))
                if sender and receiver and sender != receiver and amount > 0:
                    sent[sender] += amount
                    received[receiver] += amount
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Roommate": name,
                            "Paid at the till": known_summary.paid[code] / 100,
                            "− Share of items": known_summary.share[code] / 100,
                            "+ Settlements paid": sent[code] / 100,
                            "− Settlements received": received[code] / 100,
                            "= Balance": balances_cents[code] / 100,
                        }
                        for name, code in ROOMMATE_CODES.items()
                    ]
                ),
                hide_index=True,
                height="content",
                column_config={
                    column: st.column_config.NumberColumn(column, format="CHF %.2f")
                    for column in [
                        "Paid at the till",
                        "− Share of items",
                        "+ Settlements paid",
                        "− Settlements received",
                        "= Balance",
                    ]
                },
            )
            st.caption(
                "Each item is split evenly between the roommates it's for; receipt-wide "
                "discounts are shared in proportion to what each person bought."
            )

        chart_col, steps_col = st.columns([3, 2], gap="large")
        with chart_col:
            st.caption("Right of the line: others owe them. Left: they owe others.")
            _show_fig(_balance_chart(balances))
        with steps_col:
            st.subheader("Who owes whom")
            owes_rows = settle_up(balances_cents)
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
            frm_rm = st.selectbox("From (pays)", ROOMMATES, key="settlement_from")
        with c2:
            to_rm = st.selectbox("To (receives)", ROOMMATES, key="settlement_to")
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
                height="content",
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

    df_receipts, df_all_items, _ = _load_ledger_data()
    all_breakdowns = receipt_breakdowns(df_receipts, df_all_items)

    if all_breakdowns:
        this_month = pd.Timestamp(date.today()).to_period("M")
        if "spend_period" not in st.session_state:
            st.session_state["spend_period"] = str(this_month)

        def _shift_month(step: int) -> None:
            current = st.session_state.get("spend_period", str(this_month))
            base = this_month if current == "all" else pd.Period(current, freq="M")
            st.session_state["spend_period"] = str(min(base + step, this_month))

        def _toggle_all_time() -> None:
            st.session_state["spend_period"] = (
                str(this_month) if st.session_state["spend_period"] == "all" else "all"
            )

        period_value = st.session_state["spend_period"]
        all_time = period_value == "all"
        period = None if all_time else pd.Period(period_value, freq="M")
        prev_col, label_col, next_col, all_col = st.columns(
            [1, 3, 1, 1.4], vertical_alignment="center"
        )
        with prev_col:
            st.button("◀ Prev", key="spend_prev", on_click=_shift_month, args=(-1,), width="stretch")
        with label_col:
            st.markdown(
                f'<div class="wg-period">{"All time" if all_time else period.strftime("%B %Y")}</div>',
                unsafe_allow_html=True,
            )
        with next_col:
            st.button(
                "Next ▶",
                key="spend_next",
                on_click=_shift_month,
                args=(1,),
                disabled=not all_time and period >= this_month,
                width="stretch",
            )
        with all_col:
            st.button(
                "This month" if all_time else "All time",
                key="spend_all_time",
                on_click=_toggle_all_time,
                width="stretch",
            )

        if all_time:
            breakdowns = all_breakdowns
            period_slug = "all-time"
            period_label = "all time"
        else:
            breakdowns = [
                b for b in all_breakdowns if pd.notna(b.date) and b.date.to_period("M") == period
            ]
            period_slug = period.strftime("%Y-%m")
            period_label = period.strftime("%B %Y")

        summary = summarise_period(breakdowns)
        _stat_cards(
            [
                ("Total spent", _chf(summary.total_paid / 100), f"Printed receipt totals, {period_label}"),
                ("Receipts & payments", f"{summary.receipt_count}", f"Logged in {period_label}"),
                ("Average per person", _chf(summary.total_share / 300), "Mean share of what was bought"),
            ]
        )

        st.subheader("Who spent how much")
        st.caption(
            "Paid = money they put down at the till. Their share = what the things they "
            "benefit from cost. The difference is what they fronted for others (or owe)."
        )
        person_cards = []
        for name in ROOMMATES:
            code = ROOMMATE_CODES[name]
            paid = summary.paid[code] / 100
            share = summary.share[code] / 100
            diff = paid - share
            if diff > 0.004:
                diff_html = f'<span class="wg-pill wg-pill-good">▲ Fronted {escape(_chf(diff))} for others</span>'
            elif diff < -0.004:
                diff_html = f'<span class="wg-pill wg-pill-bad">▼ Used {escape(_chf(-diff))} more than paid</span>'
            else:
                diff_html = '<span class="wg-pill">● Even</span>'
            person_cards.append(
                f"""<div class="wg-card wg-person" style="--wg-accent:{_ROOMMATE_COLORS[name]}">
                <div class="wg-person-head"><span class="wg-avatar">{escape(name[0])}</span>
                <div class="wg-label" style="font-size:1rem;font-weight:700;opacity:1">{escape(name)}</div></div>
                <div class="wg-split">
                  <div><div class="wg-label">Paid</div><div class="wg-value">{escape(_chf(paid))}</div>
                  <div class="wg-note">{summary.receipts_paid[code]} receipt(s)</div></div>
                  <div><div class="wg-label">Their share</div><div class="wg-value">{escape(_chf(share))}</div>
                  <div class="wg-note">{(share / (summary.total_share / 100) * 100) if summary.total_share else 0:.0f}% of the total</div></div>
                </div>{diff_html}</div>"""
            )
        st.markdown(f'<div class="wg-grid">{"".join(person_cards)}</div>', unsafe_allow_html=True)

        if summary.mismatched_ids:
            st.warning(
                f"{len(summary.mismatched_ids)} receipt(s) in this period have items that don't add "
                "up to the printed total, so 'paid' and 'share' totals differ by "
                f"{_chf(abs(summary.total_paid - summary.total_share) / 100)}. Fix them in Receipts."
            )
        if summary.unknown_payer_ids:
            st.warning(
                f"{len(summary.unknown_payer_ids)} receipt(s) have no recognised payer; they count in "
                "the collective total but not under any roommate."
            )

        summary_rows = [
            {
                "Roommate": roommate,
                "Paid at the till": summary.paid[code] / 100,
                "Share of items": summary.share[code] / 100,
                "Difference": (summary.paid[code] - summary.share[code]) / 100,
                "Receipts paid": summary.receipts_paid[code],
            }
            for roommate, code in ROOMMATE_CODES.items()
        ]
        summary_rows.append(
            {
                "Roommate": "Collective total",
                "Paid at the till": summary.total_paid / 100,
                "Share of items": summary.total_share / 100,
                "Difference": (summary.total_paid - summary.total_share) / 100,
                "Receipts paid": summary.receipt_count,
            }
        )

        with st.expander("Table view"):
            st.caption(
                "Paid at the till is what they paid at checkout; share of items is their portion "
                "of what was bought. Difference shows who fronted more than they used (before settlements)."
            )
            st.dataframe(
                pd.DataFrame(summary_rows),
                hide_index=True,
                height="content",
                column_config={
                    column: st.column_config.NumberColumn(column, format="CHF %.2f")
                    for column in ["Paid at the till", "Share of items", "Difference"]
                },
            )

        paid_col, cat_col = st.columns(2, gap="large")
        with paid_col:
            st.markdown("**Paid at the till vs. share of items**")
            fig_paid = go.Figure()
            fig_paid.add_bar(
                x=ROOMMATES,
                y=[summary.paid[ROOMMATE_CODES[n]] / 100 for n in ROOMMATES],
                name="Paid at the till",
                marker=dict(color=_SINGLE_SERIES_COLOR, cornerradius=4),
                hovertemplate="%{x} paid CHF %{y:,.2f}<extra></extra>",
            )
            fig_paid.add_bar(
                x=ROOMMATES,
                y=[summary.share[ROOMMATE_CODES[n]] / 100 for n in ROOMMATES],
                name="Share of items",
                marker=dict(color=_NEUTRAL_SERIES_COLOR, cornerradius=4),
                hovertemplate="%{x}'s share CHF %{y:,.2f}<extra></extra>",
            )
            fig_paid.update_layout(barmode="group", bargroupgap=0.08)
            fig_paid.update_yaxes(title_text="CHF", rangemode="tozero")
            _show_fig(_style_fig(fig_paid))
        with cat_col:
            st.markdown("**Spending by category**")
            period_categories = _category_series(breakdowns, df_all_items)
            if period_categories.empty:
                st.info("No categorised items in this period.")
            else:
                _show_fig(_hbar_chart(period_categories, height=320))

        ledger = _ledger_frame(breakdowns)
        if not ledger.empty:
            store_totals = ledger.groupby("Store")["Total"].sum().sort_values().tail(8)
            store_totals = store_totals[store_totals > 0]
            if not store_totals.empty:
                st.markdown("**Top stores**")
                _show_fig(_hbar_chart(store_totals))

        st.subheader("Receipts in selected period")
        if not ledger.empty:
            st.caption("Newest first, with who paid and each roommate's share.")
            _receipts_table(ledger, with_shares=True)
            export = ledger[["Date", "Store", "Paid_By", "Total", *ROOMMATES]].copy()
            export["Date"] = export["Date"].dt.strftime("%Y-%m-%d")
            export[ROOMMATES] = export[ROOMMATES].round(2)
            st.download_button(
                label="Download CSV",
                data=export.to_csv(index=False).encode("utf-8"),
                file_name=f"wg-finance-total-spendings-{period_slug}.csv",
                mime="text/csv",
            )
        else:
            st.info("No receipts found for this period.")
    else:
        st.info("No receipts found. Upload a receipt to see total spendings.")
