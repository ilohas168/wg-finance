"""Streamlit Web Dashboard for WG Finance.

This module provides a real-time, browser-based dashboard that displays:
  - Net balances per roommate (with settlement guidance)
  - Spending breakdown charts (Plotly)
  - Full transaction history with search and date filters

## How to run locally
    streamlit run app/dashboard.py --server.port 8501

Requires the same environment variables as the webhook service:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_JSON  (or GOOGLE_CREDENTIALS_FILE)

## Free deployment to Streamlit Community Cloud
1. Push this repo to GitHub (public or invite collaborators).
2. Go to https://streamlit.io/cloud and sign in with GitHub.
3. Click "New App", select this repo + branch, set the entry point to
   ``app/dashboard.py`` and keep the rest as defaults.
4. Under **Settings > Secrets**, add:
     - ``GOOGLE_SHEET_ID`` — your Google Sheet ID
     - ``GOOGLE_CREDENTIALS_FILE`` — (if using a file) or store the JSON
       in your OS-level env via Streamlit's secret manager.
   For simpler setup, place your service-account JSON as
   ``app/credentials.json`` and commit it to a private repo; the
   dashboard looks for that path automatically.
5. Click **Deploy** — you'll get a free ``*.streamlit.app`` URL.

"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

import gspread
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

_KNOWN_SHEET_ID = "18oTLJ8Fpe_XKBdSwV0lTKe2ptSaRLIRHj_9JF0jsBq0"


def _get_sheet_id() -> str:
    """Resolve GOOGLE_SHEET_ID from Streamlit secrets, env, or known default."""
    try:
        import streamlit as st  # type: ignore[import-not-found]
        val = getattr(st, "secrets", {}).get("GOOGLE_SHEET_ID")
        if val:
            return str(val)
    except (ImportError, AttributeError):
        pass
    val = os.environ.get("GOOGLE_SHEET_ID", "")
    return str(val) if val else _KNOWN_SHEET_ID


SHEET_ID = _get_sheet_id()

_CREDENTIAL_PATHS = [
    os.environ.get("GOOGLE_CREDENTIALS_FILE", ""),
    os.path.join(os.path.dirname(__file__), "..", "credentials.json"),
    os.path.expanduser("~/.wg-finance/credentials.json"),
]

# Roommate display names — keep in sync with the webhook's ROOMMATE_MAP.
ROOMMATES = ["Shin", "Fabian", "Pierre"]


# --------------------------------------------------------------------------- #
# Data helpers (gspread)                                                      #
# --------------------------------------------------------------------------- #

def _authenticate() -> gspread.Client:
    """Authenticate gspread client from env or file."""
    cred_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if SHEET_ID and cred_json:
        creds = Credentials.from_service_account_info(
            json.loads(cred_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return gspread.Client(creds=creds)

    for path in _CREDENTIAL_PATHS:
        if path and os.path.isfile(path):
            creds = Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            return gspread.Client(creds=creds)

    raise RuntimeError(
        "Cannot authenticate: set GOOGLE_CREDENTIALS_JSON (service account JSON) "
        "or GOOGLE_CREDENTIALS_FILE / credentials.json pointing to a "
        "Google service-account key. See app/dashboard.py docstring."
    )


def fetch_transactions() -> List[Dict[str, str | float]]:
    """Read all rows from the 'Transactions' sheet (skipping header)."""
    if not SHEET_ID:
        return []

    client = _authenticate()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = spreadsheet.worksheet("Transactions")
    return worksheet.get_all_values()


def fetch_summary_ledger() -> Optional[List[Dict[str, str | float]]]:
    """Read all rows from the 'Summary Ledger' sheet."""
    if not SHEET_ID:
        return None

    client = _authenticate()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = spreadsheet.worksheet("Summary Ledger")
    return worksheet.get_all_values()


def compute_current_balances() -> Dict[str, float]:
    """Return the latest net balances for each roommate from Summary Ledger.

    Falls back to $0.00 when the sheet is empty or unavailable.
    """
    balances = {name: 0.0 for name in ROOMMATES}
    summary = fetch_summary_ledger()
    if not summary:
        return balances

    # Last row has the latest balances (columns A, B, C after date/type).
    latest = summary[-1]
    for idx, name in enumerate(ROOMMATES, start=1):  # col index 1=A, 2=B, 3=C
        key = "A_balance" if idx == 1 else "B_balance" if idx == 2 else "C_balance"
        val = latest[idx] if len(latest) > idx else "0"
        try:
            balances[name] = float(val)
        except (ValueError, TypeError):
            pass

    return balances


def settlement_advice(balances: Dict[str, float]) -> str:
    """Return a human-readable settlement instruction.

    The algorithm: the person with the most negative balance owes to the
    person with the most positive balance until all are near zero.
    """
    sorted_pos = sorted(
        [(n, b) for n, b in balances.items() if b > 0.01],
        key=lambda x: -x[1],
    )
    sorted_neg = sorted(
        [(n, b) for n, b in balances.items() if b < -0.01],
        key=lambda x: x[1],
    )

    if not sorted_pos and not sorted_neg:
        return "Everyone is settled! No one owes anyone anything."

    steps: List[str] = []
    pos_ptr, neg_ptr = 0, 0
    while pos_ptr < len(sorted_pos) and neg_ptr < len(sorted_neg):
        giver_name, giver_bal = sorted_neg[neg_ptr]
        receiver_name, receiver_bal = sorted_pos[pos_ptr]
        amount = min(abs(giver_bal), receiver_bal)

        if amount > 0.01:
            steps.append(f"{giver_name} pays {receiver_name} ${amount:.2f}")

        giver_bal += amount
        receiver_bal -= amount
        if abs(giver_bal) < 0.01:
            neg_ptr += 1
        else:
            sorted_neg[neg_ptr] = (giver_name, round(giver_bal, 2))
        if abs(receiver_bal) < 0.01:
            pos_ptr += 1
        else:
            sorted_pos[pos_ptr] = (receiver_name, round(receiver_bal, 2))

    return "To settle up: " + "; ".join(steps) + "."


# --------------------------------------------------------------------------- #
# Chart helpers                                                               #
# --------------------------------------------------------------------------- #

def _find_col(columns: List[str], *candidates: str) -> Optional[str]:
    """Return the first column matching any of the candidate substrings."""
    for col in columns:
        cl = col.lower().strip()
        if any(cand.lower() in cl for cand in candidates):
            return col
    return None


def spending_by_category(df: pd.DataFrame) -> go.Figure:
    """Pie chart of spending by Split Category."""
    cat_col = _find_col(list(df.columns), "split", "category")
    amt_col = _find_col(list(df.columns), "price")

    if not cat_col or not amt_col or len(df) == 0:
        return px.pie(
            names=["No data"], values=[0],
            title="Spending by Split Category — no data available",
            hole=0.4,
        )

    agg = (pd.to_numeric(df[amt_col], errors="coerce")
           .fillna(0)
           .groupby(df[cat_col].fillna("Unknown"))
           .sum())
    labels = [str(k) for k in agg.index]
    values = [round(float(v), 2) for v in agg.values]

    return px.pie(
        names=labels,
        values=values,
        title="Spending by Split Category",
        hole=0.4,
    )


# --------------------------------------------------------------------------- #
# Streamlit app                                                               #
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=60)  # cache for 60 seconds to avoid hitting Sheets quota
def _cached_transactions() -> List[Dict[str, str | float]]:
    return fetch_transactions()


@st.cache_data(ttl=60)
def _cached_balances() -> Dict[str, float]:
    return compute_current_balances()


def main():
    st.set_page_config(
        page_title="Sharehouse Ledger",
        page_icon="🏠",
        layout="wide",
    )

    st.title("🏠 Sharehouse Ledger & Expense Dashboard")
    st.caption(
        "Live data from your Google Sheet. Refresh the page to pull fresh data."
    )

    # ------------------------------------------------------------------ #
    # Balances                                                            #
    # ------------------------------------------------------------------ #
    balances = _cached_balances()
    col_a, col_b, col_c = st.columns(3)

    for col, name in zip([col_a, col_b, col_c], ROOMMATES):
        val = balances[name]
        color = "green" if val > 0.01 else ("red" if val < -0.01 else "gray")
        label = f"is owed CHF {val:,.2f}" if val >= 0 else f"owes CHF {abs(val):,.2f}"
        col.metric(
            label=name,
            value=f"CHF {val:,.2f}",
            delta=label,
            delta_color="inverse" if val < 0 else "normal",
        )

    # Settlement guidance
    st.divider()
    advice = settlement_advice(balances)
    st.info(advice)

    st.divider()

    # ------------------------------------------------------------------ #
    # Charts                                                              #
    # ------------------------------------------------------------------ #
    transactions = _cached_transactions()

    if not transactions:
        st.warning(
            "No transactions found. Make sure GOOGLE_SHEET_ID is set and the "
            "sheet has data."
        )
        return

    df = pd.DataFrame(transactions)
    # Normalise column names to lower-case with stripped whitespace.
    df.columns = [c.strip().lower() for c in df.columns]

    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        fig_pie = spending_by_category(df)
        st.plotly_chart(fig_pie, use_container_width=True)

    # Bar chart of total spending per roommate
    price_col_name = _find_col(list(df.columns), "price")
    payer_col_name = _find_col(list(df.columns), "payer")

    bar_data: List[Dict[str, object]] = []
    for name in ROOMMATES:
        mask = pd.Series([False] * len(df))
        if payer_col_name:
            mask = df[payer_col_name].astype(str).str.contains(name, case=False, na=False)
        total = float(df.loc[mask, price_col_name].sum()) if price_col_name and mask.any() else 0.0
        bar_data.append({"roommate": name, "total_spent": total})

    df_bar = pd.DataFrame(bar_data)
    fig_bar = px.bar(
        df_bar, x="roommate", y="total_spent",
        title="Total Spending by Roommate",
        labels={"total_spent": "Total Spent ($)"},
    )
    with col_chart2:
        st.plotly_chart(fig_bar, use_container_width=True)

    # ------------------------------------------------------------------ #
    # Transaction History                                                 #
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("Transaction History")

    # Filters
    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        search = st.text_input("Search (merchant, item, payer)", "")
    with filter_col2:
        start_date = st.date_input("From", value=date.today() - timedelta(days=30))
    with filter_col3:
        end_date = st.date_input("To", value=date.today())

    # Apply filters
    filtered_df = df.copy()

    if search:
        q = search.lower()
        mask = pd.Series([False] * len(filtered_df))
        for col in filtered_df.columns:
            mask |= filtered_df[col].astype(str).str.contains(q, case=False, na=False)
        filtered_df = filtered_df[mask]

    # Date filter — try common date columns.
    date_col = _find_col(list(filtered_df.columns), "date")
    if date_col:
        filtered_df[date_col] = (
            pd.to_datetime(filtered_df[date_col], errors="coerce").dt.date
        )
        date_mask = (filtered_df[date_col] >= start_date) & (
            filtered_df[date_col] <= end_date
        )
        filtered_df = filtered_df[date_mask]

    # Show table
    st.dataframe(
        filtered_df,
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
