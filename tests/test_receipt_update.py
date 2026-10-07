"""Tests for replacing a receipt and its stored line items."""

from app.services import ledger


class FakeWorksheet:
    def __init__(self, sheet_id, values, spreadsheet):
        self.id = sheet_id
        self.values = values
        self.spreadsheet = spreadsheet

    def get_all_values(self):
        return self.values


class FakeSpreadsheet:
    def __init__(self):
        self.batch_updates = []

    def batch_update(self, body):
        self.batch_updates.append(body)


def test_update_receipt_deletes_matching_rows_with_sheets_requests(monkeypatch):
    spreadsheet = FakeSpreadsheet()
    receipts = FakeWorksheet(
        11,
        [
            ["Receipt_ID", "Date"],
            ["REC-1", "2026-09-12"],
            ["REC-2", "2026-09-13"],
            ["REC-1", "2026-09-12"],
        ],
        spreadsheet,
    )
    items = FakeWorksheet(
        22,
        [
            ["Receipt_ID", "Product_Name"],
            ["REC-1", "Old item 1"],
            ["REC-2", "Other receipt item"],
            ["REC-1", "Old item 2"],
        ],
        spreadsheet,
    )
    saved = []
    monkeypatch.setattr(
        ledger,
        "_ensure_all_worksheets",
        lambda: {"Receipts": receipts, "Receipt_Items": items},
    )
    monkeypatch.setattr(
        ledger,
        "save_receipt",
        lambda receipt_data, item_rows: saved.append((receipt_data, item_rows)),
    )

    receipt_data = {"Receipt_ID": "REC-1"}
    item_rows = [{"Product_Name": "Updated item"}]
    ledger.update_receipt("REC-1", receipt_data, item_rows)

    assert spreadsheet.batch_updates == [
        {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 11,
                            "dimension": "ROWS",
                            "startIndex": 3,
                            "endIndex": 4,
                        }
                    }
                },
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 11,
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 2,
                        }
                    }
                },
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 22,
                            "dimension": "ROWS",
                            "startIndex": 3,
                            "endIndex": 4,
                        }
                    }
                },
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 22,
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 2,
                        }
                    }
                },
            ]
        }
    ]
    assert saved == [(receipt_data, item_rows)]


def test_update_receipt_allows_receipt_without_existing_items(monkeypatch):
    spreadsheet = FakeSpreadsheet()
    receipts = FakeWorksheet(11, [["Receipt_ID"], ["REC-1"]], spreadsheet)
    items = FakeWorksheet(22, [["Receipt_ID", "Product_Name"]], spreadsheet)
    saved = []
    monkeypatch.setattr(
        ledger,
        "_ensure_all_worksheets",
        lambda: {"Receipts": receipts, "Receipt_Items": items},
    )
    monkeypatch.setattr(
        ledger,
        "save_receipt",
        lambda receipt_data, item_rows: saved.append((receipt_data, item_rows)),
    )

    ledger.update_receipt("REC-1", {"Receipt_ID": "REC-1"}, [])

    assert spreadsheet.batch_updates == [
        {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 11,
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 2,
                        }
                    }
                }
            ]
        }
    ]
    assert saved == [({"Receipt_ID": "REC-1"}, [])]
