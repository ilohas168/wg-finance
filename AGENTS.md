# WG Finance — Telegram Sharehouse Expense Bot

## Overview
A FastAPI-based Telegram bot that parses receipt images using Groq's Llama 3.2 Vision,
categorizes cost splits among 3 roommates, and updates a shared Google Sheet ledger.
Roommates send photos directly to the bot in Telegram and get automatic balance updates.

## Run Commands
```bash
# Development server (local)
uvicorn app.main:app --reload

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Streamlit dashboard
streamlit run app/dashboard.py --server.port 8501
```

### Registering the Telegram Webhook
Before the bot can receive messages, register your public endpoint:
```bash
python -c "from dotenv import load_dotenv; load_dotenv(); import asyncio; from app.main import startup; asyncio.run(startup())"
# or directly:
curl https://api.telegram.org/bot<TOKEN>/setWebhook?url=TELEGRAM_WEBHOOK_URL
```

## Testing
```bash
pytest
pytest -v
pytest --cov=app --cov-report=term-missing
```

## Environment Variables (`.env`)
| Variable | Description | Example |
|---|---|---|
| `GROQ_API_KEY` | Groq API key for receipt vision parsing | `gsk-...` |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | `123456:AAFxxy...` |
| `TELEGRAM_WEBHOOK_URL` | Public HTTPS endpoint for webhook delivery | `https://.../webhook` |
| `GOOGLE_SHEET_ID` | Google Sheet ID for the ledger | `1aBcDeFgHiJkLmNoPqRsTuVwXyZ...` |
| `GOOGLE_CREDENTIALS_JSON` | Path to Google service account key JSON | `credentials.json` |

## Architecture
```
app/
├── __init__.py
├── main.py              # FastAPI app, /webhook endpoint (Telegram), message routing
├── models.py            # Pydantic schemas (ReceiptData, SplitType, etc.)
├── dashboard.py         # Streamlit web dashboard for viewing balances & history
└── services/
    ├── vision.py        # Groq Llama 3.2 Vision receipt parsing
    ├── ledger.py        # Split engine + Google Sheets integration
    ├── ledger_math.py   # Balance/share/report maths used by the dashboard (cents, tested)
    └── telegram.py      # Telegram Bot API helpers (send, download, webhook reg)
tests/
├── __init__.py
├── test_vision.py       # Pydantic validation for ReceiptData
├── test_ledger.py       # Split math verification
└── test_telegram.py     # Telegram payload parsing & routing tests
```

## Roommate Mapping
The bot maps incoming sender info to roommates:
- Telegram user IDs in `ROOMMATE_MAP` (set in `app/main.py`)
- For WhatsApp legacy support the old PHONE_NUMBER → label pattern existed;
  all new roommates should be added via their Telegram user ID.

## Google Sheet Structure
**Sheet 1 — "Transactions"**
| date | merchant | payer | item_name | price | split_category | a_bears | b_bears | c_bears |

**Sheet 2 — "Summary Ledger"** (manually maintained running balance; bot appends/updates rows)
| date | type | A_balance | B_balance | C_balance |
