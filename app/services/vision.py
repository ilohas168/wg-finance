"""Vision parsing module — Groq Llama 3.2 Vision receipt extraction.

Uses ``groq.Groq`` to send receipt images to a vision model with JSON structured
output for typed ``ReceiptData`` results.  Includes multi-model fallback so that if
Groq deprecates or renames a model the parser automatically tries the next one.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from typing import Optional

import groq
from groq import NotFoundError  # Groq-specific 404 exception
from PIL import Image
from pydantic import ValidationError

from app.models import ReceiptData

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Candidate vision models (priority order)                                    #
# --------------------------------------------------------------------------- #
# If the primary model returns a 404 / ``model_not_found``, each fallback is     #
# tried automatically. Update this list when Groq adds or deprecates models.   #
_VISION_MODEL_CANDIDATES: list[str] = [
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3.8-27b",
]

_DEFAULT_VISION_MODEL: str = _VISION_MODEL_CANDIDATES[0]  # first in priority

# --------------------------------------------------------------------------- #
# System prompt (enforces valid JSON matching ReceiptData schema)              #
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT: str = (
    "You are a receipt parsing assistant for a 3-person sharehouse.\n"
    "Extract every line-item from this receipt image into structured JSON.\n\n"
    "**You MUST output valid, parseable JSON. Do NOT include markdown fences,**\n"
    "**explanations, or any text outside the JSON object.**\n\n"
    "Each item must have:\n"
    '  - "item_name": short descriptive name (<=60 chars)\n'
    '  - "price": number — positive for regular charges; negative for discounts,\n'
    '                deductions, and vouchers (e.g. -3.60 for Rabatt/Pfand)\n'
    '  - "assigned_split": one of SPLIT_3, ONLY_A, ONLY_B, ONLY_C,\n'
    '                      SPLIT_AB, SPLIT_BC, or SPLIT_AC\n\n'
    "Split rules:\n"
    "  - SPLIT_3 : shared groceries, cleaning supplies (default)\n"
    "  - ONLY_A / ONLY_B / ONLY_C : single-person items\n"
    "  - SPLIT_AB / SPLIT_BC / SPLIT_AC : two-person shares\n\n"
    "Defaults:\n"
    "  - Food, drinks, toiletries -> SPLIT_3 unless clearly one person's.\n"
    "  - Alcohol or branded snack packs -> assign to likely user or ONLY_X.\n\n"
    "Rules:\n"
    '  - date must be YYYY-MM-DD (guess from visual cues if absent).\n'
    '  - tax_total is the total tax line (0 if not listed).\n'
    "  - Sum of all item prices + tax_total MUST equal grand_total.\n\n"
    "JSON structure:\n"
    "{\n"
    '  "merchant": "store name",\n'
    '  "date": "2026-10-03",\n'
    '  "items": [\n'
    '    {"item_name": "Milk", "price": 3.50, "assigned_split": "SPLIT_3"},\n'
    '    ...\n'
    "  ],\n"
    '  "tax_total": 0.49,\n'
    '  "grand_total": 14.74\n'
    "}\n"
)


def _get_client() -> groq.Groq:
    """Create a Groq client from the GROQ_API_KEY environment variable."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Set it in your .env file."
        )
    return groq.Groq(api_key=api_key)


# --------------------------------------------------------------------------- #
# Helpers for extracting clean JSON from LLM responses                        #
# --------------------------------------------------------------------------- #

def _extract_json(raw: str) -> str:
    """Strip markdown code fences and surrounding whitespace to get raw JSON.

    Handles both `````json ... ````` and bare ````` ... ```` fences, including
    cases where the content has leading/trailing blank lines inside the fence.
    """
    stripped = raw.strip()

    # Match fenced blocks (with optional language hint).
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fence_match:
        result = fence_match.group(1).strip()
        logger.debug("Stripped markdown fences — extracted JSON length: %d", len(result))
        return result

    # No fences found — return raw string as-is (assumed bare JSON)
    return stripped


def _safe_validate_json(raw: str) -> ReceiptData:
    """Attempt Pydantic validation on a raw JSON string, returning the model.

    Raises ``ValueError`` with detailed information if parsing or validation
    fails at any step.
    """
    # Try direct Pydantic parsing first (faster than regex for clean output).
    try:
        return ReceiptData.model_validate_json(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Direct JSON parse failed — trying fence extraction: %s", exc)

    # Strip markdown fences and retry.
    cleaned = _extract_json(raw)
    try:
        return ReceiptData.model_validate_json(cleaned)
    except (json.JSONDecodeError, ValidationError) as exc2:
        logger.error(
            "ReceiptData validation failed after fence stripping.\n"
            "Original response (%d chars):\n%s\n---\nCleaned (%d chars):\n%s",
            len(raw), raw[:500],
            len(cleaned), cleaned[:500],
        )
        raise ValueError(
            f"Groq returned invalid receipt JSON (see logs). "
            f"Original snippet: {raw[:300]}"
        ) from exc2


# --------------------------------------------------------------------------- #
# Helpers to detect model-not-found errors                                    #
# --------------------------------------------------------------------------- #

def _is_model_not_found(exc: Exception) -> bool:
    """Return True if *exc* indicates the requested Groq model was not found (404)."""
    # groq.NotFoundError is the cleanest check.
    if isinstance(exc, NotFoundError):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 404:
        return True
    msg = str(exc).lower()
    if "model_not_found" in msg or "model not found" in msg or "not found" in msg:
        return True
    return False


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

async def parse_receipt_image(
    image_bytes: bytes,
    *,
    model: str | None = None,
) -> ReceiptData:
    """Parse a receipt image via Groq with automatic model fallback.

    Parameters
    ----------
    image_bytes :
        Raw JPEG / PNG image data.
    model :
        Groq model name (default uses the primary vision model from
        ``_VISION_MODEL_CANDIDATES``).  If the model returns a 404, each
        remaining candidate is tried automatically.

    Returns
    -------
    ReceiptData
        Validated Pydantic model containing the extracted receipt.

    Raises
    ------
    ValueError
        If all candidate models fail (either 404 or another error).
    """
    candidates = _build_model_candidates(model)
    last_exc: Optional[Exception] = None
    max_retries_per_model = 3
    retry_base_delay = 2  # seconds

    for tried_model in candidates:
        logger.info("vision: trying model '%s'", tried_model)
        client = _get_client()
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        image_data_url = f"data:image/jpeg;base64,{b64_image}"

        for attempt in range(1, max_retries_per_model + 1):
            try:
                response = client.chat.completions.create(
                    model=tried_model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Parse this receipt image. Return structured JSON.",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_data_url},
                                },
                            ],
                        },
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=4096,
                )

                content = response.choices[0].message.content
                if content is None:
                    logger.error("vision: Groq returned null content")
                    raise ValueError(
                        "Groq returned no content for the receipt image."
                    )

                # Parse and validate.
                receipt = _safe_validate_json(content)
                receipt.validate_totals()  # enforce internal consistency
                logger.info(
                    "vision parsed with model '%s': merchant=%s items=%d total=%.2f",
                    tried_model,
                    receipt.merchant,
                    len(receipt.items),
                    receipt.grand_total,
                )
                return receipt

            except NotFoundError as exc:
                # 404 / model_not_found — abandon this model immediately.
                logger.warning(
                    "vision: model '%s' not found (404) — trying next candidate",
                    tried_model,
                )
                last_exc = exc
                break  # break inner loop; outer loop picks next candidate

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

                if is_transient and attempt < max_retries_per_model:
                    delay = retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "vision: transient Groq error on '%s' (attempt %d/%d): %s — retrying in %.0fs",
                        tried_model, attempt, max_retries_per_model, str(exc)[:100], delay,
                    )
                    time.sleep(delay)
                else:
                    # Non-retryable or exhausted retries on this model.
                    if _is_model_not_found(exc):
                        logger.warning(
                            "vision: model '%s' failed (model not found?) — trying next candidate",
                            tried_model,
                        )
                        last_exc = exc
                        break  # abandon this model
                    raise  # re-raise non-retryable errors immediately

    # All candidates exhausted.
    logger.exception(
        "vision: all Groq model candidates failed after retries (image=%d bytes)",
        len(image_bytes),
    )
    raise ValueError(str(last_exc)) from last_exc


def _build_model_candidates(model: str | None) -> list[str]:
    """Return the ordered list of model names to try.

    If *model* is given, it is prepended (deduplicated).  Otherwise the default
    candidate list is returned as-is.
    """
    if model:
        return [model] + [m for m in _VISION_MODEL_CANDIDATES if m != model]
    return list(_VISION_MODEL_CANDIDATES)


async def parse_receipt_from_url(
    image_url: str,
    *,
    model: str | None = None,
) -> ReceiptData:
    """Fetch an image from a URL (e.g. Telegram media URL) and parse it.

    Parameters
    ----------
    image_url :
        Publicly accessible URL of the receipt image.
    model :
        Model name to use for parsing (or None for auto-candidates).

    Returns
    -------
    ReceiptData
    """
    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(image_url)
        resp.raise_for_status()
        return await parse_receipt_image(resp.content, model=model)
