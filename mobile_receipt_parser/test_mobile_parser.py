from receipt_parser import MobileReceiptParser


def test_parse_text_basic_receipt():
    text = """MART Grocery
Cappuccino 4.50
Croissant 2.20
Orange Juice 5.80
Subtotal 12.50
Total 12.50"""

    result = MobileReceiptParser().parse_text(text)

    assert result["merchant"] == "MART Grocery"
    assert result["subtotal"] == 12.5
    assert result["total"] == 12.5
    assert len(result["items"]) == 3
    assert result["items"][0]["name"] == "Cappuccino"
