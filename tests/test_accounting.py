"""Tests for exact roommate share allocation and currency rounding."""

from decimal import Decimal

from app.services.accounting import round_shares_to_cents


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
