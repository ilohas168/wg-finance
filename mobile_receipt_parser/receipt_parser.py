from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

from PIL import Image, ImageEnhance, ImageOps

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None


PRICE_RE = re.compile(r"(?:CHF|CHF\s*)?(\d+[.,]\d{2})")
TOTAL_RE = re.compile(r"(?:total|totale|sum|amount)\s*[:=]?\s*(?:CHF\s*)?(\d+[.,]\d{2})", re.I)


@dataclass
class ReceiptItem:
    name: str
    quantity: int
    unit_price: float
    total: float


@dataclass
class ParsedReceipt:
    merchant: str
    items: List[ReceiptItem]
    subtotal: float
    total: float
    currency: str = "CHF"

    def as_dict(self):
        return {
            "merchant": self.merchant,
            "currency": self.currency,
            "items": [asdict(item) for item in self.items],
            "subtotal": self.subtotal,
            "total": self.total,
        }


class MobileReceiptParser:
    """Small, smartphone-friendly receipt parser built around OCR + rule filtering."""

    def __init__(self, tesseract_config: str = "--psm 6"):
        self.tesseract_config = tesseract_config

    def preprocess(self, image_path: str | Path):
        img = Image.open(image_path).convert("L")
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        width, height = img.size
        target_width = max(1200, width)
        target_height = max(1800, height)
        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        return img

    def _ocr(self, image_path: str | Path) -> str:
        if pytesseract is None:
            raise RuntimeError("pytesseract is not installed. Install dependencies from requirements.txt.")
        img = self.preprocess(image_path)
        return pytesseract.image_to_string(img, config=self.tesseract_config)

    def _normalize_line(self, line: str) -> str:
        return " ".join(line.strip().split())

    def _extract_merchant(self, lines: List[str]) -> str:
        for line in lines:
            text = self._normalize_line(line)
            if not text:
                continue
            if re.search(r"\d", text):
                continue
            if text.lower() in {"subtotal", "total", "tax", "balance", "thank you"}:
                continue
            return text
        return "Unknown merchant"

    def parse_text(self, text: str) -> dict:
        lines = [self._normalize_line(line) for line in text.splitlines() if self._normalize_line(line)]
        merchant = self._extract_merchant(lines)
        items: List[ReceiptItem] = []
        subtotal = None
        total = None

        for line in lines:
            item = self._parse_item_line(line)
            if item is not None:
                items.append(ReceiptItem(**item))
                continue

            if re.search(r"subtotal|sum", line, re.I):
                m = re.search(r"(?:CHF\s*)?(\d+[.,]\d{2})", line)
                if m:
                    subtotal = float(m.group(1).replace(",", "."))
                continue

            if re.search(r"total|amount due|balance", line, re.I):
                m = re.search(r"(?:CHF\s*)?(\d+[.,]\d{2})", line)
                if m:
                    total = float(m.group(1).replace(",", "."))
                continue

        if subtotal is None and items:
            subtotal = round(sum(item.total for item in items), 2)
        if total is None and subtotal is not None:
            total = subtotal

        parsed = ParsedReceipt(
            merchant=merchant,
            items=items,
            subtotal=float(subtotal or 0.0),
            total=float(total or subtotal or 0.0),
        )
        return parsed.as_dict()

    def _parse_item_line(self, line: str):
        text = self._normalize_line(line)
        if not text:
            return None

        price_matches = [float(m.replace(",", ".")) for m in PRICE_RE.findall(text)]
        if not price_matches:
            return None

        price = max(price_matches)
        if text.lower().startswith(("subtotal", "total", "tax", "balance", "change")):
            return None

        # Try to separate item name from the trailing price.
        without_price = re.sub(r"(?:CHF\s*)?(\d+[.,]\d{2})$", "", text).strip()
        if not without_price:
            return None

        qty = 1
        qty_match = re.search(r"^(\d+)\s+[xX]\s+", without_price)
        if qty_match:
            qty = int(qty_match.group(1))
            without_price = without_price[qty_match.end():].strip()

        return {
            "name": without_price,
            "quantity": qty,
            "unit_price": round(price / qty, 2) if qty else 1.0,
            "total": round(price, 2),
        }

    def parse(self, image_path: str | Path) -> dict:
        text = self._ocr(image_path)
        return self.parse_text(text)


if __name__ == "__main__":
    parser = MobileReceiptParser()
    result = parser.parse("/tmp/receipt.jpg")
    print(json.dumps(result, indent=2, ensure_ascii=False))
