"""Exact currency allocation helpers for the sharehouse ledger."""

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Mapping

import pandas as pd


def resolve_line_item_amounts(
    original_quantity: float,
    original_unit_price: float,
    original_discount: float,
    original_line_total: float,
    quantity: float,
    unit_price: float,
    discount: float,
    line_total: float,
) -> tuple[float, float]:
    """Resolve edited amount fields, giving a changed Line Total precedence.

    If Line Total was left alone, edits to quantity, unit price, or discount
    recalculate it. If Line Total changed, adjust unit price to keep the fields
    consistent. With no amount edits, preserve the stored total as-is.
    """
    def to_cents(value: float) -> int:
        return int(
            (Decimal(str(value)) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    amount_fields_changed = any(
        abs(new_value - old_value) > 0.000001
        for new_value, old_value in (
            (quantity, original_quantity),
            (unit_price, original_unit_price),
            (discount, original_discount),
        )
    )

    if to_cents(line_total) != to_cents(original_line_total):
        resolved_total = float(line_total)
        if quantity > 0:
            unit_price = (resolved_total + discount) / quantity
    elif amount_fields_changed:
        resolved_total = quantity * unit_price - discount
    else:
        resolved_total = original_line_total

    return float(resolved_total), float(unit_price)


def recover_legacy_weighted_quantities(items: pd.DataFrame) -> pd.DataFrame:
    """Restore fractional quantities from saved line totals where possible."""
    result = items.copy()
    numeric_columns = ("Qty", "Unit_Price", "Discount", "Line_Total")
    for column in numeric_columns:
        if column in result.columns:
            result[column] = pd.to_numeric(
                result[column], errors="coerce"
            ).fillna(0)

    required = {"Qty", "Unit_Price", "Discount", "Line_Total"}
    if not required.issubset(result.columns):
        return result

    # Sheets often returns Qty as int64 when every stored value is whole. Cast
    # before restoring fractions so pandas can accept weighted quantities.
    result["Qty"] = result["Qty"].astype(float)
    inferred_qty = (result["Line_Total"] + result["Discount"]) / result[
        "Unit_Price"
    ].replace(0, float("nan"))
    formula_total = result["Qty"] * result["Unit_Price"] - result["Discount"]
    recover_qty = (
        (result["Unit_Price"] > 0)
        & (inferred_qty > 0)
        & ((formula_total - result["Line_Total"]).abs() > 0.01)
    )
    result.loc[recover_qty, "Qty"] = inferred_qty.loc[recover_qty].astype(float)
    return result

def round_shares_to_cents(
    exact_shares: Mapping[str, Decimal], total_cents: int
) -> dict[str, int]:
    """Round exact roommate shares to cents while preserving their total.

    Any remainder is assigned by largest fractional part. Callers can use this
    once per account period to avoid repeatedly giving rounding cents to the
    same roommate across many receipts.
    """
    shares = {
        code: int(amount.to_integral_value(rounding=ROUND_FLOOR))
        for code, amount in exact_shares.items()
    }
    if not shares:
        return shares

    cents_left = total_cents - sum(shares.values())
    fractions = {
        code: exact_shares[code] - Decimal(shares[code])
        for code in shares
    }
    ranked = sorted(shares, key=lambda code: (-fractions[code], code))

    if cents_left >= 0:
        for index in range(cents_left):
            shares[ranked[index % len(ranked)]] += 1
    else:
        # This branch is only needed if callers supply a target that differs
        # from the exact sum; reduce the smallest remainders first.
        ranked.reverse()
        for index in range(-cents_left):
            shares[ranked[index % len(ranked)]] -= 1

    return shares
