"""Receipt date filtering helpers for spend reports."""

import pandas as pd


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

    dates = pd.to_datetime(receipts[date_column], errors="coerce")
    matching_period = (dates.dt.month == month) & (dates.dt.year == year)
    return receipts.loc[matching_period].copy()
