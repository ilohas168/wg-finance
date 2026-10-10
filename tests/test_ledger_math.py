"""Hand-checked tests for the dashboard's balance and spending maths."""

import random

import pandas as pd
import pytest

from app.services.ledger_math import (
    category_totals_cents,
    compute_balances,
    money_to_cents,
    receipt_breakdowns,
    settle_up,
    summarise_period,
)

NO_SETTLEMENTS = pd.DataFrame(columns=["From_Roommate", "To_Roommate", "Amount"])


def _receipt(rid, payer, total, date="2026-10-01", discount="0", store="Migros"):
    return {
        "Receipt_ID": rid,
        "Date": date,
        "Store": store,
        "Paid_By": payer,
        "Header_Discounts": discount,
        "Grand_Total": total,
    }


def _item(rid, line_total, beneficiary="ALL", category="Food"):
    return {
        "Receipt_ID": rid,
        "Line_Total": line_total,
        "Beneficiary": beneficiary,
        "Category": category,
    }


def _ledger(receipts, items):
    return receipt_breakdowns(pd.DataFrame(receipts), pd.DataFrame(items))


@pytest.mark.parametrize(
    "raw, cents",
    [
        ("12.50", 1250),
        ("12,50", 1250),
        ("1'234.50", 123450),
        ("1’234.50", 123450),
        ("1,234.50", 123450),
        ("CHF 7.05", 705),
        ("-2.00", -200),
        (3.335, 334),
        ("", 0),
        ("n/a", 0),
    ],
)
def test_money_to_cents_reads_sheet_formats(raw, cents):
    assert money_to_cents(raw) == cents


def test_shared_receipt_credits_payer_and_splits_three_ways():
    ledger = _ledger([_receipt("R1", "Shin", "30.00")], [_item("R1", "30.00")])

    balances = compute_balances(ledger, NO_SETTLEMENTS)

    # Shin paid 30 and used 10; Fabian and Pierre each used 10.
    assert balances == {"A": 2000, "B": -1000, "C": -1000}
    assert settle_up(balances) == [
        {"From": "Fabian", "To": "Shin", "Amount (CHF)": 10.0},
        {"From": "Pierre", "To": "Shin", "Amount (CHF)": 10.0},
    ]


def test_pair_and_private_items_only_charge_their_beneficiaries():
    ledger = _ledger(
        [_receipt("R1", "Fabian", "35.00")],
        [_item("R1", "20.00", "AB"), _item("R1", "9.00", "ALL"), _item("R1", "6.00", "C")],
    )

    summary = summarise_period(ledger)

    # Shin 10 + 3, Fabian 10 + 3, Pierre 3 + 6.
    assert summary.share == {"A": 1300, "B": 1300, "C": 900}
    assert summary.paid == {"A": 0, "B": 3500, "C": 0}
    assert compute_balances(ledger, NO_SETTLEMENTS) == {"A": -1300, "B": 2200, "C": -900}


def test_display_labels_are_accepted_as_beneficiaries():
    ledger = _ledger(
        [_receipt("R1", "Pierre", "12.00")],
        [_item("R1", "12.00", "Shin & Pierre")],
    )
    assert summarise_period(ledger).share == {"A": 600, "B": 0, "C": 600}


def test_header_discount_is_shared_by_what_each_person_bought():
    ledger = _ledger(
        [_receipt("R1", "Shin", "54.00", discount="6.00")],
        [_item("R1", "40.00", "A"), _item("R1", "20.00", "ALL")],
    )

    summary = summarise_period(ledger)

    # Before the discount: Shin 46.67, Fabian 6.67, Pierre 6.67 of 60.
    # Each pays 90% (54/60) of that: 42.00, 6.00, 6.00.
    assert summary.share == {"A": 4200, "B": 600, "C": 600}
    assert summary.total_share == summary.total_paid == 5400
    assert summary.mismatched_ids == []


def test_negative_discount_lines_reduce_the_split():
    ledger = _ledger(
        [_receipt("R1", "Shin", "10.00")],
        [_item("R1", "12.00", "ALL"), _item("R1", "-2.00", "ALL")],
    )
    assert sum(summarise_period(ledger).share.values()) == 1000


def test_settlements_move_both_balances():
    ledger = _ledger([_receipt("R1", "Shin", "30.00")], [_item("R1", "30.00")])
    settlements = pd.DataFrame(
        [
            {"From_Roommate": "Fabian", "To_Roommate": "Shin", "Amount": "10"},
            {"From_Roommate": "Pierre", "To_Roommate": "Shin", "Amount": "4.50"},
        ]
    )

    assert compute_balances(ledger, settlements) == {"A": 550, "B": 0, "C": -550}


def test_rounding_happens_once_per_period_not_per_receipt():
    receipts = [_receipt(f"R{i}", "Shin", "10.01") for i in range(3)]
    items = [_item(f"R{i}", "10.01") for i in range(3)]

    summary = summarise_period(_ledger(receipts, items))

    # 30.03 / 3 = 10.01 exactly; per-receipt rounding would give 10.02/10.01/10.00.
    assert summary.share == {"A": 1001, "B": 1001, "C": 1001}


def test_mismatched_receipt_is_flagged_and_balances_show_the_gap():
    ledger = _ledger([_receipt("R1", "Shin", "31.00")], [_item("R1", "30.00")])

    summary = summarise_period(ledger)
    balances = compute_balances(ledger, NO_SETTLEMENTS)

    assert summary.mismatched_ids == ["R1"]
    assert sum(balances.values()) == 100


def test_blank_grand_total_falls_back_to_item_total():
    ledger = _ledger([_receipt("R1", "Fabian", "")], [_item("R1", "9.00")])
    assert summarise_period(ledger).paid["B"] == 900


def test_unknown_payer_is_excluded_from_balances_but_counted_in_total():
    ledger = _ledger(
        [_receipt("R1", "Shin", "30.00"), _receipt("R2", "Someone", "99.00")],
        [_item("R1", "30.00"), _item("R2", "99.00")],
    )

    summary = summarise_period(ledger)

    assert summary.unknown_payer_ids == ["R2"]
    assert summary.total_paid == 12900
    assert compute_balances(ledger, NO_SETTLEMENTS) == {"A": 2000, "B": -1000, "C": -1000}


def test_payer_codes_are_accepted():
    ledger = _ledger([_receipt("R1", "c", "3.00")], [_item("R1", "3.00")])
    assert ledger[0].payer == "Pierre"


def test_category_totals_include_header_discounts_and_match_shares():
    ledger = _ledger(
        [_receipt("R1", "Shin", "54.00", discount="6.00")],
        [_item("R1", "40.00", "A", "Household"), _item("R1", "20.00", "ALL", "Food")],
    )
    items = pd.DataFrame(
        [_item("R1", "40.00", "A", "Household"), _item("R1", "20.00", "ALL", "Food")]
    )

    totals = category_totals_cents(ledger, items)

    assert totals == {"Household": 3600, "Food": 1800}
    assert sum(totals.values()) == summarise_period(ledger).total_share


def test_receipts_without_items_are_kept_but_allocate_nothing():
    ledger = _ledger([_receipt("R1", "Shin", "5.00")], [_item("R2", "1.00")])
    summary = summarise_period(ledger)
    assert summary.total_share == 0
    assert summary.mismatched_ids == ["R1"]


def test_balances_match_a_naive_float_reimplementation():
    rng = random.Random(7)
    codes = ["ALL", "A", "B", "C", "AB", "BC", "AC"]
    names = {"A": "Shin", "B": "Fabian", "C": "Pierre"}
    receipts, items = [], []
    expected = {"A": 0.0, "B": 0.0, "C": 0.0}
    for i in range(200):
        rid = f"R{i}"
        payer = rng.choice("ABC")
        lines = [round(rng.uniform(-3, 40), 2) for _ in range(rng.randint(1, 8))]
        item_total = sum(lines)
        discount = round(rng.uniform(0, 3), 2) if rng.random() < 0.3 else 0.0
        receipts.append(_receipt(rid, names[payer], f"{item_total - discount:.2f}", discount=f"{discount}"))
        expected[payer] += item_total - discount
        shares = {"A": 0.0, "B": 0.0, "C": 0.0}
        for amount in lines:
            code = rng.choice(codes)
            items.append(_item(rid, f"{amount:.2f}", code))
            people = "ABC" if code == "ALL" else code
            for person in people:
                shares[person] += amount / len(people)
        if discount:
            positive = {k: max(0.0, v) for k, v in shares.items()}
            weight = sum(positive.values()) or 3.0
            for person in "ABC":
                share_weight = positive[person] if sum(positive.values()) else 1.0
                shares[person] -= discount * share_weight / weight
        for person in "ABC":
            expected[person] -= shares[person]

    balances = compute_balances(_ledger(receipts, items), NO_SETTLEMENTS)

    for person in "ABC":
        assert balances[person] == pytest.approx(expected[person] * 100, abs=1)
    assert sum(balances.values()) == 0
