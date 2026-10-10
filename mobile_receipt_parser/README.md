# Mobile Receipt Parser (parallel prototype)

This folder is a separate prototype built in parallel with the existing Telegram/Google Sheets expense bot. It intentionally does not modify the current app.

## Goal
Create a simple receipt-parser design that can run on a smartphone without depending on a server-side LLM.

## Recommended architecture

For real smartphone use, the best compromise is:

- On-device OCR: Apple Vision / Android ML Kit / Tesseract Lite
- Tiny post-processing model: lightweight classification for item lines, totals, taxes, and merchants
- Optional cloud fallback: only if the on-device result is low-confidence

This keeps the app fast and private while still handling normal receipts.

## Included prototype

This folder includes a compact Python prototype that:

1. loads a receipt image,
2. extracts text with Tesseract OCR,
3. normalizes rows,
4. identifies item lines, subtotal, and total,
5. returns JSON that a mobile app can consume.

The implementation is intentionally lightweight and can be adapted into:

- an Android app (ML Kit + Kotlin)
- an iPhone app (Vision + Swift)
- a web app or PWA running directly on a phone

## Run the demo

```bash
cd mobile_receipt_parser
python3 demo.py
```

This generates a synthetic receipt image and prints a parsed result.

## Why this is smartphone-friendly

- no large server GPU is required
- the logic is small enough to run within a mobile app
- it can be packaged as a quantized model or a tiny rule-based parser
- it only asks for a cloud model when the local result is uncertain

## Files

- `receipt_parser.py` — OCR + parser logic
- `demo.py` — synthetic receipt demo
- `requirements.txt` — minimal Python dependencies
- `pwa/` — installable browser app for iOS, Android, Windows, and macOS with in-browser OCR and parsing

## Web/PWA version (iOS + Android + Windows + macOS)

A separate installable PWA is available in `mobile_receipt_parser/pwa`.
It runs OCR in the browser with Tesseract.js and parses receipt fields locally.

### Run locally

```bash
cd mobile_receipt_parser/pwa
python3 -m http.server 8080
```

Open:

- `http://localhost:8080` (desktop testing)
- `http://<your-lan-ip>:8080` (phone testing on same Wi-Fi)

### Install

- Android (Chrome): open app URL, tap **Install app** (or browser install prompt).
- iOS (Safari): open app URL, tap **Share** → **Add to Home Screen**.
- Windows (Chrome/Edge): use the install icon in the address bar (or browser menu → **Install app**).
- macOS (Safari/Chrome/Edge): install from browser share/install option to run as an app window.

### Notes

- The PWA works best on HTTPS (or localhost) because service workers require a secure context.
- OCR runs on-device in the browser; no receipt image is sent to your server.
