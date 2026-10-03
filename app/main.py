"""FastAPI application — Telegram webhook entry point for WG Finance.

Exposes a ``POST /webhook`` endpoint that receives Telegram Update payloads,
routes photo messages (receipts) and text commands to their handlers, and
sends back formatted replies via the Telegram Bot API.
"""

from __future__ import annotations

import json as _json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from app.models import (
    LedgerEntry,
    ReceiptData,
    RoommateBalance,
    SharehouseLedgerState,
    SplitType,
)

load_dotenv()  # load .env so TELEGRAM_BOT_TOKEN is available for telegram.py

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Pydantic request models (Telegram Update payload)                  #
# ------------------------------------------------------------------ #


class PhotoSize(BaseModel):
    """A single photo size entry from a Telegram message."""

    file_id: str
    file_unique_id: Optional[str] = None
    width: int
    height: int


class SenderInfo(BaseModel):
    """Telegram user who sent the message."""

    id: int
    is_bot: bool = False
    first_name: Optional[str] = None
    username: Optional[str] = None


class Message(BaseModel):
    """Incoming Telegram message."""

    message_id: int
    from_user: Optional[SenderInfo] = None
    chat: Optional[SenderInfo] = None  # can be None or omitted for forwarded messages
    date: int
    photo: Optional[List[PhotoSize]] = None
    text: Optional[str] = None

    model_config = {
        "extra": "ignore",  # Telegram may send extra fields we don't track
    }


class Update(BaseModel):
    """Full Telegram Update payload."""

    update_id: int
    message: Optional[Message] = None  # can be None (e.g. deleted messages)


# ------------------------------------------------------------------ #
# App setup                                                            #
# ------------------------------------------------------------------ #

# Roommate lookup by Telegram user ID — matches roommates who have set up the bot.
ROOMMATE_MAP: Dict[int, str] = {
    1555000001: "Person A",
    1555000002: "Person B",
    1555000003: "Person C",
}

_DEFAULT_PAYER = "Person A"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager — registers Telegram webhook on startup."""
    logger.info("Starting WG Finance (Telegram) server …")

    # Validate critical environment variables.
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key or "your-key" in groq_key.lower():
        logger.warning(
            "GROQ_API_KEY is missing, empty, or still at its default placeholder value. "
            "Receipt parsing will fail until a valid Groq API key is set in .env."
        )

    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning(
            "TELEGRAM_WEBHOOK_URL is not set — incoming messages will not reach this server."
        )
    else:
        try:
            from app.services.telegram import register_webhook

            ok = await register_webhook(webhook_url)
            if ok:
                logger.info("Telegram webhook registered at %s", webhook_url)
            else:
                logger.error("Failed to register Telegram webhook.")
        except Exception:
            logger.exception("Webhook registration error")

    yield  # — server is running while we wait here —

    logger.info("Shutting down WG Finance (Telegram) …")


app = FastAPI(title="WG Finance (Telegram)", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ #
# Webhook handler                                                      #
# ------------------------------------------------------------------ #

@app.post("/webhook")
async def webhook(update: Update):
    """Telegram webhook — handles photos (receipts) and text commands."""

    update_id = update.update_id
    raw = _json.dumps(
        {k: v for k, v in vars(update).items() if v is not None}, default=str
    )
    logger.info("Received Telegram Update #%d: %s", update_id, raw)

    msg = update.message
    if not msg or not msg.chat:
        logger.warning("Update #%d — no message or chat; skipping.", update_id)
        return {"status": "ok"}

    chat_id = msg.chat.id

    # Log sender info so we can diagnose unmapped users.
    from_user = msg.from_user  # type: ignore[union-attr]
    sender_id = from_user.id if from_user else chat_id
    sender_name = from_user.first_name if from_user else "unknown"
    logger.info(
        "Update #%d — sender: id=%s name=%s", update_id, sender_id, sender_name
    )

    # Photo message → receipt parsing.
    if msg.photo:
        return await _handle_photo(chat_id, msg)

    # Text message → command handler.
    if msg.text:
        return await _handle_text(chat_id, msg.text)

    # Unsupported content type (video, audio, etc.) — log and acknowledge.
    logger.info("Update #%d — unsupported content; acknowledging silently.", update_id)
    return {"status": "ok"}


# ------------------------------------------------------------------ #
# Message handlers                                                     #
# ------------------------------------------------------------------ #

async def _handle_photo(chat_id: int, msg: Message) -> Dict[str, Any]:
    """Process an incoming receipt photo from Telegram."""
    logger.info("Processing photo for chat %d …", chat_id)

    try:
        from app.services.telegram import download_photo

        # Pick the highest-resolution photo (Telegram sends sizes sorted ascending).
        file_id = msg.photo[-1].file_id  # type: ignore[union-attr]
        image_bytes = await download_photo(file_id)
        logger.info(
            "Telegram: downloaded photo for chat %d — %d bytes (%.1f KB)",
            chat_id, len(image_bytes), len(image_bytes) / 1024,
        )
    except Exception as exc:
        logger.exception("Downloading Telegram photo failed for chat %d", chat_id)
        await _reply(
            chat_id,
            f"Error downloading photo: {str(exc)}",
        )
        return {"status": "ok"}

    # Parse the receipt via Groq Vision.
    try:
        from app.services.vision import parse_receipt_image

        receipt = await parse_receipt_image(image_bytes)
    except Exception as exc:
        logger.exception("Receipt parsing failed for chat %d", chat_id)
        await _reply(
            chat_id,
            f"Error parsing image: {str(exc)}",
        )
        return {"status": "ok"}

    # Determine payer (from Telegram user ID or caption).
    from_user = msg.from_user  # type: ignore[union-attr]
    sender_id = from_user.id if from_user else chat_id
    payer = _lookup_roommate(sender_id)
    if payer is None:
        logger.warning(
            "Sender %s (ID=%d) not in ROOMMATE_MAP — defaulting to %s. "
            "Ask this person to register with the bot.",
            from_user.first_name if from_user else "unknown",
            sender_id,
            _DEFAULT_PAYER,
        )
    payer = payer or _DEFAULT_PAYER

    # Build and send the reply.
    reply = _format_receipt_reply(receipt, payer)
    await _reply(chat_id, reply)
    logger.info("Photo reply sent to chat %d", chat_id)
    return {"status": "ok"}


async def _handle_text(chat_id: int, text: str) -> Dict[str, Any]:
    """Process an incoming text command from Telegram."""
    sender_name = f"@{text}"  # placeholder until we have the full Update context
    logger.info("Processing text '%s' for chat %d", text[:80], chat_id)

    cmd = text.strip().lower()

    if cmd in ("/balance", "/balances", "/status"):
        reply = await _get_balances_reply()
        await _reply(chat_id, reply)
        logger.info("Balance reply sent to chat %d", chat_id)

    elif cmd in ("/help", "/commands"):
        help_text = (
            "Commands:\n"
            "<b>/balance</b> — Check your sharehouse balances\n"
            "<b>/status</b> — Same as /balance\n"
            "<b>/help</b> — Show this help message\n\n"
            "Send a photo of any receipt to log it automatically!"
        )
        await _reply(chat_id, help_text)

    else:
        await _reply(chat_id, "Unknown command. Type <b>/help</b> for options.")

    return {"status": "ok"}


# ------------------------------------------------------------------ #
# Reply helper                                                         #
# ------------------------------------------------------------------ #

async def _reply(chat_id: int, text: str):
    """Send an HTML-formatted message back to a Telegram chat."""
    from app.services.telegram import send_message

    try:
        await send_message(chat_id, text, parse_mode="HTML")
        logger.info("Reply sent to chat %d", chat_id)
    except Exception:
        logger.exception("Failed to send reply to chat %d", chat_id)


# ------------------------------------------------------------------ #
# Formatting helpers                                                   #
# ------------------------------------------------------------------ #

def _format_receipt_reply(receipt: ReceiptData, payer: str) -> str:
    """Format a receipt parsing result into Telegram-friendly HTML text."""
    lines = [
        f"<b>Receipt from {receipt.merchant}</b>",
        f"Date: {receipt.date}",
        f"Paid by: <b>{payer}</b>",
        "",
        "<b>Items:</b>",
    ]

    for i, item in enumerate(receipt.items, 1):
        split_label = _split_to_label(item.assigned_split)
        lines.append(
            f"  {i}. <b>{item.item_name}</b> — ${item.price:.2f} [{split_label}]"
        )

    lines += [
        "",
        f"Tax: ${receipt.tax_total:.2f}",
        f"<b>Grand Total: ${receipt.grand_total:.2f}</b>",
        "",
        "Balances will update automatically.",
    ]

    return "\n".join(lines)


def _split_to_label(split: SplitType) -> str:
    """Convert a SplitType enum to a human-readable label."""
    labels = {
        SplitType.SPLIT_3: "Split 3 ways",
        SplitType.ONLY_A: "A only",
        SplitType.ONLY_B: "B only",
        SplitType.ONLY_C: "C only",
        SplitType.SPLIT_AB: "A & B split",
        SplitType.SPLIT_BC: "B & C split",
        SplitType.SPLIT_AC: "A & C split",
    }
    return labels.get(split, str(split))


async def _get_balances_reply() -> str:
    """Query current ledger state and return a formatted reply string."""
    try:
        from app.services.ledger import compute_current_balances

        balances = compute_current_balances()
        advice = _settlement_advice(balances)

        lines = ["<b>Sharehouse Balances</b>", ""]
        for name in ["Person A", "Person B", "Person C"]:
            val = balances.get(name, 0.0)
            if val > 0:
                status = f"is owed <b>${val:,.2f}</b>"
            elif val < 0:
                status = f"owes <b>${abs(val):,.2f}</b>"
            else:
                status = "<i>$0.00 (settled)</i>"
            lines.append(f"  {name} — {status}")

        lines += ["", advice]
        return "\n".join(lines)
    except Exception:
        # Fallback when Google Sheets is unreachable.
        return (
            "<b>Sharehouse Balances</b>\n\n"
            "Could not fetch live data from the ledger.<br>"
            "(Tip: make sure GOOGLE_SHEET_ID and credentials are configured.)"
        )


def _settlement_advice(balances: dict) -> str:
    """Return a settlement sentence or 'everyone is settled' message."""
    sorted_pos = sorted(
        [(n, b) for n, b in balances.items() if b > 0.01],
        key=lambda x: -x[1],
    )
    sorted_neg = sorted(
        [(n, b) for n, b in balances.items() if b < -0.01],
        key=lambda x: x[1],
    )

    if not sorted_pos and not sorted_neg:
        return "Everyone is settled!"

    steps = []
    pos_ptr = neg_ptr = 0
    while pos_ptr < len(sorted_pos) and neg_ptr < len(sorted_neg):
        giver_name, giver_bal = sorted_neg[neg_ptr]
        receiver_name, receiver_bal = sorted_pos[pos_ptr]
        amount = min(abs(giver_bal), receiver_bal)

        if amount > 0.01:
            steps.append(f"{giver_name} pays {receiver_name} ${amount:.2f}")

        giver_bal += amount
        receiver_bal -= amount
        if abs(giver_bal) < 0.01:
            neg_ptr += 1
        else:
            sorted_neg[neg_ptr] = (giver_name, round(giver_bal, 2))
        if abs(receiver_bal) < 0.01:
            pos_ptr += 1
        else:
            sorted_pos[pos_ptr] = (receiver_name, round(receiver_bal, 2))

    return "To settle up: " + "; ".join(steps) + "."


# ------------------------------------------------------------------ #
# Utility                                                              #
# ------------------------------------------------------------------ #

def _lookup_roommate(user_id: int) -> Optional[str]:
    """Look up roommate label from Telegram user ID using ROOMMATE_MAP."""
    return ROOMMATE_MAP.get(user_id)


# ------------------------------------------------------------------ #
# Health check                                                         #
# ------------------------------------------------------------------ #

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "wg-finance-telegram"}
