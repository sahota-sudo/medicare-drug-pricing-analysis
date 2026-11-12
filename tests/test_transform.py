"""Transform tests: the traps, pinned down."""

from __future__ import annotations

import pandas as pd
import pytest

from cms.classes import attach_class, class_members, price_comparable
from cms.config import RAW_DIR
from cms.extract import find_raw_files, read_part_b, read_part_d
from cms.transform import (
    add_derived_measures, clean_name, name_flags,
    part_d_by_manufacturer, part_d_market_total, to_long,
)


@pytest.fixture(scope="module")
def part_b_long():
    found = find_raw_files(RAW_DIR)
    if "Part B" not in found:
        pytest.skip("No Part B file in data/raw")
    return add_derived_measures(to_long(read_part_b(found["Part B"])))


@pytest.fixture(scope="module")
def part_d_long():
    found = find_raw_files(RAW_DIR)
    if "Part D" not in found:
        pytest.skip("No Part D file in data/raw")
    return to_long(read_part_d(found["Part D"]))


# ---------------------------------------------------------------- names

@pytest.mark.parametrize("raw,expected", [
    ("Aflibercept", "Aflibercept"),
    ("HyperHEP B*", "HyperHEP B"),
    ("Something**", "Something"),
    ("  Padded  ", "Padded"),
    (None, None),
])
def test_clean_name_strips_markers_not_content(raw, expected):
    assert clean_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Aflibercept", (False, False)),
    ("Aflibercept*", (True, False)),
    ("Aflibercept**", (False, True)),   # '**' must win over '*'
])
def test_name_flags(raw, expected):
    assert name_flags(raw) == expected


# ------------------------------------------------------- blanks vs zeros

def test_unmarketed_years_have_no_measures(part_b_long):
    unmarketed = part_b_long[~part_b_long["marketed"]]
    assert len(unmarketed) > 0, "expected some pre-launch years in the data"
    for column in ["total_spending_usd", "total_dosage_units", "total_beneficiaries"]:
        assert unmarketed[column].isna().all()


def test_blank_years_are_not_filled_with_zero(part_b_long):
    """Zero-filling would turn a 2022 launch into a 2020 collapse."""
    assert not (part_b_long.loc[~part_b_long["marketed"], "total_spending_usd"] == 0).any()


def test_first_marketed_year_identifies_launches(part_b_long):
    launched_late = part_b_long[
        part_b_long["first_marketed_year"] > part_b_long["data_year"].min()
    ]
    assert len(launched_late) > 0
    # Every row of a drug carries the same launch year.
    per_drug = launched_late.groupby("hcpcs_code")["first_marketed_year"].nunique()
    assert (per_drug == 1).all()


# ------------------------------------------------- the Part D double count

def test_overall_and_manufacturer_rows_are_disjoint(part_d_long):
    overall = part_d_market_total(part_d_long)
    by_mftr = part_d_by_manufacturer(part_d_long)
    assert len(overall) + len(by_mftr) == len(part_d_long)
    assert overall["manufacturer"].eq("Overall").all()
    assert not by_mftr["manufacturer"].eq("Overall").any()


def test_manufacturer_rows_reconcile_to_overall(part_d_long):
    """They sum to the same total, which is why filtering is safe."""
    year = part_d_long["data_year"].max()
    overall = part_d_market_total(part_d_long)
    by_mftr = part_d_by_manufacturer(part_d_long)
    a = overall.loc[overall["data_year"] == year, "total_spending_usd"].sum()
    b = by_mftr.loc[by_mftr["data_year"] == year, "total_spending_usd"].sum()
    assert abs(a - b) / a < 1e-4


def test_naive_sum_double_counts(part_d_long):
    """The mistake this project exists to avoid, measured."""
    year = part_d_long["data_year"].max()
    naive = part_d_long.loc[part_d_long["data_year"] == year, "total_spending_usd"].sum()
    correct = part_d_market_total(part_d_long).pipe(
        lambda d: d.loc[d["data_year"] == year, "total_spending_usd"].sum()
    )
    assert naive / correct == pytest.approx(2.0, abs=0.01)


# ------------------------------------------------------------- price identity

def test_part_b_price_identity_holds(part_b_long):
    marketed = part_b_long[part_b_long["marketed"]]
    implied = marketed["total_spending_usd"] / marketed["total_dosage_units"]
    relative = (implied - marketed["avg_spend_per_dosage_unit"]).abs() \
        / marketed["avg_spend_per_dosage_unit"].abs()
    assert relative.max() < 1e-4


def test_part_d_published_price_is_a_weighted_average(part_d_long):
    """Part D's per-unit figure is NOT spending / units.

    If this ever starts passing as an identity, the price/volume
    decomposition could use the published column directly and the
    caveat in the README should be removed.
    """
    marketed = part_d_long[part_d_long["marketed"]]
    implied = marketed["total_spending_usd"] / marketed["total_dosage_units"]
    relative = (implied - marketed["avg_spend_per_dosage_unit"]).abs() \
        / marketed["avg_spend_per_dosage_unit"].abs()
    assert (relative > 0.001).mean() > 0.1


# ------------------------------------------------------------ derived measures

def test_yoy_is_null_in_a_drugs_first_year(part_b_long):
    first_rows = part_b_long[
        part_b_long["data_year"] == part_b_long["first_marketed_year"]
    ]
    assert first_rows["price_yoy_pct"].isna().all()


def test_yoy_matches_a_hand_computed_case(part_b_long):
    drug = part_b_long[part_b_long["hcpcs_code"] == "J0178"].sort_values("data_year")
    marketed = drug[drug["marketed"]]
    if len(marketed) < 2:
        pytest.skip("not enough years")
    prior, current = marketed.iloc[-2], marketed.iloc[-1]
    expected = current["avg_spend_per_dosage_unit"] / prior["avg_spend_per_dosage_unit"] - 1
    assert current["price_yoy_pct"] == pytest.approx(expected)
