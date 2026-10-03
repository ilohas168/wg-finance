"""Tests for the Vision parsing module (app/models).

Validates that ``ReceiptData`` and ``ReceiptItem`` schemas correctly
enforce constraints and reject invalid payloads.
"""

import pytest
from pydantic import ValidationError

from app.models import ReceiptData, ReceiptItem, SplitType


# ------------------------------------------------------------------ #
# ReceiptItem schema validation                                        #
# ------------------------------------------------------------------ #

class TestReceiptItemSchema:
    """Unit tests for the ``ReceiptItem`` Pydantic model."""

    def test_valid_item_defaults_to_split_3(self):
        item = ReceiptItem(item_name="Bananas", price=2.50)
        assert item.item_name == "Bananas"
        assert item.price == 2.50
        assert item.assigned_split == SplitType.SPLIT_3

    def test_valid_item_with_custom_split(self):
        item = ReceiptItem(
            item_name="Protein Bars",
            price=8.99,
            assigned_split=SplitType.ONLY_A,
        )
        assert item.assigned_split == SplitType.ONLY_A


# ------------------------------------------------------------------ #
# ReceiptData schema validation                                        #
# ------------------------------------------------------------------ #

class TestReceiptDataSchema:
    """Unit tests for the ``ReceiptData`` Pydantic model."""

    @pytest.fixture
    def valid_receipt(self):
        """Factory to build a minimal valid receipt for testing."""
        items = [
            ReceiptItem(item_name="Milk", price=3.50),
            ReceiptItem(item_name="Bread", price=2.75, assigned_split=SplitType.ONLY_B),
        ]
        return ReceiptData(
            merchant="Corner Mart",
            date="2026-10-02",
            items=items,
            tax_total=0.49,
            grand_total=6.74,
        )

    def test_valid_receipt(self, valid_receipt):
        """A properly constructed receipt passes all validation."""
        assert len(valid_receipt.items) == 2
        assert valid_receipt.merchant == "Corner Mart"
        assert valid_receipt.grand_total == 6.74

    def test_validate_totals_passes(self, valid_receipt):
        """validate_totals succeeds when items + tax ≈ grand_total."""
        result = valid_receipt.validate_totals()
        assert isinstance(result, ReceiptData)

    def test_validate_totals_passes_with_ocr_discrepancy(self):
        """Receipts with minor OCR line-item total discrepancies should pass validation.

        Groq's vision model may miss or merge items, so the sum of extracted prices
        can differ from grand_total by up to the tolerance threshold (currently 50 ¢).
        """
        items = [ReceiptItem(item_name="Water", price=1.00)]
        receipt = ReceiptData(
            merchant="ConfusedOCR Mart",
            date="2026-10-02",
            items=items,
            tax_total=0.0,
            grand_total=3.50,  # OCR missed most items — but should still pass
        )
        # Should NOT raise; logs a warning instead.
        result = receipt.validate_totals()
        assert isinstance(result, ReceiptData)
        assert result.grand_total == 3.50

    def test_validate_totals_fails_on_mismatch(self):
        """Previously raised ValueError on mismatch; now logs a warning and returns."""
        items = [ReceiptItem(item_name="Water", price=1.00)]
        receipt = ReceiptData(
            merchant="Bad Store",
            date="2026-10-02",
            items=items,
            tax_total=0.0,
            grand_total=99.00,  # clearly wrong
        )
        # Should NOT raise — we log a warning instead.
        result = receipt.validate_totals()
        assert isinstance(result, ReceiptData)
        assert result.grand_total == 99.00

    def test_validate_totals_logs_warning_on_large_discrepancy(self, caplog):
        """A large discrepancy triggers a logger.warning (not an exception)."""
        import logging

        items = [ReceiptItem(item_name="Water", price=1.00)]
        receipt = ReceiptData(
            merchant="BigGap Store",
            date="2026-10-02",
            items=items,
            grand_total=50.00,
        )

        with caplog.at_level(logging.WARNING, logger="app.models"):
            receipt.validate_totals()
            assert "does not match grand total" in caplog.text

    def test_empty_items_rejected(self):
        """A receipt without line-items must be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ReceiptData(
                merchant="Empty Store",
                date="2026-10-02",
                items=[],
                grand_total=10.0,
            )
        assert "items" in str(exc_info.value)

    def test_invalid_date_format_accepted_by_schema(self):
        """The schema stores the date as a plain string — format is
        enforced at the business layer (not Pydantic)."""
        receipt = ReceiptData(
            merchant="Bad Dates",
            date="10/02/2026",  # wrong format, but schema accepts it
            items=[ReceiptItem(item_name="Pasta", price=1.50)],
            grand_total=1.50,
        )
        assert receipt.date == "10/02/2026"

    def test_tax_defaults_to_zero(self):
        """tax_total is optional and defaults to 0."""
        receipt = ReceiptData(
            merchant="No Tax Store",
            date="2026-10-02",
            items=[ReceiptItem(item_name="Candy", price=1.00)],
            grand_total=1.00,
        )
        assert receipt.tax_total == 0.0


# ------------------------------------------------------------------ #
# Negative prices (discounts, coupons, deposits)                       #
# ------------------------------------------------------------------ #

class TestNegativeItemPrices:
    """Verify that negative item prices for discounts / Pfand / Rabatt work end-to-end."""

    def test_negative_price_accepted_by_schema(self):
        """ReceiptItem must accept a negative price for a discount line."""
        item = ReceiptItem(item_name="Rabatt", price=-3.60)
        assert item.price == -3.60

    def test_receipt_with_discount_total_check(self):
        """A receipt containing a negative discount should pass validate_totals
        when the arithmetic (items + tax ≈ grand_total) holds."""
        items = [
            ReceiptItem(item_name="Milk", price=3.50),
            ReceiptItem(item_name="Rabatt", price=-1.20),
        ]
        receipt = ReceiptData(
            merchant="Edeka Berlin",
            date="2026-10-04",
            items=items,
            tax_total=0.15,
            grand_total=2.45,  # (3.50 + -1.20) + 0.15 = 2.45 ✓
        )
        result = receipt.validate_totals()
        assert isinstance(result, ReceiptData)

    def test_receipt_with_pfand_deposit(self):
        """Pfand / deposit lines use negative prices and should parse cleanly."""
        items = [
            ReceiptItem(item_name="Bier", price=4.80),
            ReceiptItem(item_name="Pfand", price=-0.50),
        ]
        receipt = ReceiptData(
            merchant="Getraenke Mark",
            date="2026-10-04",
            items=items,
            tax_total=0.0,
            grand_total=4.30,  # 4.80 + -0.50 = 4.30 ✓
        )
        receipt.validate_totals()

    def test_zero_price_rejected(self):
        """Items with exactly price=0 should raise ValueError in validate_totals."""
        items = [ReceiptItem(item_name="Free Sample", price=0)]
        receipt = ReceiptData(
            merchant="Bad Store",
            date="2026-10-04",
            items=items,
            grand_total=5.00,
        )
        with pytest.raises(ValueError, match="must not be zero"):
            receipt.validate_totals()

    def test_json_roundtrip_with_negative_price(self):
        """Negative prices survive a JSON serialise / deserialise round-trip."""
        original = ReceiptData(
            merchant="TestMart",
            date="2026-10-04",
            items=[
                ReceiptItem(item_name="Rabatt", price=-2.00),
                ReceiptItem(item_name="Coffee", price=4.50),
            ],
            tax_total=0.30,
            grand_total=2.80,
        )
        json_str = original.model_dump_json()
        restored = ReceiptData.model_validate_json(json_str)
        assert restored.items[0].price == -2.00


# ------------------------------------------------------------------ #
# Structured output round-trip                                         #
# ------------------------------------------------------------------ #

class TestStructuredOutputRoundTrip:
    """Ensure ReceiptData can serialise and deserialise (JSON round-trip)."""

    def test_json_roundtrip(self):
        original = ReceiptData(
            merchant="TestMart",
            date="2026-10-02",
            items=[
                ReceiptItem(item_name="Coffee", price=4.50),
                ReceiptItem(
                    item_name="Sugar",
                    price=3.00,
                    assigned_split=SplitType.SPLIT_AC,
                ),
            ],
            tax_total=0.56,
            grand_total=8.06,
        )
        json_str = original.model_dump_json()
        restored = ReceiptData.model_validate_json(json_str)
        assert restored.merchant == original.merchant
        assert len(restored.items) == len(original.items)
        assert restored.items[1].assigned_split == SplitType.SPLIT_AC
