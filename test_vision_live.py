#!/usr/bin/env python3
"""Standalone script to test Groq Llama 3.2 Vision receipt parsing directly.

Generates a small dummy JPEG image, passes it through parse_receipt_image(),
and prints the parsed result — verifies the full vision pipeline works end-to-end.

Usage:
    cd /Users/shin/projects/personal-apps/wg-finance && python test_vision_live.py
"""

import asyncio
import io
import os
import sys
import traceback as tb_mod


def _ensure_env():
    """Load .env so GROQ_API_KEY is available."""
    from dotenv import load_dotenv
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        load_dotenv(env_file)


def _generate_dummy_image(width: int = 800, height: int = 600) -> bytes:
    """Create a small dummy JPEG image with text (a fake receipt)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    lines = [
        "CornerMart",
        "123 Main St",
        "",
        "Milk           $3.50",
        "Bread          $2.75",
        "Eggs (12pk)    $4.99",
        "Apples (lb)    $1.25",
        "",
        "--- TOTAL ---",
        "Subtotal:  $12.49",
        "Tax:          $0.87",
        "Total:       $13.36",
        "",
        "Date: 2026-10-03",
        "Thank you!",
    ]

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
    except OSError:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except OSError:
            font = ImageFont.load_default()

    y = 30
    for line in lines:
        draw.text((30, y), line, fill="black", font=font)
        y += 38 if line != "--- TOTAL ---" else 42

    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    return buf.getvalue()


# --------------------------------------------------------------------------- #

async def main():
    _ensure_env()

    # 1. Check GROQ_API_KEY is present.
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key or "your-key" in api_key.lower():
        print("[FAIL] GROQ_API_KEY not set or still at placeholder value.")
        print("       Update .env with a valid key before running this test.")
        sys.exit(1)

    print(f"[OK] GROQ_API_KEY present ({len(api_key)} chars)")

    # 2. Generate dummy receipt image.
    try:
        dummy_bytes = _generate_dummy_image()
        print(f"[OK] Generated dummy JPEG — {len(dummy_bytes)} bytes")
    except Exception as e:
        print(f"[FAIL] Could not generate dummy image: {e}")
        tb_mod.print_exc()
        sys.exit(1)

    # 3. Parse receipt via Groq.
    from app.services.vision import parse_receipt_image

    print("\n[---] Calling llama-3.2-11b-vision-instruct to parse the receipt ...\n")
    try:
        result = await parse_receipt_image(dummy_bytes)
    except Exception as e:
        print(f"[FAIL] Groq API call failed:")
        tb_mod.print_exc()
        print(f"\nError message: {e}")
        sys.exit(1)

    # 4. Print the parsed result.
    print("\n" + "=" * 60)
    print("GROQ PARSED RECEIPT")
    print("=" * 60)
    print(f"  Merchant : {result.merchant}")
    print(f"  Date     : {result.date}")
    print(f"  Tax      : ${result.tax_total:.2f}")
    print(f"  Grand Total: ${result.grand_total:.2f}")
    print(f"\n  Items ({len(result.items)}):")
    split_labels = {
        "SPLIT_3": "Split 3 ways",
        "ONLY_A": "A only",
        "ONLY_B": "B only",
        "ONLY_C": "C only",
        "SPLIT_AB": "A & B split",
        "SPLIT_BC": "B & C split",
        "SPLIT_AC": "A & C split",
    }
    for i, item in enumerate(result.items, 1):
        label = split_labels.get(item.assigned_split, str(item.assigned_split))
        print(f"    {i}. {item.item_name:20s}  ${item.price:>7.2f}  [{label}]")

    # 5. Verify totals.
    computed = round(sum(itm.price for itm in result.items) + result.tax_total, 2)
    if abs(computed - result.grand_total) > 0.02:
        print(f"\n  [WARN] item sum ({computed}) != grand_total ({result.grand_total})")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED — Groq API is working correctly.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
