"""Streamlit dashboard for WG Sharehouse — upload, edit, balances, and reports.

Pages:
  1. **Upload Receipt** — upload / camera-capture a receipt, parse with Groq
     vision, edit line-items in-place, then save to Google Sheets (new schema).
  2. **Edit History** — browse historical receipts, edit line-items, resave.
  3. **Balances & Settlements** — per-roommate balances, who-owes-whom matrix,
     settlement tracking.
  4. **Parent Reports** — filtered expense history with total-spent metric and CSV export.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import streamlit as st
import pandas as pd

# --------------------------------------------------------------------------- #
# Page config                                                                  #
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="WG Sharehouse Hub", layout="wide", page_icon="")

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
        "current_tab": "Upload Receipt" if "logged_in_user" in st.session_state else "View History",
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
    st.title("WG Sharehouse Hub")

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
            if st.button("Logout", use_container_width=True):
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
    today = date.today()
    default_start = today.replace(day=1) if today.day > 1 else today - __import__("datetime").timedelta(days=today.day - 1)
    date_range = st.date_input(
        "Date Range",
        value=(default_start, today),
        key="date_range_picker",
    )
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
    if logged_in:
        tabs = ["Upload Receipt", "Edit History", "Balances & Settlements", "Parent Reports"]
    else:
        tabs = ["View History", "Balances & Settlements", "Parent Reports"]

    selected_tab = st.radio(
        "Page",
        options=tabs,
        index=tabs.index(st.session_state.current_tab) if st.session_state.current_tab in tabs else 0,
        key="current_tab",
    )

# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #


def _get_roommate_label(user: str) -> str:
    """Get the initial (A/B/C) for a roommate."""
    return _ROOMMATE_INITIALS.get(user, "X")


def _beneficiary_code(value: Any) -> str:
    """Normalize a beneficiary display label or code to A/B/C/AB/BC/AC/ALL."""
    code = _LABEL_TO_BENEFICIARY.get(str(value), str(value))
    return code if code in {"A", "B", "C", "AB", "BC", "AC", "ALL"} else "ALL"


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


def _load_items_df(raw_dict: Optional[dict]) -> pd.DataFrame:
    """Build an editable DataFrame from the vision parser output dict."""
    if raw_dict is None or not raw_dict.get("items"):
        return pd.DataFrame(columns=["Product_Name", "Category", "Qty", "Unit_Price", "Discount", "Line_Total", "Split_Type", "Beneficiary"])

    rows = []
    for item in raw_dict.get("items", []):
        qty = max(int(item.get("qty", 1)), 1)
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


# =========================================================================== #
# Tab 1 — Upload Receipt                                                     #
# =========================================================================== #

if selected_tab == "Upload Receipt":
    st.title("Upload Receipt")
    st.caption("Send a receipt photo, edit line-items, and save to the shared ledger.")

    uploaded_file = st.file_uploader("Upload receipt image", type=["png", "jpg", "jpeg"], key="file_uploader")
    camera_img = st.camera_input("Or take a photo", key="camera_input")

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
            st.session_state.receipt_total_input = float(raw_dict.total_amount)
            st.session_state.header_discounts_input = float(raw_dict.header_discounts)
            st.session_state.has_parsed = True
            st.info(f"Parsed receipt from **{raw_dict.merchant}** on **{raw_dict.date}** — {len(raw_dict.items)} items.")
        except Exception as exc:
            st.error(f"Failed to parse receipt: {exc}")

    # Edit area & summary.
    if st.session_state.has_parsed and st.session_state.raw_items_df is not None and not st.session_state.raw_items_df.empty:
        df = st.session_state.raw_items_df.copy()
        df["Line_Total"] = _compute_line_totals(df)
        st.session_state.raw_items_df = df

        st.subheader("Line-items")
        # Display labels for the Beneficiary column.
        _ben_options = list(_BENEFICIARY_LABELS.values())  # e.g. ["All (Shin…)", "Shin", ...]

        # Convert internal codes → display labels for the editor.
        if "Beneficiary" in df.columns:
            df["Beneficiary"] = df["Beneficiary"].apply(
                lambda v: _BENEFICIARY_LABELS.get(str(v), str(v))
            )

        col_config = {
            "Product_Name": st.column_config.TextColumn("Name", width="medium"),
            "Category": st.column_config.SelectboxColumn(
                "Category",
                options=["Food", "Drink", "Toiletries", "Household", "General"],
                width="small",
            ),
            "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1, width="small"),
            "Unit_Price": st.column_config.NumberColumn("Unit Price", format="%.2f", width="small"),
            "Discount": st.column_config.NumberColumn("Discount", format="%.2f", width="small"),
            "Line_Total": st.column_config.NumberColumn("Line Total", format="%.2f", disabled=True, width="small"),
            "Beneficiary": st.column_config.SelectboxColumn(
                "Who pays for this item",
                options=_ben_options,
                width="medium",
            ),
        }

        edited_df = st.data_editor(
            df.copy(),  # pass a copy so the lambda mutation doesn't persist
            column_config=col_config,
            column_order=list(col_config.keys()),
            hide_index=True,
            use_container_width=True,
            key="items_editor",
        )

        # Re-capture edits into session state.
        for col in ["Qty", "Unit_Price", "Discount"]:
            if col in edited_df.columns:
                edited_df[col] = pd.to_numeric(edited_df[col], errors="coerce").fillna(0)
        edited_df["Line_Total"] = _compute_line_totals(edited_df)
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
                        "Date": st.session_state.parsed_dict.get("date", datetime.now().strftime("%Y-%m-%d")),
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
# Tab 2 — Edit History                                                         #
# =========================================================================== #

elif selected_tab == "Edit History":
    st.title("Edit History")
    st.caption("Browse and edit historical receipts.")

    try:
        with st.spinner("Loading receipts…"):
            from app.services.ledger import get_all_receipts, get_receipt_items  # type: ignore
            df_receipts = get_all_receipts()
    except Exception as exc:
        st.error(f"Failed to load receipts: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store"])

    if not df_receipts.empty:
        receipt_ids = [str(r) for r in df_receipts["Receipt_ID"].tolist()]
        selected_rid = st.selectbox("Select Receipt", options=receipt_ids, key="edit_receipt_select")

        if selected_rid:
            try:
                with st.spinner("Loading items…"):
                    from app.services.ledger import get_receipt_items  # type: ignore
                    df_items = get_receipt_items(selected_rid)

                if not df_items.empty:
                    for col in ["Qty", "Unit_Price", "Discount", "Line_Total"]:
                        if col in df_items.columns:
                            df_items[col] = pd.to_numeric(df_items[col], errors="coerce").fillna(0)
                    df_items["Line_Total"] = _compute_line_totals(df_items)

                    st.subheader("Line-items")
                    _ben_options = list(_BENEFICIARY_LABELS.values())

                    # Convert internal codes → display labels for the editor.
                    df_items["Beneficiary"] = df_items["Beneficiary"].apply(
                        lambda v: _BENEFICIARY_LABELS.get(str(v), str(v))
                    )

                    col_config = {
                        "Product_Name": st.column_config.TextColumn("Name", width="medium"),
                        "Category": st.column_config.SelectboxColumn(
                            "Category",
                            options=["Food", "Drink", "Toiletries", "Household", "General"],
                            width="small",
                        ),
                        "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1, width="small"),
                        "Unit_Price": st.column_config.NumberColumn("Unit Price", format="%.2f", width="small"),
                        "Discount": st.column_config.NumberColumn("Discount", format="%.2f", width="small"),
                        "Line_Total": st.column_config.NumberColumn("Line Total", format="%.2f", disabled=True, width="small"),
                        "Beneficiary": st.column_config.SelectboxColumn(
                            "Who pays for this item",
                            options=_ben_options,
                            width="medium",
                        ),
                    }
                    edited_df = st.data_editor(
                        df_items,
                        column_config=col_config,
                        column_order=list(col_config.keys()),
                        hide_index=True,
                        use_container_width=True,
                        key="edit_history_editor",
                    )

                    # Compute Line_Total after edit.
                    for col in ["Qty", "Unit_Price", "Discount"]:
                        if col in edited_df.columns:
                            edited_df[col] = pd.to_numeric(edited_df[col], errors="coerce").fillna(0)
                    edited_df["Line_Total"] = _compute_line_totals(edited_df)
                    edited_df["Split_Type"] = edited_df["Beneficiary"].apply(_split_type_for_beneficiary)

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
                                    "Paid_By": selected_user,
                                    "Header_Discounts": 0.0,
                                    "Grand_Total": float(edited_df["Line_Total"].sum()),
                                    "Shared_Total": 0.0,
                                    "Notes": "",
                                }
                                items = clean_df.to_dict(orient="records")
                                update_receipt(selected_rid, receipt_data, items)
                            st.toast(f"Updated receipt **{selected_rid}**", icon="✅")
                            st.success(f"Receipt **{selected_rid}** updated successfully.")
                        except Exception as exc:
                            st.error(f"Error updating receipt: {str(exc)}")
                else:
                    st.info("No items found for this receipt.")
            except Exception as exc:
                st.error(f"Failed to load items: {exc}")
    else:
        st.info("No receipts found. Upload a receipt to get started.")


# =========================================================================== #
# Tab 3 — Balances & Settlements                                               #
# =========================================================================== #

elif selected_tab == "Balances & Settlements":
    st.title("Balances & Settlements")
    st.caption("Track who owes whom based on shared expenses and settlements.")

    try:
        with st.spinner("Loading data…"):
            from app.services.ledger import get_all_receipts, get_receipt_items, get_settlements  # type: ignore

            df_receipts = get_all_receipts()
            df_settlements = get_settlements()
    except Exception as exc:
        st.error(f"Failed to load data: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store", "Paid_By"])
        df_settlements = pd.DataFrame(columns=["Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"])

    if not df_receipts.empty:
        # Build balance dictionary per roommate
        balances = {rm: 0.0 for rm in _DEFAULT_ROOMMATES}

        for rid in df_receipts["Receipt_ID"].dropna().unique():
            try:
                items_df = get_receipt_items(str(rid))
                if items_df.empty:
                    continue
                # Get payer from Receipts sheet
                payer_row = df_receipts[df_receipts["Receipt_ID"] == rid]
                payer = str(payer_row["Paid_By"].iloc[0]) if len(payer_row) > 0 else selected_user

                for _, item in items_df.iterrows():
                    line_total = float(item.get("Line_Total", 0))
                    split_type = str(item.get("Split_Type", "Shared"))
                    beneficiary_str = str(item.get("Beneficiary", "ALL"))

                    # Payer gets out-of-pocket credit
                    if payer in balances and line_total > 0:
                        balances[payer] += line_total

                    if split_type == "Shared":
                        # Determine how many beneficiaries
                        if beneficiary_str == "ALL":
                            n_beneficiaries = 3
                        elif len(beneficiary_str) == 2:
                            n_beneficiaries = 2
                        else:
                            n_beneficiaries = 1

                        share_per_beneficiary = line_total / n_beneficiaries
                        # Deduct each beneficiary's share
                        for rm in _DEFAULT_ROOMMATES:
                            initial = _get_roommate_label(rm)
                            if initial in beneficiary_str:
                                if rm != payer:
                                    balances[rm] -= share_per_beneficiary

                    elif split_type == "Private":
                        # Only the specific beneficiary is charged (not the payer's share)
                        for rm in _DEFAULT_ROOMMATES:
                            initial = _get_roommate_label(rm)
                            if initial in beneficiary_str and rm != payer:
                                balances[rm] -= line_total

            except Exception as exc:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("Error processing receipt %s: %s", rid, exc)
                continue

        # Apply settlements
        if not df_settlements.empty and "From_Roommate" in df_settlements.columns and "Amount" in df_settlements.columns:
            for _, row in df_settlements.iterrows():
                frm = str(row.get("From_Roommate", ""))
                to = str(row.get("To_Roommate", ""))
                amt = float(row.get("Amount", 0))

                # Find matching roommate labels
                if amt > 0:
                    for rm in _DEFAULT_ROOMMATES:
                        if rm.startswith(frm) or (rm[-1] == frm[-1]):
                            balances[rm] -= amt
                            break
                    for rm in _DEFAULT_ROOMMATES:
                        if rm.startswith(to) or (rm[-1] == to[-1]):
                            balances[rm] += amt
                            break

        # Display balance metrics
        st.subheader("Current Balances")
        bal_cols = st.columns(len(_DEFAULT_ROOMMATES))
        for col, rm in zip(bal_cols, _DEFAULT_ROOMMATES):
            with col:
                val = balances.get(rm, 0.0)
                status = "green" if val > 0 else "red" if val < -0.01 else "gray"
                st.metric(
                    rm,
                    f"CHF {abs(val):,.2f}",
                    delta=f"{'Owes' if val < -0.01 else 'Is owed' if val > 0.01 else 'Settled'}",
                    delta_color="inverse" if val < 0 else "normal",
                )

        # Who-owes-whom matrix
        st.subheader("Who Owes Whom")
        owes_rows = []
        for rm_from in _DEFAULT_ROOMMATES:
            for rm_to in _DEFAULT_ROOMMATES:
                if rm_from != rm_to:
                    diff = -(balances[rm_from] + balances[rm_to])
                    # If from is negative (owes), and to is positive (is owed)
                    if balances[rm_from] < -0.01 and balances[rm_to] > 0.01:
                        net = min(abs(balances[rm_from]), balances[rm_to])
                        owes_rows.append({
                            "From": rm_from,
                            "To": rm_to,
                            "Amount": round(net, 2),
                        })

        if owes_rows:
            df_owes = pd.DataFrame(owes_rows)
            st.dataframe(df_owes, hide_index=True, use_container_width=True)
        else:
            st.info("Everyone is settled up! 🎉")

        # Settlement form
        st.subheader("Log Settlement")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            frm_rm = st.selectbox("From", _DEFAULT_ROOMMATES, key="settlement_from")
        with c2:
            to_rm = st.selectbox("To", _DEFAULT_ROOMMATES, key="settlement_to")
        with c3:
            amt = st.number_input("Amount ($)", min_value=0.0, step=0.01, format="%.2f", key="settlement_amount")
        with c4:
            method = st.selectbox("Method", ["Bank Transfer", "Twint", "Cash"], key="settlement_method")

        if st.button("Log Settlement", type="primary", key="log_settlement_btn"):
            try:
                with st.spinner("Logging settlement…"):
                    from app.services.ledger import append_settlement  # type: ignore
                    sid = append_settlement(frm_rm, to_rm, amt, method)
                st.toast(f"Settlement **{sid}** logged.", icon="✅")
            except Exception as exc:
                st.error(f"Error logging settlement: {str(exc)}")

    else:
        st.info("No receipts found. Upload a receipt to start tracking balances.")


# =========================================================================== #
# Tab 4 — Parent Reports                                                       #
# =========================================================================== #

elif selected_tab == "Parent Reports":
    st.title("Parent Reports")
    st.caption("Browse expense history filtered by roommate and date range.")

    try:
        with st.spinner("Loading data…"):
            from app.services.ledger import get_all_receipts, get_receipt_items  # type: ignore

            df_receipts = get_all_receipts()
    except Exception as exc:
        st.error(f"Failed to load receipts: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store"])

    if not df_receipts.empty and "Date" in df_receipts.columns and "Paid_By" in df_receipts.columns:
        # Filter by date range
        start_date, end_date = date_range[0], date_range[1]
        df_receipts["Date"] = pd.to_datetime(df_receipts["Date"], errors="coerce")
        df_filtered = df_receipts[
            (df_receipts["Date"] >= pd.Timestamp(start_date)) &
            (df_receipts["Date"] <= pd.Timestamp(end_date))
        ].copy()

        # Filter by selected roommate
        if "Paid_By" in df_filtered.columns:
            df_roommate = df_filtered[df_filtered["Paid_By"] == selected_user]
        else:
            df_roommate = df_filtered

        # Show total metric
        st.metric("Total Spent by Selected Roommate", f"CHF {df_roommate['Grand_Total'].sum():,.2f}" if "Grand_Total" in df_roommate.columns else "CHF 0.00")

        st.divider()

        # Display filtered DataFrame
        st.subheader(f"Receipts — {selected_user}")
        display_cols = [c for c in ["Receipt_ID", "Date", "Store", "Paid_By", "Grand_Total"] if c in df_roommate.columns]
        if display_cols:
            st.dataframe(df_roommate[display_cols], hide_index=True, use_container_width=True)

        # Download CSV button
        csv_data = df_roommate.to_csv(index=False).encode("utf-8") if not df_roommate.empty else b"Receipt_ID,Date,Store,Paid_By,Grand_Total\n"
        st.download_button(
            label="Download CSV",
            data=csv_data,
            file_name=f"wg-finance-{selected_user.replace(' ', '-')}-{start_date}-{end_date}.csv",
            mime="text/csv",
        )
    else:
        st.info("No receipts found. Upload a receipt to see reports.")
