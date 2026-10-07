"""Line-item receipt parser — Groq vision with multi-model fallback.

Provides ``parse_itemized_receipt()`` which extracts individual line-items (name,
price, quantity, category) from a receipt image using any of the active Groq
vision models.  If the first candidate raises a 404 it falls back automatically.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Literal

import groq
from groq import NotFoundError
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
# Model candidates (priority order)                                            #
# --------------------------------------------------------------------------- #

_VISION_MODEL_CANDIDATES: list[str] = [
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]

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
- date must use YYYY-MM-DD; infer only when it is not printed.

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


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def parse_itemized_receipt(image_bytes: bytes) -> ItemizedReceipt:
    """Parse a receipt image into an ``ItemizedReceipt`` using Groq vision.

    Automatically falls back through ``_VISION_MODEL_CANDIDATES`` when a model
    returns 404 / ``model_not_found``.

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
        If all candidate models fail or the response cannot be parsed.
    """
    candidates = list(_VISION_MODEL_CANDIDATES)
    last_exc: Optional[Exception] = None

    for model_name in candidates:
        logger.info("vision_line_items: trying model '%s'", model_name)
        client = _get_client()

        # Preserve dimensions and text detail; use JPEG to keep payloads compact.
        prepared_images = _prepare_images(image_bytes)
        image_content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64.b64encode(image).decode('utf-8')}"
                },
            }
            for image, mime_type in prepared_images
        ]

        for attempt in range(1, 4):
            try:
                response = client.chat.completions.create(
                    model=model_name,
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
                if content is None:
                    raise ValueError("Groq returned no content for the receipt image.")

                # Clean and parse.
                cleaned = _strip_markdown_json(content)
                receipt = ItemizedReceipt.model_validate_json(cleaned)

                # If extracted rows do not reconcile to the printed footer,
                # give the vision model one focused pass to re-check row/column
                # alignment. Keep whichever parse is closer; never fabricate a
                # balancing item or turn the discrepancy into a charge.
                item_total = round(
                    sum(item.price for item in receipt.items) - receipt.header_discounts,
                    2,
                )
                initial_gap = round(receipt.total_amount - item_total, 2)
                if abs(initial_gap) > 0.05:
                    logger.warning(
                        "vision_line_items: item rows total %.2f differs from footer %.2f by %.2f; rechecking receipt rows",
                        item_total,
                        receipt.total_amount,
                        initial_gap,
                    )
                    try:
                        correction_response = client.chat.completions.create(
                            model=model_name,
                            messages=[
                                {"role": "system", "content": _SYSTEM_PROMPT},
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": (
                                                "Re-read every printed item row and return corrected JSON. "
                                                f"Your previous item rows total CHF {item_total:.2f} after header "
                                                f"discounts, while the receipt footer says CHF {receipt.total_amount:.2f} "
                                                f"(difference CHF {initial_gap:.2f}). Check for omitted rows and values "
                                                "shifted between adjacent rows, especially the rightmost Total column. "
                                                "Do not invent a balancing item, duplicate an Aktion discount, or add tax twice."
                                            ),
                                        },
                                        *image_content,
                                    ],
                                },
                            ],
                            response_format={"type": "json_object"},
                            max_tokens=4096,
                        )
                        correction_content = correction_response.choices[0].message.content
                        if correction_content:
                            corrected = ItemizedReceipt.model_validate_json(
                                _strip_markdown_json(correction_content)
                            )
                            corrected_total = round(
                                sum(item.price for item in corrected.items)
                                - corrected.header_discounts,
                                2,
                            )
                            corrected_gap = round(
                                corrected.total_amount - corrected_total,
                                2,
                            )
                            if abs(corrected_gap) < abs(initial_gap):
                                receipt = corrected
                                logger.info(
                                    "vision_line_items: reconciliation pass improved gap from %.2f to %.2f",
                                    initial_gap,
                                    corrected_gap,
                                )
                            else:
                                logger.warning(
                                    "vision_line_items: reconciliation pass did not improve the total gap"
                                )
                    except Exception:
                        logger.exception(
                            "vision_line_items: reconciliation pass failed; keeping first parse"
                        )

                logger.info(
                    "vision_line_items parsed with '%s': merchant=%s items=%d total=%.2f",
                    model_name, receipt.merchant, len(receipt.items), receipt.total_amount,
                )
                return receipt

            except NotFoundError as exc:
                logger.warning("vision_line_items: model '%s' not found (404) — trying next candidate", model_name)
                last_exc = exc
                break  # abandon this model, move to next candidate

            except Exception as exc:
                last_exc = exc
                status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                error_msg = str(exc).lower()
                if status_code == 413 or "request_too_large" in error_msg or "request entity too large" in error_msg:
                    logger.warning(
                        "vision_line_items: image request too large for '%s' — trying next model",
                        model_name,
                    )
                    break
                is_transient = (
                    status_code in (429, 503)
                    or "rate limit" in error_msg
                    or "unavailable" in error_msg
                    or "overloaded" in error_msg
                )
                if is_transient and attempt < 3:
                    delay = 2 * (2 ** (attempt - 1))
                    logger.warning("vision_line_items: transient error on '%s' (attempt %d/3) — retrying in %.0fs", model_name, attempt, delay)
                    import time
                    time.sleep(delay)
                elif getattr(exc, "status_code", None) == 404 or "model_not_found" in str(exc).lower():
                    logger.warning("vision_line_items: model '%s' failed (not found?) — trying next candidate", model_name)
                    break
                else:
                    raise

    logger.exception(
        "vision_line_items: all candidates exhausted (image=%d bytes)",
        len(image_bytes),
    )
    raise ValueError(str(last_exc)) from last_exc


def parse_itemized_receipt_dict(image_bytes: bytes) -> Dict[str, Any]:
    """Thin wrapper that returns a plain dict for use in Streamlit callbacks."""
    receipt = parse_itemized_receipt(image_bytes)
    return json.loads(receipt.model_dump_json())
