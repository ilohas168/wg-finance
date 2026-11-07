"""Single-request line-item receipt parser using Groq vision.

Provides ``parse_itemized_receipt()`` which extracts individual line-items (name,
price, quantity, category) from a receipt image with one Qwen vision request.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional

import groq
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Pydantic schemas                                                             #
# --------------------------------------------------------------------------- #


class LineItem(BaseModel):
    """A single line-item extracted from a receipt image."""

    name: str = Field(description="Descriptive item name.")
    price: float = Field(
        description=(
            "Final line total printed on the receipt, after any line discount. "
            "Positive for charges; negative for separate discount/refund lines."
        ),
    )
    qty: float = Field(
        default=1.0,
        gt=0,
        description="Quantity of the item; may be fractional for weighed goods, such as 0.42 kg.",
    )
    category: str = Field(
        default="General",
        description="Category tag: Food, Drink, Toiletries, Household, General — never null or empty.",
    )
    beneficiary: Literal["A", "B", "C", "AB", "BC", "AC", "ALL"] = Field(
        default="ALL",
        description="Who shares this item's cost: A, B, C, AB, BC, AC, or ALL.",
    )


class ItemizedReceipt(BaseModel):
    """Top-level parsed receipt returned by Groq vision."""

    receipt_id: str = Field(default="", description="Auto-generated receipt ID (REC-YYYYMMDD-HHMMSS).")
    paid_by: str = Field(default="Roommate 1", description="Who paid this receipt.")
    header_discounts: float = Field(default=0.0, description="Receipt-level subtotal discount.")
    merchant: str = Field(description="Store / merchant name.")
    date: str = Field(description="Purchase date in YYYY-MM-DD format.")
    date_printed: str = Field(
        default="",
        description="The purchase date copied exactly as printed, e.g. 7.11.2026.",
    )
    total_amount: float = Field(description="Grand total from the receipt footer.")
    items: List[LineItem] = Field(
        min_length=0, description="Extracted line-items (may be empty on poor OCR)."
    )


# --------------------------------------------------------------------------- #
# Model                                                                        #
# --------------------------------------------------------------------------- #

_VISION_MODEL = "qwen/qwen3.8-27b"

_SYSTEM_PROMPT: str = """You extract receipts into JSON for a 3-person sharehouse.
Return valid JSON only, with no markdown or extra text.

Read the receipt carefully, especially the final amount at the bottom:
- total_amount MUST be copied exactly from the printed grand total / amount paid.
  It is the source of truth. Never calculate or guess it from the item rows.
- Receipt item prices are normally tax-inclusive. Do not add a VAT/tax breakdown
  to total_amount a second time.
- For Swiss receipts with Artikel, Menge, Preis, Aktion, and Total columns,
  read each physical row horizontally. Return one item per printed row, in the
  same order. Never move a number to the row above or below it.
- Transcribe each product name as it is printed, preserving its spelling,
  capitalization, abbreviations, and visible truncation. Do not autocorrect,
  translate, expand, or replace an unfamiliar name with a likely product name.
  If a character is genuinely unreadable, mark only that character with `?`;
  do not guess the rest of the name.
- Read qty from Menge, including fractional weights such as 0.420 kg. Read
  price from the rightmost Total column on that same row. Preis is the unit
  price; Aktion is informational. Do not multiply Total by qty or subtract
  Aktion from Total because Total already reflects the final line amount.
- Include every charge and discount exactly once. A separate Rabatt/refund
  row with a trailing minus (such as 3.60-) is one negative-price item (-3.60).
  Do not also create a discount field or subtract the amount a second time.
- header_discounts is a positive amount for a receipt-wide discount that is
  not already included in item prices. Use 0 when absent.
- beneficiary is the only per-item allocation field: one of A, B, C, AB, BC,
  AC, or ALL. Default to ALL. Do not output a separate Shared/Private field.
- category must be one of Food, Drink, Toiletries, Household, General.
- date_printed: copy the purchase date character for character exactly as it
  is printed (for example "7.11.2026" or "07.11.26"). Do not reorder it.
- date: the same date as YYYY-MM-DD. Swiss receipts always print
  day.month.year, never month.day.year: 7.11.2026 is 2026-11-07 (7 November),
  12.9.2026 is 2026-09-12. Infer only when no date is printed.

JSON shape:
{
  "merchant": "store name",
  "date": "2026-11-07",
  "date_printed": "7.11.2026",
  "total_amount": 14.74,
  "header_discounts": 0.0,
  "items": [
    {"name": "Milk", "price": 3.50, "qty": 2, "category": "Drink", "beneficiary": "ALL"},
    {"name": "Weighed item", "price": 5.55, "qty": 0.420, "category": "Food", "beneficiary": "ALL"},
    {"name": "Rabatt", "price": -1.20, "qty": 1, "category": "General", "beneficiary": "ALL"}
  ]
}
"""


def _get_client() -> groq.Groq:
    """Create a Groq client from the GROQ_API_KEY environment variable."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Set it in your .env file."
        )
    return groq.Groq(api_key=api_key)


# --------------------------------------------------------------------------- #
# JSON helpers                                                                 #
# --------------------------------------------------------------------------- #

def _strip_markdown_json(raw: str) -> str:
    """Remove markdown code fences and surrounding whitespace to get raw JSON."""
    stripped = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _image_data_url(image_bytes: bytes) -> str:
    """Base64-wrap the original JPEG/PNG bytes without changing the image."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    else:
        raise ValueError("Receipt image must be a JPEG or PNG file.")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

_PRINTED_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def _make_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_receipt_date(
    printed: str, model_iso: str, today: Optional[date] = None
) -> Optional[date]:
    """Return the purchase date without the model's day/month mix-ups.

    Vision models tend to convert "7.11.2026" the US way (month first) into
    2026-07-11. So the printed text is read here instead, with the Swiss rule:
    day.month.year, always. Only when nothing usable was printed is the
    model's ISO date used; if that lies in the future but swapping day and
    month gives a past date, it was almost certainly swapped, so swap it back.
    """
    today = today or date.today()
    match = _PRINTED_DATE.search(printed or "")
    if match:
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        resolved = _make_date(year, month, day)
        if resolved:
            return resolved
    match = _ISO_DATE.match((model_iso or "").strip())
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    resolved = _make_date(year, month, day)
    swapped = _make_date(year, day, month) if day <= 12 and day != month else None
    if resolved and resolved > today + timedelta(days=1) and swapped and swapped <= today:
        return swapped
    return resolved or swapped


def parse_itemized_receipt(image_bytes: bytes) -> ItemizedReceipt:
    """Parse and validate a receipt with one Groq vision request.

    Parameters
    ----------
    image_bytes :
        Raw JPEG / PNG image data.

    Returns
    -------
    ItemizedReceipt
        Parsed and validated receipt structure.

    Raises
    ------
    ValueError
        If Groq returns no content or the response cannot be parsed.
    """
    # Preserve the uploaded image exactly; base64 is only the API transport format.
    image_url = _image_data_url(image_bytes)
    logger.info("vision_line_items: parsing with '%s'", _VISION_MODEL)
    response = _get_client().chat.completions.create(
        model=_VISION_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Parse this receipt. Return structured JSON."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=4096,
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("Groq returned no content for the receipt image.")
    receipt = ItemizedReceipt.model_validate_json(_strip_markdown_json(content))
    resolved_date = resolve_receipt_date(receipt.date_printed, receipt.date)
    if resolved_date:
        if resolved_date.isoformat() != receipt.date:
            logger.info(
                "vision_line_items: date %r (printed %r) resolved to %s",
                receipt.date, receipt.date_printed, resolved_date,
            )
        receipt.date = resolved_date.isoformat()

    # Compare totals locally and report discrepancies without another model call.
    item_total = round(
        sum(item.price for item in receipt.items) - receipt.header_discounts,
        2,
    )
    gap = round(receipt.total_amount - item_total, 2)
    if abs(gap) > 0.05:
        logger.warning(
            "vision_line_items: item rows total %.2f differs from footer %.2f by %.2f",
            item_total,
            receipt.total_amount,
            gap,
        )

    logger.info(
        "vision_line_items parsed with '%s': merchant=%s items=%d total=%.2f",
        _VISION_MODEL,
        receipt.merchant,
        len(receipt.items),
        receipt.total_amount,
    )
    return receipt


def parse_itemized_receipt_dict(image_bytes: bytes) -> Dict[str, Any]:
    """Thin wrapper that returns a plain dict for use in Streamlit callbacks."""
    receipt = parse_itemized_receipt(image_bytes)
    return json.loads(receipt.model_dump_json())
