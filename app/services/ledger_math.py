"""Pure money maths for the sharehouse ledger: shares, balances, and reports.

Everything here works in integer cents (or exact ``Decimal`` cents before the
final rounding) so totals shown on the dashboard always add up to the cent.

Rules
-----
* A receipt's payer is credited with the receipt's printed ``Grand_Total``
  (falling back to the item total when the cell is blank).
* Each line total is split evenly between its beneficiaries
  (``ALL`` = A, B, C; ``AB``/``BC``/``AC`` = that pair; a letter = one person).
* A receipt-wide ``Header_Discounts`` amount is subtracted from the item total
  and apportioned in proportion to each roommate's positive item spending.
* Exact shares are rounded to cents once per reporting period, never per
  receipt, so no roommate accumulates a cent of bias per receipt.
* A settlement from X to Y raises X's balance and lowers Y's by the amount.

A positive balance means the others owe that roommate money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional

import pandas as pd

from app.services.accounting import round_shares_to_cents
from app.services.reporting import parse_receipt_dates

ROOMMATES = ["Shin", "Fabian", "Pierre"]
ROOMMATE_CODES = {"Shin": "A", "Fabian": "B", "Pierre": "C"}
CODE_TO_ROOMMATE = {code: name for name, code in ROOMMATE_CODES.items()}

BENEFICIARY_LABELS: dict[str, str] = {
    "ALL": "All (Shin, Fabian, Pierre)",
    "A": "Shin",
    "B": "Fabian",
    "C": "Pierre",
    "AB": "Shin & Fabian",
    "BC": "Fabian & Pierre",
    "AC": "Shin & Pierre",
}
LABEL_TO_BENEFICIARY = {label: code for code, label in BENEFICIARY_LABELS.items()}


def beneficiary_code(value: Any) -> str:
    """Normalize a beneficiary display label or code to A/B/C/AB/BC/AC/ALL."""
    code = LABEL_TO_BENEFICIARY.get(str(value), str(value))
    return code if code in BENEFICIARY_LABELS else "ALL"


def beneficiary_codes(value: Any) -> list[str]:
    """Return every roommate code responsible for an item's cost."""
    code = beneficiary_code(value)
    return ["A", "B", "C"] if code == "ALL" else [part for part in "ABC" if part in code]


def roommate_code(value: Any) -> Optional[str]:
    """Resolve a roommate name or code to the internal A/B/C code."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip().casefold()
    for roommate, code in ROOMMATE_CODES.items():
        if text in {roommate.casefold(), code.casefold()}:
            return code
    return None


def money_to_cents(value: Any) -> int:
    """Convert a sheet value to integer cents with normal currency rounding.

    Accepts the formatted strings Google Sheets can return for a Swiss
    locale, e.g. ``"CHF 1'234.50"``, ``"1’234.50"`` or ``"12,50"``.
    """
    text = str(value).strip()
    for token in ("CHF", "Fr.", "'", "’", " ", " "):
        text = text.replace(token, "")
    if "," in text:
        # "12,50" is a decimal comma; "1,234.50" uses it as a thousands mark.
        text = text.replace(",", "") if "." in text else text.replace(",", ".")
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            return 0
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() != ""


def line_total_cents(item: Any) -> int:
    """Read a saved line total, deriving it only when the cell is blank."""
    line_total = item.get("Line_Total", "")
    if _has_value(line_total):
        return money_to_cents(line_total)
    try:
        amount = (
            Decimal(str(item.get("Qty", 1)))
            * Decimal(str(item.get("Unit_Price", 0)))
            - Decimal(str(item.get("Discount", 0)))
        )
        return money_to_cents(amount)
    except (InvalidOperation, TypeError, ValueError):
        return 0


def receipt_spending_exact(
    items_df: pd.DataFrame, header_discount_cents: int = 0
) -> tuple[dict[str, Decimal], int]:
    """Return exact cent shares and the receipt's net item total."""
    exact_spending = {code: Decimal(0) for code in "ABC"}
    if items_df.empty:
        return exact_spending, 0
    item_total_cents = 0
    for _, item in items_df.iterrows():
        amount = line_total_cents(item)
        item_total_cents += amount
        codes = beneficiary_codes(item.get("Beneficiary", "ALL"))
        exact_share = Decimal(amount) / len(codes)
        for code in codes:
            exact_spending[code] += exact_share

    # Apportion any receipt-wide discount by positive item spending before
    # rounding, so row-level remainders do not accumulate against one person.
    if header_discount_cents != 0:
        weights = {
            code: max(Decimal(0), amount) for code, amount in exact_spending.items()
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


def receipt_spending_cents(
    items_df: pd.DataFrame, header_discount_cents: int = 0
) -> dict[str, int]:
    """Allocate one receipt's net cost among its beneficiaries, in cents."""
    exact_spending, target_total_cents = receipt_spending_exact(
        items_df, header_discount_cents
    )
    return round_shares_to_cents(exact_spending, target_total_cents)


def items_for_receipt(items_df: pd.DataFrame, receipt_id: str) -> pd.DataFrame:
    """Return the saved line items that belong to one receipt."""
    if items_df.empty or "Receipt_ID" not in items_df.columns:
        return items_df.iloc[0:0].copy()
    return items_df[items_df["Receipt_ID"].astype(str).str.strip() == receipt_id].copy()


@dataclass
class ReceiptBreakdown:
    """One receipt's money facts: who paid, how much, and who it was for."""

    receipt_id: str
    date: pd.Timestamp
    store: str
    payer: Optional[str]  # roommate name, or None when unrecognised
    raw_payer: str
    paid_cents: int  # printed receipt total credited to the payer
    allocated_cents: int  # item total after header discount
    exact_shares: dict[str, Decimal]  # unrounded cents per roommate code

    @property
    def difference_cents(self) -> int:
        return self.allocated_cents - self.paid_cents


def receipt_breakdowns(
    receipts_df: pd.DataFrame, items_df: pd.DataFrame
) -> list[ReceiptBreakdown]:
    """Break every receipt down into payer credit and per-roommate shares."""
    if receipts_df.empty or "Receipt_ID" not in receipts_df.columns:
        return []
    dates = (
        parse_receipt_dates(receipts_df["Date"])
        if "Date" in receipts_df.columns
        else pd.Series(pd.NaT, index=receipts_df.index)
    )
    breakdowns = []
    for (_, receipt), parsed_date in zip(receipts_df.iterrows(), dates):
        receipt_id = str(receipt.get("Receipt_ID", "")).strip()
        if not receipt_id:
            continue
        exact_shares, allocated_cents = receipt_spending_exact(
            items_for_receipt(items_df, receipt_id),
            money_to_cents(receipt.get("Header_Discounts", 0)),
        )
        grand_total = receipt.get("Grand_Total", "")
        paid_cents = (
            money_to_cents(grand_total) if _has_value(grand_total) else allocated_cents
        )
        raw_payer = str(receipt.get("Paid_By", "") or "").strip()
        payer_code = roommate_code(raw_payer)
        breakdowns.append(
            ReceiptBreakdown(
                receipt_id=receipt_id,
                date=parsed_date,
                store=str(receipt.get("Store", "") or "").strip() or "Unknown store",
                payer=CODE_TO_ROOMMATE.get(payer_code) if payer_code else None,
                raw_payer=raw_payer,
                paid_cents=paid_cents,
                allocated_cents=allocated_cents,
                exact_shares=exact_shares,
            )
        )
    return breakdowns


def allocate_shares(breakdowns: Iterable[ReceiptBreakdown]) -> dict[str, int]:
    """Round the combined exact shares of several receipts to cents, once."""
    exact = {code: Decimal(0) for code in "ABC"}
    total = 0
    for breakdown in breakdowns:
        total += breakdown.allocated_cents
        for code, share in breakdown.exact_shares.items():
            exact[code] += share
    return round_shares_to_cents(exact, total)


@dataclass
class PeriodSummary:
    """Totals for a set of receipts, all in cents keyed by roommate code."""

    paid: dict[str, int]
    share: dict[str, int]
    receipts_paid: dict[str, int]
    receipt_count: int
    total_paid: int  # sum of printed receipt totals, including unknown payers
    total_share: int  # sum of allocated shares (= item totals after discounts)
    unknown_payer_ids: list[str]
    mismatched_ids: list[str]  # receipts whose items don't add up to the total


def summarise_period(breakdowns: list[ReceiptBreakdown]) -> PeriodSummary:
    """Who paid what and who consumed what across the given receipts."""
    paid = {code: 0 for code in "ABC"}
    receipts_paid = {code: 0 for code in "ABC"}
    unknown = []
    for breakdown in breakdowns:
        code = ROOMMATE_CODES.get(breakdown.payer or "")
        if code:
            paid[code] += breakdown.paid_cents
            receipts_paid[code] += 1
        else:
            unknown.append(breakdown.receipt_id)
    share = allocate_shares(breakdowns)
    return PeriodSummary(
        paid=paid,
        share=share,
        receipts_paid=receipts_paid,
        receipt_count=len(breakdowns),
        total_paid=sum(b.paid_cents for b in breakdowns),
        total_share=sum(share.values()),
        unknown_payer_ids=unknown,
        mismatched_ids=[b.receipt_id for b in breakdowns if abs(b.difference_cents) > 1],
    )


def compute_balances(
    breakdowns: list[ReceiptBreakdown], settlements_df: pd.DataFrame
) -> dict[str, int]:
    """Net balance per roommate code in cents (positive = is owed).

    Receipts whose payer is not a known roommate are left out entirely, since
    nobody can be credited for them; ``summarise_period`` lists them.
    """
    known = [b for b in breakdowns if b.payer]
    balances = {code: 0 for code in "ABC"}
    for breakdown in known:
        balances[ROOMMATE_CODES[breakdown.payer]] += breakdown.paid_cents
    for code, share in allocate_shares(known).items():
        balances[code] -= share

    if not settlements_df.empty:
        for _, settlement in settlements_df.iterrows():
            sender = roommate_code(settlement.get("From_Roommate"))
            receiver = roommate_code(settlement.get("To_Roommate"))
            amount = money_to_cents(settlement.get("Amount", 0))
            if sender and receiver and sender != receiver and amount > 0:
                balances[sender] += amount
                balances[receiver] -= amount
    return balances


def settle_up(balances_cents: dict[str, int]) -> list[dict[str, Any]]:
    """Greedily pair debtors with creditors into at most two transfers."""
    debtors = [
        [name, -balances_cents[ROOMMATE_CODES[name]]]
        for name in ROOMMATES
        if balances_cents[ROOMMATE_CODES[name]] < 0
    ]
    creditors = [
        [name, balances_cents[ROOMMATE_CODES[name]]]
        for name in ROOMMATES
        if balances_cents[ROOMMATE_CODES[name]] > 0
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


def category_totals_cents(
    breakdowns: list[ReceiptBreakdown], items_df: pd.DataFrame
) -> dict[str, int]:
    """Net spending per item category, with header discounts apportioned.

    Each receipt's header discount is spread across its categories in
    proportion to their positive spending, so category totals add up to the
    same net item total as the roommate shares.
    """
    exact: dict[str, Decimal] = {}
    total = 0
    for breakdown in breakdowns:
        items = items_for_receipt(items_df, breakdown.receipt_id)
        per_category: dict[str, Decimal] = {}
        item_total = 0
        for _, item in items.iterrows():
            amount = line_total_cents(item)
            item_total += amount
            category = str(item.get("Category", "") or "").strip() or "General"
            per_category[category] = per_category.get(category, Decimal(0)) + amount
        discount = item_total - breakdown.allocated_cents
        if discount:
            weights = {c: max(Decimal(0), v) for c, v in per_category.items()}
            weight_total = sum(weights.values())
            if weight_total:
                for category in per_category:
                    per_category[category] -= Decimal(discount) * weights[category] / weight_total
            else:
                per_category["General"] = per_category.get("General", Decimal(0)) - discount
        for category, amount in per_category.items():
            exact[category] = exact.get(category, Decimal(0)) + amount
        total += breakdown.allocated_cents
    return round_shares_to_cents(exact, total) if exact else {}


def manual_entry_records(
    description: str,
    amount: float,
    entry_date: str,
    payer: str,
    beneficiary: str,
    category: str = "General",
    note: str = "",
    entry_id: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the receipt row and single line item for a payment with no receipt.

    The entry is stored like a one-line receipt, so balances and reports treat
    it exactly like scanned receipts: the payer is credited ``amount`` and it
    is split evenly between the chosen beneficiaries.
    """
    code = beneficiary_code(beneficiary)
    amount = money_to_cents(amount) / 100
    split_type = "Shared" if code == "ALL" or len(code) > 1 else "Private"
    notes = "Manual entry (no receipt)" + (f": {note.strip()}" if note.strip() else "")
    receipt = {
        "Receipt_ID": entry_id,
        "Date": entry_date,
        "Store": description.strip(),
        "Paid_By": payer,
        "Header_Discounts": 0.0,
        "Grand_Total": amount,
        "Shared_Total": amount if split_type == "Shared" else 0.0,
        "Notes": notes,
    }
    item = {
        "Date": entry_date,
        "Store": description.strip(),
        "Paid_By": payer,
        "Product_Name": description.strip(),
        "Category": category,
        "Qty": 1.0,
        "Unit_Price": amount,
        "Discount": 0.0,
        "Line_Total": amount,
        "Split_Type": split_type,
        "Beneficiary": code,
    }
    return receipt, [item]
