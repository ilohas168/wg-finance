"""Single-request line-item receipt parser using Groq vision.

Provides ``parse_itemized_receipt()`` which extracts individual line-items (name,
price, quantity, category) from a receipt image with one Qwen vision request.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
from typing import Any, Dict, List, Literal, Sequence

import groq
from PIL import Image, ImageOps
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
- The user may provide several overlapping crops of the same receipt. Treat
  them as consecutive views of one receipt, combine their rows in top-to-bottom
  order, and include any row visible in an overlap only once.
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
- For Swiss numeric dates, interpret the printed order as day.month.year
  (DD.MM.YYYY), never month.day.year. For example, 12.9.2026 means
  2026-09-12, not 2026-12-09.
- Return date as YYYY-MM-DD; infer only when it is not printed.

JSON shape:
{
  "merchant": "store name",
  "date": "2026-10-03",
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


# --------------------------------------------------------------------------- #
# Image compression                                                            #
# --------------------------------------------------------------------------- #

_MAX_INLINE_IMAGE_BYTES = 6 * 1024 * 1024
_MAX_CROP_IMAGE_BYTES = 768 * 1024
_JPEG_QUALITY = 95


def _encode_image(image: Image.Image, max_bytes: int) -> bytes:
    """Encode an image as high-quality JPEG, reducing it only when necessary."""
    quality = _JPEG_QUALITY
    for _ in range(12):
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        encoded = out.getvalue()
        if len(encoded) <= max_bytes:
            return encoded

        max_dim = max(image.size)
        if max_dim > 1200:
            scale = min(0.95, math.sqrt(max_bytes / len(encoded)) * 0.95)
            new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        elif quality > 75:
            quality -= 5
        else:
            break

    raise ValueError("Could not reduce the receipt image below Groq's image request limit.")


def _prepare_images(image_bytes: bytes) -> list[tuple[bytes, str]]:
    """Prepare one image or three overlapping detail crops for a tall receipt.

    Tall images are split into three overlapping, full-width crops. This makes
    each printed row larger for OCR while preserving receipt order. Each crop
    is capped at 768 KiB so the combined base64 request stays compact.
    """
    with Image.open(io.BytesIO(image_bytes)) as source:
        image_format = (source.format or "").upper()
        image = ImageOps.exif_transpose(source).convert("RGB")

    # Keep compact, non-tall JPEGs byte-for-byte. For tall receipts, close
    # detail crops give the vision model more readable text per image.
    is_tall = image.height / image.width >= 1.25
    if not is_tall and image_format == "JPEG" and len(image_bytes) <= _MAX_INLINE_IMAGE_BYTES:
        return [(image_bytes, "image/jpeg")]

    if not is_tall:
        return [(_encode_image(image, _MAX_INLINE_IMAGE_BYTES), "image/jpeg")]

    crop_height = max(1, (image.height + 1) // 2)
    starts = (0, (image.height - crop_height) // 2, image.height - crop_height)
    crops = [image.crop((0, top, image.width, top + crop_height)) for top in starts]
    return [(_encode_image(crop, _MAX_CROP_IMAGE_BYTES), "image/jpeg") for crop in crops]


def _image_content(prepared_images: Sequence[tuple[bytes, str]]) -> list[dict[str, Any]]:
    """Encode prepared JPEGs as Groq image_url content blocks."""
    return [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{base64.b64encode(image).decode('utf-8')}"
            },
        }
        for image, mime_type in prepared_images
    ]


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

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
    # Retain high-detail crops for tall receipts, but send them in one request.
    prepared_images = _prepare_images(image_bytes)
    image_content = _image_content(prepared_images)
    logger.info("vision_line_items: parsing with '%s'", _VISION_MODEL)
    response = _get_client().chat.completions.create(
        model=_VISION_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Parse this receipt. Return structured JSON."},
                    *image_content,
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
