"""Tests for month and all-time receipt report filters."""

import pandas as pd

from app.services.reporting import filter_receipts_for_period, parse_receipt_dates


def test_month_filter_includes_only_that_month_in_selected_year():
    receipts = pd.DataFrame(
        {
            "Receipt_ID": ["nov-2025", "nov-2026", "dec-2026"],
            "Date": ["2025-11-15", "03.11.2026", "2026-12-01"],
        }
    )

    filtered = filter_receipts_for_period(receipts, month=11, year=2026)

    assert filtered["Receipt_ID"].tolist() == ["nov-2026"]


def test_all_time_filter_includes_receipts_from_all_years():
    receipts = pd.DataFrame(
        {
            "Receipt_ID": ["jan-2025", "jan-2026"],
            "Date": ["2025-01-15", "2026-01-10"],
        }
    )

    filtered = filter_receipts_for_period(receipts)

    assert filtered["Receipt_ID"].tolist() == ["jan-2025", "jan-2026"]


def test_date_parser_preserves_iso_and_swiss_day_first_months():
    dates = parse_receipt_dates(
        pd.Series(["2026-11-03", "03.11.2026", "03/11/2026"])
    )

    assert dates.dt.strftime("%Y-%m-%d").tolist() == [
        "2026-11-03",
        "2026-11-03",
        "2026-11-03",
    ]
