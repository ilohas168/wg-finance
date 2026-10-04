"""Streamlit dashboard for WG Sharehouse — upload, edit, and analyse receipts.

Pages:
  1. **Upload & Edit Receipt** — upload / camera-capture a receipt, parse with Groq
     vision, edit line-items in-place, then save to Google Sheets.
  2. **Product Analytics** — search historical item purchases and view monthly quantity
     charts from the shared ledger (Google Sheets).
"""

from __future__ import annotations

import json
import io
from typing import Any, Dict, List, Optional

import streamlit as st
import pandas as pd
import plotly.express as px

# --------------------------------------------------------------------------- #
# Page config                                                                  #
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="WG Sharehouse Hub", layout="wide", page_icon="")

# --------------------------------------------------------------------------- #
# Session-state helpers                                                        #
# --------------------------------------------------------------------------- #

_DEFAULT_ROOMMATES = ["Roommate 1", "Roommate 2", "Roommate 3"]


def _init_session() -> None:
    """Ensure all required keys exist in ``st.session_state``."""
    defaults = {
        "selected_user": "Roommate 1",
        "current_page": "Upload & Edit Receipt",
        "raw_items_df": None,       # pandas DataFrame from vision parser
        "parsed_dict": None,        # raw dict returned by parse_itemized_receipt
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
    st.header("Settings")

    user_sel = st.radio(
        "Viewing as",
        options=_DEFAULT_ROOMMATES,
        index=_DEFAULT_ROOMMATES.index(st.session_state.selected_user) if st.session_state.selected_user in _DEFAULT_ROOMMATES else 0,
        key="selected_user",
    )

    st.divider()

    page_sel = st.radio(
        "Page",
        options=["Upload & Edit Receipt", "Product Analytics"],
        index=["Upload & Edit Receipt", "Product Analytics"].index(st.session_state.current_page),
        key="current_page",
    )

# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #


def _load_items_df(raw_dict: Optional[dict]) -> pd.DataFrame:
    """Build an editable DataFrame from the vision parser output dict."""
    if raw_dict is None or not raw_dict.get("items"):
        return pd.DataFrame(columns=["name", "price", "qty", "category", "is_shared"])

    rows = []
    for item in raw_dict["items"]:
        rows.append({
            "name": item.get("name", ""),
            "price": item.get("price", 0.0),
            "qty": int(item.get("qty", 1)),
            "category": item.get("category", ""),
            "is_shared": True,
        })
    df = pd.DataFrame(rows, columns=["name", "price", "qty", "category", "is_shared"])
    # Ensure bool dtype.
    df["is_shared"] = df["is_shared"].astype(bool)
    return df


def _summarise_receipt(df: pd.DataFrame) -> Dict[str, float]:
    """Calculate shared/personal totals from the DataFrame."""
    if df.empty:
        return {"shared_total": 0.0, "per_roommate_share": 0.0, "personal_total": 0.0}

    shared_mask = df["is_shared"] == True  # noqa: E712 — explicit bool comparison
    personal_mask = df["is_shared"] == False  # noqa: E712

    shared_total = float((df.loc[shared_mask, "price"] * df.loc[shared_mask, "qty"]).sum())
    personal_total = float((df.loc[personal_mask, "price"] * df.loc[personal_mask, "qty"]).sum())
    per_roommate_share = round(shared_total / 3.0, 2)

    return {
        "shared_total": round(abs(shared_total), 2),
        "per_roommate_share": abs(per_roommate_share),
        "personal_total": round(abs(personal_total), 2),
    }


# --------------------------------------------------------------------------- #
# Page 1 — Upload & Edit Receipt                                             #
# --------------------------------------------------------------------------- #

if st.session_state.current_page == "Upload & Edit Receipt":
    st.title("Upload & Edit Receipt")
    st.caption("Send a receipt photo, edit line-items, and save to the shared ledger.")

    left, right = st.columns(2)
    uploaded_file = left.file_uploader("Upload receipt image", type=["png", "jpg", "jpeg"], key="file_uploader")
    camera_img = right.camera_input("Or take a photo", key="camera_input")

    # Determine source bytes.
    image_bytes: Optional[bytes] = None
    if uploaded_file is not None:
        image_bytes = uploaded_file.getvalue()
    elif camera_img is not None:
        image_bytes = camera_img.getvalue()

    col_parse, _ = st.columns([1, 5])
    with col_parse:
        parse_clicked = st.button("Parse Receipt", type="primary", key="parse_btn")

    if parse_clicked and image_bytes:
        try:
            from app.services.vision_line_items import parse_itemized_receipt  # type: ignore

            raw_dict = parse_itemized_receipt(image_bytes)
            st.session_state.parsed_dict = raw_dict.model_dump()
            st.session_state.raw_items_df = _load_items_df(raw_dict.model_dump())
            st.session_state.has_parsed = True
            st.info(f"Parsed receipt from **{raw_dict.merchant}** on **{raw_dict.date}** — {len(raw_dict.items)} items.")
        except Exception as exc:
            st.error(f"Failed to parse receipt: {exc}")

    # Edit area & summary.
    if st.session_state.has_parsed and st.session_state.raw_items_df is not None:
        st.subheader("Line-items")
        edited_df = st.data_editor(
            st.session_state.raw_items_df,
            column_config={
                "name": st.column_config.TextColumn("Name", width="medium"),
                "price": st.column_config.NumberColumn("Price", format="%.2f", width="small"),
                "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1, width="small"),
                "category": st.column_config.TextColumn("Category", width="medium"),
                "is_shared": st.column_config.CheckboxColumn(
                    "Shared?",
                    help="Check if this item is shared among all roommates.",
                    default=True,
                ),
            },
            hide_index=True,
            use_container_width=True,
            key="items_editor",
        )

        # Re-capture edits into session state.
        st.session_state.raw_items_df = edited_df

        # Summary metrics.
        summary = _summarise_receipt(edited_df)
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Shared Total", f"${summary['shared_total']:,.2f}")
        with m2:
            st.metric("Per-Roommate Share", f"${summary['per_roommate_share']:,.2f}")
        with m3:
            st.metric("Personal Total", f"${summary['personal_total']:,.2f}")

        # Save.
        col_save, _ = st.columns([1, 5])
        with col_save:
            save_clicked = st.button("Save to Google Sheets", type="primary", key="save_btn")

        if save_clicked and edited_df is not None and not edited_df.empty:
            try:
                # ------------------------------------------------------------------ #
                # Clean the DataFrame: fill NaN, ensure clean Python types.          #
                # ------------------------------------------------------------------ #
                df_clean = edited_df.copy()
                df_clean["Category"] = df_clean["Category"].fillna("General").astype(str)
                df_clean = df_clean.fillna("")

                from app.services.ledger import append_transactions, append_line_items
                from app.models import LedgerEntry, SplitType
                from datetime import date as _date
                import asyncio

                saved_summary_count = 0
                saved_line_item_count = 0

                # Build LedgerEntry rows for the summary ledger (Sheet 1: Transactions).
                entries = []
                line_items = []
                parsed_merchant = ""
                parsed_date = str(_date.today())
                if st.session_state.parsed_dict:
                    parsed_merchant = st.session_state.parsed_dict.get("merchant", "")
                    if st.session_state.parsed_dict.get("date"):
                        parsed_date = st.session_state.parsed_dict["date"]

                for _, row in df_clean.iterrows():  # type: ignore[possibly-scalar-assignment]
                    _price_raw = row["price"]
                    _name_raw = str(row["name"]) if pd.notna(row["name"]) else ""

                    entries.append(
                        LedgerEntry(
                            date=parsed_date,
                            payer_phone="",
                            payer_name=st.session_state.selected_user,
                            merchant=parsed_merchant,
                            item_name=_name_raw,
                            price=float(_price_raw),
                            split_category=SplitType.SPLIT_3 if row["is_shared"] else SplitType.ONLY_A,  # type: ignore[arg-type]
                        )
                    )
                    line_items.append({
                        "name": _name_raw,
                        "price": float(_price_raw) if pd.notna(_price_raw) else 0.0,
                        "qty": int(row["qty"]) if pd.notna(row["qty"]) else 1,
                        "category": str(row["Category"]) if pd.notna(row["Category"]) else "General",
                        "is_shared": bool(row["is_shared"]),
                    })

                # Write summary ledger (Transactions sheet) — run async safely.
                try:
                    import nest_asyncio as _na
                    _na.apply()  # allow asyncio.run inside Streamlit's event loop
                except ImportError:
                    pass  # nest_asyncio not installed; asyncio.run may work anyway

                saved_summary_count = asyncio.run(append_transactions(entries))

                # Write itemised breakdown (Receipt_Items sheet).
                saved_line_item_count = append_line_items(
                    merchant=parsed_merchant,
                    date=parsed_date,
                    payer=st.session_state.selected_user,
                    items=line_items,
                )

                msg = (
                    f"Saved **{saved_summary_count}** transaction rows and "
                    f"**{saved_line_item_count}** line-item rows for "
                    f"{st.session_state.selected_user}."
                )
                st.toast(msg, icon="✅")
                st.success(msg)

            except Exception as exc:
                st.error(f"Error saving to Sheets: {str(exc)}")

    elif st.session_state.has_parsed and (st.session_state.raw_items_df is None or st.session_state.raw_items_df.empty):
        st.warning("Parsed receipt returned no items. Please add them manually.")


# --------------------------------------------------------------------------- #
# Page 2 — Product Analytics                                                   #
# --------------------------------------------------------------------------- #

else:  # Product Analytics
    st.title("Product Analytics")
    st.caption("Search historical item purchases and view monthly quantity trends.")

    search = st.text_input("Search product name", placeholder="e.g. Milk, Eggs, Coffee …")

    # ------------------------------------------------------------------ #
    # Fetch data from Google Sheets (or use a demo fallback).             #
    # ------------------------------------------------------------------ #
    try:
        _import_gspread = __import__("gspread")
        _cred = __import__("google.oauth2.service_account", fromlist=["Credentials"]).Credentials  # type: ignore[attr-defined]
        _creds_path = __import__("os").environ.get("GOOGLE_CREDENTIALS_JSON", "")

        if _creds_path and __import__("os").path.isfile(__import__("os").path.expanduser(_creds_path)):
            creds = _cred.from_service_account_file(
                __import__("os").path.expanduser(_creds_path),
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            gc = _import_gspread.authorize(creds)
            ss = gc.open_by_key(__import__("os").environ.get("GOOGLE_SHEET_ID", ""))
            ws = ss.worksheet("Transactions")
            rows = ws.get_all_values()

            if rows and len(rows) > 1:
                df_hist = pd.DataFrame(rows[1:], columns=rows[0])
            else:
                df_hist = pd.DataFrame(columns=["date", "merchant", "item_name", "price", "qty"])
        else:
            raise RuntimeError("No Google Sheets credentials configured.")

    except Exception:
        # Demo / fallback data.
        _demo_rows = [
            ["2026-09-01", "Edeka", "Milk", 3.50, 2],
            ["2026-09-01", "Edeka", "Bread", 2.75, 1],
            ["2026-09-08", "Rewe", "Coffee", 4.20, 1],
            ["2026-09-15", "Aldi", "Eggs", 3.10, 2],
            ["2026-09-22", "Edeka", "Milk", 3.60, 3],
            ["2026-10-01", "Rewe", "Butter", 1.80, 1],
            ["2026-10-02", "Aldi", "Coffee", 4.50, 1],
            ["2026-10-02", "Edeka", "Milk", 3.60, 2],
        ]
        _demo_cols = ["date", "merchant", "item_name", "price", "qty"]
        df_hist = pd.DataFrame(_demo_rows, columns=_demo_cols)

    # ------------------------------------------------------------------ #
    # Filtering                                                            #
    # ------------------------------------------------------------------ #
    if search.strip():
        df_filtered = df_hist[df_hist["item_name"].str.contains(search, case=False, na=False)]  # type: ignore[attr-defined]
    else:
        df_filtered = df_hist.copy()

    st.subheader("Purchase History")
    st.dataframe(df_filtered, use_container_width=True)

    # ------------------------------------------------------------------ #
    # Monthly quantity chart                                               #
    # ------------------------------------------------------------------ #
    if not df_filtered.empty and search.strip():
        try:
            df_filtered["date"] = pd.to_datetime(df_filtered["date"])  # type: ignore[call-arg, union-attr]
            df_filtered["month"] = df_filtered["date"].dt.to_period("M")  # type: ignore[union-attr]

            agg = (
                df_filtered.groupby(["month", "item_name"], observed=False)["qty"]  # type: ignore[union-attr, attr-defined]
                .sum()
                .reset_index()
            )
            fig = px.bar(
                agg,
                x="month",
                y="qty",
                color="item_name",
                barmode="group",
                title=f"Monthly Quantity — '{search}'",
                labels={"month": "Month", "qty": "Quantity"},
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception as exc:
            st.warning(f"Could not render chart: {exc}")

    elif not search.strip():
        st.info("Enter a product name to see monthly quantity trends.")
