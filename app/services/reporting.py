"""Receipt date filtering helpers for spend reports."""

import re

import pandas as pd


_ISO_DATE_PREFIX = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s]|$)")


def parse_receipt_dates(values: pd.Series) -> pd.Series:
    """Parse ISO and Swiss-style day-first dates without swapping month/day."""

    def parse_one(value: object) -> pd.Timestamp:
        if value is None or pd.isna(value):
            return pd.NaT
        text = str(value).strip().lstrip("'")
        if not text:
            return pd.NaT
        if _ISO_DATE_PREFIX.match(text):
            return pd.to_datetime(text, errors="coerce", yearfirst=True)
        return pd.to_datetime(text, errors="coerce", dayfirst=True)

    return pd.to_datetime(values.map(parse_one), errors="coerce")


def filter_receipts_for_period(
    receipts: pd.DataFrame,
    month: int | None = None,
    year: int | None = None,
    date_column: str = "Date",
) -> pd.DataFrame:
    """Return all-time receipts or only the selected calendar month and year."""
    if month is None:
        return receipts.copy()
    if year is None:
        raise ValueError("A year is required when filtering by month")
    if date_column not in receipts.columns:
        return receipts.iloc[0:0].copy()

    dates = parse_receipt_dates(receipts[date_column])
    matching_period = (dates.dt.month == month) & (dates.dt.year == year)
    return receipts.loc[matching_period].copy()
