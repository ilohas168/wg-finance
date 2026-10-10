from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from receipt_parser import MobileReceiptParser


def create_demo_receipt(path: Path):
    width, height = 1200, 1800
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    # Default font may not exist on all systems; use a common default.
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 52)
    except Exception:
        font = ImageFont.load_default()

    lines = [
        "MART Grocery",
        "",
        "Cappuccino 4.50",
        "Croissant 2.20",
        "Orange Juice 5.80",
        "",
        "Subtotal 12.50",
        "Total 12.50",
    ]

    y = 160
    for line in lines:
        draw.text((120, y), line, fill="black", font=font)
        y += 90

    img.save(path)


def main():
    base = Path(__file__).resolve().parent
    receipt_path = base / "demo_receipt.png"
    parser = MobileReceiptParser()

    try:
        create_demo_receipt(receipt_path)
        result = parser.parse(receipt_path)
    except Exception:
        sample = """MART Grocery
Cappuccino 4.50
Croissant 2.20
Orange Juice 5.80
Subtotal 12.50
Total 12.50"""
        result = parser.parse_text(sample)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
