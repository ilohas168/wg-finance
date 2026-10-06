"""Line-item receipt parser — Groq vision with multi-model fallback.

Provides ``parse_itemized_receipt()`` which extracts individual line-items (name,
price, quantity, category) from a receipt image using any of the active Groq
vision models.  If the first candidate raises a 404 it falls back automatically.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Literal

import groq
from groq import NotFoundError
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
            "Price of this line-item. Positive for regular charges; negative for "
            "discounts, deductions, vouchers (e.g. -3.60 for Rabatt/Pfand)."
        ),
    )
    qty: int = Field(default=1, description="Quantity of the item.")
    category: str = Field(
        default="General",
        description="Category tag: Food, Drink, Toiletries, Household, General — never null or empty.",
    )
    discount: float = Field(default=0.0, description="Line-level discount amount. Use negative for discounts.")
    split_type: Literal["Shared", "Private"] = Field(default="Shared", description="Whether this item is Shared among roommates or Private to specific ones.")
    beneficiary: str = Field(default="ALL", description="Which roommates benefit: 'A','B','C','AB','BC','AC','ALL'")


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
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3.8-27b",
]

_SYSTEM_PROMPT: str = ""

_SYSTEM_PROMPT: str = """You are a receipt parsing assistant for a 3-person sharehouse.\n
Extract every line-item from this receipt image into structured JSON.\n\n
**You MUST output valid, parseable JSON only — no markdown fences,**\n
**explanations, or text outside the JSON object.**\n\n

--- Swiss Receipt Rules (Coop / Migros / Aldi / Lidl etc.) ---\n
Swiss receipts use a column layout:\n
  `Artikel` (Name) | `Menge` (Qty/Weight) | `Preis` (Unit Price) | `Aktion` (Discount) | `Total` (Line Total)\n\n
- ALWAYS use the **rightmost `Total`** column as the line item `price`.\n
- For decimal weights in `Menge` (e.g. `0.420 kg`), set `qty = 1` and `price = Total`.\n
--- Split Fields ---\n
- Each item MUST have `split_type`: either \"Shared\" (cost shared among roommates) or \"Private\" (specific roommate only).\n
- Default `split_type` is "Shared".\n
- Each item MUST have `beneficiary`: which roommates benefit — one of \"A\",\"B\",\"C\",\"AB\",\"BC\",\"AC\",\"ALL\".\n
- Default `beneficiary` is "ALL".\n
- Line_Total calculation: `(Qty × Unit_Price) − Discount`. The Vision parser should output the line total separately from unit price.\n

- Swiss trailing minus signs indicate negatives: `"3.60-"` → `-3.60`.\n
- Items like `Rabatt`, `Aktion`, `Sonderpreis` are **discounts** — their price MUST be negative (e.g. `-3.60`).\n
- Always assign every item a non-empty `category`: one of `Food`, `Drink`, `Toiletries`, `Household`, `General`.\n

Each item must have:\n
  - "name": short descriptive name (<=60 chars)\n
  - "price": number — positive for regular charges; negative for discounts,\n
                deductions, and vouchers (e.g. -3.60 for Rabatt/Pfand)\n
  - "qty": integer quantity (default 1 when not visible)\n
  - "category": one of Food, Drink, Toiletries, Household, General — NEVER null or empty.\n
  - "discount": line-level discount amount (0.0 when none). For Rabatt/Aktion lines this should be positive (e.g. 1.20).\n
  - "split_type": "Shared" or "Private" — default "Shared".\n
  - "beneficiary": one of "A","B","C","AB","BC","AC","ALL" — default "ALL".\n\n
Receipt-level fields:\n
- `header_discounts`: receipt-wide subtotal discount (default 0.0).\n\n
Rules:\n
  - date must be YYYY-MM-DD (guess from visual cues if absent).\n
  - tax_total is the total tax line (0 if not listed).\n\n
JSON structure:\n
{\n
  "merchant": "store name",\n
  "date": "2026-10-03",\n
  "total_amount": 14.74,\n
  "header_discounts": 0.0,\n
  "items": [\n
    {"name": "Milk", "price": 3.50, "qty": 2, "category": "Drink", "discount": 0.0, "split_type": "Shared", "beneficiary": "ALL"},\n
    {"name": "Rabatt", "price": -1.20, "qty": 1, "category": "General", "discount": 1.20, "split_type": "Shared", "beneficiary": "ALL"}\n
  ]\n
}\n"""



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
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        image_data_url = f"data:image/jpeg;base64,{b64_image}"

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
                                {"type": "image_url", "image_url": {"url": image_data_url}},
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
