"""Telegram Bot service helpers for WG Finance.

Provides lightweight wrappers around the Telegram Bot API:
  - ``send_message`` — deliver formatted replies to a chat.
  - ``download_photo`` — retrieve raw image bytes from a Telegram file.
  - ``register_webhook`` — tell Telegram where our webhook endpoint lives.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


def _make_url(method: str) -> str:
    """Build the full API URL for a Bot method."""
    token = _get_token()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Set it in your .env file."
        )
    return f"https://api.telegram.org/bot{token}/{method}"


def _get_token() -> str:
    """Retrieve the bot token from environment (lazy, so it works
    regardless of whether dotenv has been loaded)."""
    import os

    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


async def send_message(
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "HTML",
) -> bool:
    """Send a message to a Telegram chat.

    Parameters
    ----------
    chat_id :
        The target chat's numeric ID (from ``message.chat.id``).
    text :
        Message body. Supports HTML formatting when *parse_mode* is ``"HTML"``.
    parse_mode :
        One of ``"HTML"``, ``"MarkdownV2"``, or ``None``.

    Returns
    -------
    bool
        ``True`` if Telegram returned HTTP 200, ``False`` otherwise.
    """
    url = _make_url("sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, json=payload)
        ok = resp.status_code == 200
        if not ok:
            logger.error("send_message failed (%d): %s", resp.status_code, resp.text)
        return ok


async def download_photo(file_id: str) -> bytes:
    """Download the raw image bytes of a Telegram photo.

    Parameters
    ----------
    file_id :
        The ``file_id`` from ``message.photo[-1].file_id`` (highest-res).

    Returns
    -------
    bytes
        The raw JPEG/PNG image data.

    Raises
    ------
    RuntimeError
        If the Telegram API fails to provide a valid *file_path*.
    """
    token = _get_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")

    # Step 1 — resolve file_path via getFile.
    get_url = f"https://api.telegram.org/bot{token}/getFile"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(get_url, params={"file_id": file_id})
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        logger.error("Telegram getFile returned error for file_id=%s: %s", file_id, data)
        raise RuntimeError(f"Telegram getFile failed: {data}")

    file_path = data["result"]["file_path"]
    logger.info("telegram: got file_path=%s for file_id=%s", file_path, file_id)

    # Step 2 — download the actual file bytes.
    file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(file_url)
        if resp.status_code != 200:
            logger.error(
                "telegram: download failed for file_id=%s — HTTP %d", file_id, resp.status_code
            )
            raise RuntimeError(f"Telegram file download returned HTTP {resp.status_code}: {resp.text[:300]}")
        image_data = resp.content
        logger.info("telegram: downloaded %d bytes from Telegram CDN for file_id=%s", len(image_data), file_id)
        return image_data


async def register_webhook(webhook_url: str) -> bool:
    """Register (or update) the bot's webhook endpoint.

    Call this once during deployment so Telegram can deliver updates
    to your ``/webhook`` route.

    Returns ``True`` on success, ``False`` otherwise.
    """
    token = _get_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")

    url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {"url": webhook_url}

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, json=payload)
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        if ok:
            logger.info("Webhook registered at %s", webhook_url)
        else:
            logger.error("setWebhook failed (%d): %s", resp.status_code, resp.text)
        return ok
