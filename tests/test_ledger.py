"""Tests for the Ledger engine (app/models split logic).

Verifies that every ``SplitType`` produces correct per-person bearings
and that ``compute_bearings`` chains correctly.
"""

import pytest

from app.models import (
    LedgerEntry,
    ReceiptItem,
    SplitType,
    compute_bearings,
    split_ratios,
)

# calculate_split is in the ledger service; import here for tests.
from app.services.ledger import calculate_split  # noqa: F401


# ------------------------------------------------------------------ #
# split_ratios helper                                                  #
# ------------------------------------------------------------------ #

class TestSplitRatios:
    """Unit tests for the ``split_ratios`` dictionary helper."""

    @pytest.mark.parametrize("stype,expected", [
        (SplitType.SPLIT_3,  {"A": pytest.approx(1 / 3), "B": pytest.approx(1 / 3), "C": pytest.approx(1 / 3)}),
        (SplitType.ONLY_A,   {"A": 1.0, "B": 0.0, "C": 0.0}),
        (SplitType.ONLY_B,   {"A": 0.0, "B": 1.0, "C": 0.0}),
        (SplitType.ONLY_C,   {"A": 0.0, "B": 0.0, "C": 1.0}),
        (SplitType.SPLIT_AB, {"A": 0.5, "B": 0.5, "C": 0.0}),
        (SplitType.SPLIT_BC, {"A": 0.0, "B": 0.5, "C": 0.5}),
        (SplitType.SPLIT_AC, {"A": 0.5, "B": 0.0, "C": 0.5}),
    ])
    def test_all_split_types_return_correct_ratios(self, stype, expected):
        result = split_ratios(stype)
        assert result["A"] == pytest.approx(expected["A"])
        assert result["B"] == pytest.approx(expected["B"])
        assert result["C"] == pytest.approx(expected["C"])


# ------------------------------------------------------------------ #
# calculate_split (dollar-bearing math)                                #
# ------------------------------------------------------------------ #

class TestCalculateSplit:
    """Unit tests for the ``calculate_split`` function."""

    def test_split_3_on_30(self):
        result = calculate_split(30.0, SplitType.SPLIT_3)
        assert result["A"] == pytest.approx(10.0)
        assert result["B"] == pytest.approx(10.0)
        assert result["C"] == pytest.approx(10.0)

    def test_only_a_on_25(self):
        result = calculate_split(25.0, SplitType.ONLY_A)
        assert result["A"] == 25.0
        assert result["B"] == 0.0
        assert result["C"] == 0.0

    def test_only_b_on_18(self):
        result = calculate_split(18.0, SplitType.ONLY_B)
        assert result["A"] == 0.0
        assert result["B"] == 18.0
        assert result["C"] == 0.0

    def test_only_c_on_7(self):
        result = calculate_split(7.0, SplitType.ONLY_C)
        assert result["A"] == 0.0
        assert result["B"] == 0.0
        assert result["C"] == 7.0

    def test_split_ab_on_40(self):
        result = calculate_split(40.0, SplitType.SPLIT_AB)
        assert result["A"] == 20.0
        assert result["B"] == 20.0
        assert result["C"] == 0.0

    def test_split_bc_on_50(self):
        result = calculate_split(50.0, SplitType.SPLIT_BC)
        assert result["A"] == 0.0
        assert result["B"] == 25.0
        assert result["C"] == 25.0

    def test_split_ac_on_15(self):
        result = calculate_split(15.0, SplitType.SPLIT_AC)
        assert result["A"] == 7.5
        assert result["B"] == 0.0
        assert result["C"] == 7.5

    def test_bearings_sum_to_price(self):
        """For every split type, A + B + C must equal the input price."""
        for stype in SplitType:
            result = calculate_split(100.0, stype)
            assert round(result["A"] + result["B"] + result["C"], 2) == pytest.approx(100.0)

    def test_small_price_precision(self):
        """Ensure rounding works on very small prices."""
        result = calculate_split(0.10, SplitType.SPLIT_3)
        assert result["A"] + result["B"] + result["C"] == pytest.approx(0.10)


# ------------------------------------------------------------------ #
# compute_bearings (ReceiptItem → LedgerEntry chain)                   #
# ------------------------------------------------------------------ #

class TestComputeBearings:
    """Unit tests for the ``compute_bearings`` pipeline."""

    def test_single_item_compute(self):
        items = [ReceiptItem(item_name="Milk", price=4.00)]
        entries = compute_bearings(items)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.item_name == "Milk"
        assert entry.price == 4.00
        # Default is SPLIT_3 — rounding residual goes to A.
        assert entry.a_bears == pytest.approx(1.34)  # gets rounding residual
        assert entry.b_bears == pytest.approx(1.33)
        assert entry.c_bears == pytest.approx(1.33)

    def test_mixed_splits(self):
        items = [
            ReceiptItem(item_name="Rice", price=6.00),           # SPLIT_3 (default)
            ReceiptItem(item_name="Coffee", price=12.00, assigned_split=SplitType.ONLY_A),
        ]
        entries = compute_bearings(items)
        assert len(entries) == 2

        # Rice: each pays ~2.00
        assert entries[0].a_bears == pytest.approx(2.0)
        assert entries[0].b_bears == pytest.approx(2.0)
        assert entries[0].c_bears == pytest.approx(2.0)

        # Coffee: A only
        assert entries[1].a_bears == 12.0
        assert entries[1].b_bears == 0.0
        assert entries[1].c_bears == 0.0

    def test_ledger_entry_field_defaults(self):
        """New LedgerEntry rows should have sensible defaults for unpopulated fields."""
        items = [ReceiptItem(item_name="Salt", price=2.00)]
        entries = compute_bearings(items)
        assert entries[0].date == ""
        assert entries[0].payer_phone == ""
        assert entries[0].payer_name == ""

    def test_all_split_types_in_compute(self):
        """compute_bearings must handle every SplitType without error."""
        items: list = [
            ReceiptItem(item_name="Milk", price=3.00),
        ]
        # Should not raise for any split type.
        for stype in SplitType:
            item = ReceiptItem(
                item_name=f"Test_{stype.value}",
                price=10.0,
                assigned_split=stype,
            )
            entries = compute_bearings([item])
            assert len(entries) == 1
            total_bearing = round(entries[0].a_bears + entries[0].b_bears + entries[0].c_bears, 2)
            assert total_bearing == pytest.approx(10.0)
