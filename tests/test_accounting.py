"""Tests for exact roommate share allocation and currency rounding."""

from decimal import Decimal

import pandas as pd
import pytest

from app.services.accounting import (
    recover_legacy_weighted_quantities,
    round_shares_to_cents,
)


def test_rounding_preserves_receipt_total_without_row_bias():
    exact_third = Decimal(5335) / Decimal(3)

    shares = round_shares_to_cents(
        {"A": exact_third, "B": exact_third, "C": exact_third}, 5335
    )

    assert shares == {"A": 1779, "B": 1778, "C": 1778}
    assert sum(shares.values()) == 5335


def test_cumulative_rounding_avoids_one_cent_per_receipt_bias():
    exact_share_for_four_receipts = Decimal(5335 * 4) / Decimal(3)

    shares = round_shares_to_cents(
        {
            "A": exact_share_for_four_receipts,
            "B": exact_share_for_four_receipts,
            "C": exact_share_for_four_receipts,
        },
        5335 * 4,
    )

    assert shares == {"A": 7114, "B": 7113, "C": 7113}
    assert sum(shares.values()) == 5335 * 4


def test_legacy_integer_qty_column_accepts_recovered_fractional_quantities():
    items = pd.DataFrame(
        {
            "Qty": pd.Series([0, 1], dtype="int64"),
            "Unit_Price": [13.21, 1.19],
            "Discount": [0, 0],
            "Line_Total": [5.55, 1.45],
        }
    )

    recovered = recover_legacy_weighted_quantities(items)

    assert recovered["Qty"].dtype.kind == "f"
    assert recovered["Qty"].tolist() == pytest.approx([5.55 / 13.21, 1.45 / 1.19])
