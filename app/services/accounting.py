"""Exact currency allocation helpers for the sharehouse ledger."""

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping


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
