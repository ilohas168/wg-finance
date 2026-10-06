"""Core Pydantic schemas for WG Finance.

Defines structured output models for receipt parsing (used with Groq Llama 3.2 Vision
structured outputs) and split-engine operations (used by the ledger service).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Split types — which roommates bear the cost of a single item.     #
# ------------------------------------------------------------------ #

class SplitType(str, Enum):
    """How to split one line-item among 3 roommates (A, B, C)."""

    SPLIT_3 = "SPLIT_3"       # All three: each pays 1/3
    ONLY_A = "ONLY_A"          # Only Shin pays 100 %
    ONLY_B = "ONLY_B"          # Only Fabian pays 100 %
    ONLY_C = "ONLY_C"          # Only Pierre pays 100 %
    SPLIT_AB = "SPLIT_AB"      # A and B each pay 50 %
    SPLIT_BC = "SPLIT_BC"      # B and C each pay 50 %
    SPLIT_AC = "SPLIT_AC"      # A and C each pay 50 %

    # Named roommate splits (A=Shin, B=Fabian, C=Pierre).
    ONLY_SHIN = "ONLY_SHIN"       # Only Shin pays 100%
    ONLY_FABI = "ONLY_FABI"       # Only Fabian pays 100%
    ONLY_PIERRE = "ONLY_PIERRE"   # Only Pierre pays 100%
    SPLIT_SF = "SPLIT_SF"         # Shin & Fabian each pay 50%
    SPLIT_SP = "SPLIT_SP"         # Shin & Pierre each pay 50%
    SPLIT_FP = "SPLIT_FP"         # Fabian & Pierre each pay 50%


# ------------------------------------------------------------------ #
# Receipt parsing (Vision output)                                    #
# ------------------------------------------------------------------ #

class ReceiptItem(BaseModel):
    """A single line-item extracted from a receipt."""

    item_name: str = Field(
        description="Descriptive name of the purchased item.",
    )
    price: float = Field(
        description=(
            "Price of this line-item (before split). "
            "Positive for regular charges; negative for discounts, deductions, and vouchers (e.g. -3.60)."
        ),
    )
    assigned_split: SplitType = Field(
        default=SplitType.SPLIT_3,
        description="Who bears the cost of this item.",
    )


class ReceiptData(BaseModel):
    """Top-level parsed receipt structure returned by Groq Llama 3.2 Vision."""

    merchant: str = Field(
        description="Name or identifier of the merchant/store.",
    )
    date: str = Field(
        description="Date of purchase in YYYY-MM-DD format.",
    )
    items: List[ReceiptItem] = Field(
        min_length=1,
        description="Extracted line-items from the receipt.",
    )
    tax_total: float = Field(
        ge=0,
        default=0.0,
        description="Total tax shown on the receipt.",
    )
    grand_total: float = Field(
        description="Grand total from the receipt.",
    )

    def validate_totals(self) -> ReceiptData:
        """Validate line-item constraints and total consistency.

        Also validates that every item's price is non-zero and grand_total is positive.
        Negative prices are allowed for discounts, deductions, and vouchers
        (e.g. ``-3.60`` for a ``Rabatt`` line).

        The sum of items + tax is compared against the receipt footer *grand_total*
        with a generous tolerance.  When OCR has missed or merged items the totals
        will differ — in that case we log a warning but **keep grand_total as the
        financial source of truth** and return the items as-is so the user can
        review/edit them in the Streamlit UI.

        (These checks can't live on the Pydantic fields because Groq's strict JSON
        object output parser rejects ``exclusiveMinimum`` — we move validation to
        business layer.)
        """
        if self.grand_total <= 0:
            raise ValueError(f"grand_total must be > 0, got {self.grand_total}")

        for item in self.items:
            if item.price == 0:
                raise ValueError(
                    f"Item '{item.item_name}' has invalid price {item.price} (must not be zero)"
                )

        subtotal = sum(item.price for item in self.items)
        calculated_total = round(subtotal + self.tax_total, 2)
        tolerance = 0.50  # generous tolerance for OCR / vision-model discrepancies

        if abs(calculated_total - self.grand_total) > tolerance:
            logger.warning(
                "Line items sum (%s) does not match grand total (%s). "
                "Keeping grand_total as financial source of truth.",
                f"{subtotal:.2f}",
                f"{self.grand_total:.2f}",
            )

        return self


# ------------------------------------------------------------------ #
# Split-engine models (ledger integration)                           #
# ------------------------------------------------------------------ #

class RoommateBalance(BaseModel):
    """Net balance for a single roommate in the sharehouse.

    Balance > 0 means others owe *this* person money.
    Balance < 0 means *this* person owes others money.
    """

    name: str = Field(description="Roommate label, e.g. 'Shin'.")
    phone: str = Field(description="WhatsApp phone number (with protocol).")
    balance: float = Field(
        default=0.0,
        description="Net balance (paid − owed). Positive = creditor.",
    )


class SharehouseLedgerState(BaseModel):
    """Aggregated state of all roommates' balances."""

    balances: List[RoommateBalance] = Field(
        min_length=3,
        max_length=3,
    )

    def total_balance(self) -> float:
        """Sum of all balances should ≈ 0 (closed system)."""
        return round(sum(b.balance for b in self.balances), 2)


class LedgerEntry(BaseModel):
    """A single row appended to the Google Sheets 'Transactions' sheet."""

    date: str = Field(description="Transaction date YYYY-MM-DD.")
    payer_phone: str = Field(description="Sender phone of this receipt.")
    payer_name: str = Field(description="Roommate label who paid.")
    merchant: str = Field(description="Store/merchant name.")
    item_name: str = Field(description="Item purchased.")
    price: float = Field(description="Price of this line-item (positive or negative for discounts).")
    split_category: SplitType = Field()
    a_bears: float = Field(ge=0, default=0.0)
    b_bears: float = Field(ge=0, default=0.0)
    c_bears: float = Field(ge=0, default=0.0)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def split_ratios(split_type: SplitType) -> dict[str, float]:
    """Return the fractional share (A=Shin, B=Fabian, C=Pierre) for a given SplitType."""
    mapping = {
        SplitType.SPLIT_3: {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
        SplitType.ONLY_A: {"A": 1.0, "B": 0.0, "C": 0.0},
        SplitType.ONLY_B: {"A": 0.0, "B": 1.0, "C": 0.0},
        SplitType.ONLY_C: {"A": 0.0, "B": 0.0, "C": 1.0},
        SplitType.SPLIT_AB: {"A": 0.5, "B": 0.5, "C": 0.0},
        SplitType.SPLIT_BC: {"A": 0.0, "B": 0.5, "C": 0.5},
        SplitType.SPLIT_AC: {"A": 0.5, "B": 0.0, "C": 0.5},
        # Named roommate splits
        SplitType.ONLY_SHIN: {"A": 1.0, "B": 0.0, "C": 0.0},
        SplitType.ONLY_FABI: {"A": 0.0, "B": 1.0, "C": 0.0},
        SplitType.ONLY_PIERRE: {"A": 0.0, "B": 0.0, "C": 1.0},
        SplitType.SPLIT_SF: {"A": 0.5, "B": 0.5, "C": 0.0},
        SplitType.SPLIT_SP: {"A": 0.5, "B": 0.0, "C": 0.5},
        SplitType.SPLIT_FP: {"A": 0.0, "B": 0.5, "C": 0.5},
    }
    return mapping[split_type]


def calculate_split(price: float, split_type: SplitType) -> dict[str, float]:
    """Return per-person bearing for a single line-item (in dollars).

    Bearings are guaranteed to sum exactly to *price* (to the cent) by
    pushing any rounding residual onto the first active roommate.
    """
    ratios = split_ratios(split_type)

    # Round each bearing individually.
    rounded: dict[str, float] = {k: round(price * v, 2) for k, v in ratios.items()}

    # The sum might differ from *price* by a few cents due to rounding.
    diff = round(price - sum(rounded.values()), 2)
    if diff != 0:
        for k in ("A", "B", "C"):
            if ratios[k] > 0:
                rounded[k] = round(rounded[k] + diff, 2)
                break

    return rounded


def compute_bearings(items: List[ReceiptItem]) -> list[LedgerEntry]:
    """Given parsed receipt items, produce one LedgerEntry per item.

    Delegates to :func:`calculate_split` so that bearing residuals are
    handled consistently.
    """
    entries: list[LedgerEntry] = []
    for item in items:
        bearings = calculate_split(item.price, item.assigned_split)
        entries.append(
            LedgerEntry(
                date="",  # filled by ledger service before DB write
                payer_phone="",  # filled by webhook handler
                payer_name="",  # filled by webhook handler
                merchant="",  # filled from ReceiptData.merchant
                item_name=item.item_name,
                price=item.price,
                split_category=item.assigned_split,
                a_bears=bearings["A"],
                b_bears=bearings["B"],
                c_bears=bearings["C"],
            )
        )
    return entries


# ------------------------------------------------------------------ #
# Beneficiary ↔ SplitType converter                                    #
# ------------------------------------------------------------------ #

# Maps each SplitType to its canonical beneficiary string.
_SPLIT_TO_BENEFICIARY = {
    SplitType.SPLIT_3: "ALL",
    SplitType.ONLY_A: "A",
    SplitType.ONLY_B: "B",
    SplitType.ONLY_C: "C",
    SplitType.SPLIT_AB: "AB",
    SplitType.SPLIT_BC: "BC",
    SplitType.SPLIT_AC: "AC",
    SplitType.ONLY_SHIN: "A",
    SplitType.ONLY_FABI: "B",
    SplitType.ONLY_PIERRE: "C",
    SplitType.SPLIT_SF: "AB",
    SplitType.SPLIT_SP: "AC",
    SplitType.SPLIT_FP: "BC",
}

# Reverse mapping from (beneficiary, length) → SplitType.
_SPLITS_FROM_BENEFICIARY = {
    ("ALL", None): SplitType.SPLIT_3,
    ("A", 1): SplitType.ONLY_A,
    ("B", 1): SplitType.ONLY_B,
    ("C", 1): SplitType.ONLY_C,
    ("AB", 2): SplitType.SPLIT_AB,
    ("BC", 2): SplitType.SPLIT_BC,
    ("AC", 2): SplitType.SPLIT_AC,
}


def beneficiary_to_split_type(beneficiary: str) -> SplitType:
    """Convert a beneficiary string (e.g. 'A', 'AB', 'ALL') to a SplitType."""
    key = (beneficiary, len(beneficiary) if beneficiary != "ALL" else None)
    return _SPLITS_FROM_BENEFICIARY.get(key, SplitType.SPLIT_3)


def split_type_to_beneficiary(split: SplitType) -> str:
    """Convert a SplitType to its canonical beneficiary string."""
    return _SPLIT_TO_BENEFICIARY.get(split, "ALL")
