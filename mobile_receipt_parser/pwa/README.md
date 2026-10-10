# Receipt Parser PWA

This is a standalone web/PWA version of the mobile receipt parser.
It works in modern browsers on iOS, Android, Windows, and macOS.

## Run

```bash
cd mobile_receipt_parser/pwa
python3 -m http.server 8080
```

Then open `http://localhost:8080`.

## UI structure

The app is organized into clear sections:

1. **Upload** — choose or capture receipt image and run parsing.
2. **Overview** — merchant, currency, item count, subtotal, and total at a glance.
3. **Itemized Receipt** — clean table of parsed items.
4. **Structured JSON + Raw OCR Text** — debug and verification panes.

## Phone testing

1. Connect phone and laptop to the same Wi-Fi.
2. Find your laptop IP (for example `192.168.1.10`).
3. Open `http://192.168.1.10:8080` on the phone browser.

## Install

- **Android (Chrome):** tap the install prompt.
- **iOS (Safari):** Share → Add to Home Screen.
- **Windows (Chrome/Edge):** use the install icon in the address bar (or browser menu → Install app).
- **macOS (Safari/Chrome/Edge):** open the app URL and install/add to Dock from the browser's install/share option.

## Tech

- OCR: Tesseract.js (in-browser, on-device)
- Parsing: lightweight rule-based JavaScript parser
- PWA: manifest + service worker for installability and caching
