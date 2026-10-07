"""Tests for month and all-time receipt report filters."""

import pandas as pd

from app.services.reporting import filter_receipts_for_period


def test_month_filter_includes_only_that_month_in_selected_year():
    receipts = pd.DataFrame(
        {
            "Receipt_ID": ["jan-2025", "jan-2026", "feb-2026"],
            "Date": ["2025-01-15", "2026-01-10", "2026-02-01"],
        }
    )

    filtered = filter_receipts_for_period(receipts, month=1, year=2026)

    assert filtered["Receipt_ID"].tolist() == ["jan-2026"]


def test_all_time_filter_includes_receipts_from_all_years():
    receipts = pd.DataFrame(
        {
            "Receipt_ID": ["jan-2025", "jan-2026"],
            "Date": ["2025-01-15", "2026-01-10"],
        }
    )

    filtered = filter_receipts_for_period(receipts)

    assert filtered["Receipt_ID"].tolist() == ["jan-2025", "jan-2026"]
