"""Tests for correcting a receipt date without changing its accounting data."""

from app.services import ledger


class FakeWorksheet:
    def __init__(self):
        self.updated_cells = []

    def get_all_values(self):
        return [
            [
                "Receipt_ID",
                "Date",
                "Store",
                "Paid_By",
                "Header_Discounts",
                "Grand_Total",
                "Shared_Total",
                "Notes",
            ],
            ["REC-1", "2026-12-09", "Store", "Shin", "0", "10", "10", ""],
        ]

    def update_cell(self, row, column, value):
        self.updated_cells.append((row, column, value))


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def worksheet(self, name):
        assert name == "Receipts"
        return self._worksheet


def test_update_receipt_date_changes_only_the_date_cell(monkeypatch):
    worksheet = FakeWorksheet()
    monkeypatch.setattr(ledger, "_open_sheet", lambda: FakeSpreadsheet(worksheet))

    ledger.update_receipt_date("REC-1", "2026-11-09")

    assert worksheet.updated_cells == [(2, 2, "2026-11-09")]
