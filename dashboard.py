"""Streamlit dashboard for WG Sharehouse — upload, edit, balances, and reports.

Pages:
  1. **Upload Receipt** — upload / camera-capture a receipt, parse with Groq
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
from typing import Any, Dict, List, Optional

import streamlit as st
import pandas as pd
from app.services.accounting import (
    recover_legacy_weighted_quantities,
    round_shares_to_cents,
)
from app.services.reporting import filter_receipts_for_period

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
    if logged_in:
        tabs = ["Upload Receipt", "Past Receipts", "Balances & Settlements", "Total Spendings"]
    else:
        tabs = ["View History", "Balances & Settlements", "Total Spendings"]

    selected_tab = st.radio(
        "Page",
        options=tabs,
        index=tabs.index(st.session_state.current_tab) if st.session_state.current_tab in tabs else 0,
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
            "Qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.001, format="%.3f", width="small"),
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
# Tab 2 — Past Receipts                                                        #
# =========================================================================== #

elif selected_tab == "Past Receipts":
    st.title("Past Receipts")
    st.caption("Review receipt totals and each roommate's allocated spending; edit or delete receipts.")
    if st.session_state.pop("clear_edit_receipt_select", False):
        st.session_state.pop("edit_receipt_select", None)
    delete_message = st.session_state.pop("receipt_delete_message", None)
    if delete_message:
        st.success(delete_message)

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
            if "Receipt_ID" in df_all_items.columns:
                receipt_items = df_all_items[
                    df_all_items["Receipt_ID"].astype(str).str.strip() == rid
                ]
            else:
                receipt_items = df_all_items.iloc[0:0]
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
                use_container_width=True,
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

        receipt_ids = [str(r) for r in df_receipts["Receipt_ID"].tolist()]
        selected_rid = st.selectbox("Select Receipt", options=receipt_ids, key="edit_receipt_select")

        if selected_rid:
            try:
                df_items = df_all_items[
                    df_all_items["Receipt_ID"].astype(str).str.strip() == selected_rid
                ].copy() if "Receipt_ID" in df_all_items.columns else df_all_items.iloc[0:0].copy()

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

                if not df_items.empty:
                    # Older saves truncated weighed-item quantities to integers.
                    # Recover them from the saved line total and unit price so
                    # opening/editing a past receipt does not change its value.
                    df_items = recover_legacy_weighted_quantities(df_items)
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
                        "Qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.001, format="%.3f", width="small"),
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
    st.title("Balances & Settlements")
    st.caption("Positive means others owe that roommate; negative means they owe others. Settlements reduce both sides.")
    settlement_message = st.session_state.pop("settlement_message", None)
    if settlement_message:
        st.success(settlement_message)

    try:
        with st.spinner("Loading data…"):
            from app.services.ledger import get_all_receipts, get_receipt_items, get_settlements  # type: ignore

            df_receipts = get_all_receipts()
            df_all_items = get_receipt_items()
            df_settlements = get_settlements()
    except Exception as exc:
        st.error(f"Failed to load data: {exc}")
        df_receipts = pd.DataFrame(columns=["Receipt_ID", "Date", "Store", "Paid_By"])
        df_all_items = pd.DataFrame(columns=["Receipt_ID", "Line_Total", "Beneficiary"])
        df_settlements = pd.DataFrame(columns=["Settlement_ID", "Date", "From_Roommate", "To_Roommate", "Amount", "Method"])

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
        debtor_index = creditor_index = 0
        while debtor_index < len(debtors) and creditor_index < len(creditors):
            amount_cents = min(
                debtors[debtor_index][1], creditors[creditor_index][1]
            )
            owes_rows.append(
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

        if owes_rows:
            df_owes = pd.DataFrame(owes_rows)
            st.dataframe(df_owes, hide_index=True, use_container_width=True)
        else:
            st.info("Everyone is settled up! 🎉")

        # Settlement form
        st.subheader("Log Settlement")
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

    else:
        st.info("No receipts found. Upload a receipt to start tracking balances.")


# =========================================================================== #
# Tab 4 — Total Spendings                                                     #
# =========================================================================== #

elif selected_tab == "Total Spendings":
    st.title("Total Spendings")
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
        receipt_dates = pd.to_datetime(df_receipts["Date"], errors="coerce")
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
            if "Receipt_ID" in df_all_items.columns:
                receipt_items = df_all_items[
                    df_all_items["Receipt_ID"].astype(str).str.strip() == receipt_id
                ]
            else:
                receipt_items = df_all_items.iloc[0:0]

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

        st.subheader("Spending by roommate")
        st.caption(
            "Paid for receipts is what they paid at checkout; allocated share is their portion of the items."
        )
        st.dataframe(
            pd.DataFrame(summary_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                column: st.column_config.NumberColumn(column, format="%.2f")
                for column in [
                    "Paid for receipts (CHF)",
                    "Allocated share (CHF)",
                ]
            },
        )

        st.subheader("Receipts in selected period")
        details_df = pd.DataFrame(detail_rows)
        if not details_df.empty:
            st.dataframe(
                details_df,
                hide_index=True,
                use_container_width=True,
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
