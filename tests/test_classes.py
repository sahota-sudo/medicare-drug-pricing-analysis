"""Class definition and dose-normalization tests.

The normalization is the analytical claim of this project, so it is
tested against hand-computed values rather than against itself.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cms.classes import (
    RETINAL_ANTI_VEGF, attach_class, class_frame, class_members,
    parse_billing_unit_mg, price_comparable,
)
from cms.config import RAW_DIR
from cms.extract import find_raw_files, read_part_b
from cms.transform import add_derived_measures, to_long


@pytest.fixture(scope="module")
def class_long():
    found = find_raw_files(RAW_DIR)
    if "Part B" not in found:
        pytest.skip("No Part B file in data/raw")
    return attach_class(add_derived_measures(to_long(read_part_b(found["Part B"]))))


@pytest.mark.parametrize("description,expected", [
    ("Injection, aflibercept, 1 mg", 1.0),
    ("Injection, faricimab-svoa, 0.1 mg", 0.1),
    ("Injection, bevacizumab, 10 mg", 10.0),
    ("Injection, something, 500 mcg", 0.5),
    ("Injection, something, 25 units", None),      # activity units, not mass
    ("Injection, something, per 5 ml", None),      # volume, not mass
    ("Supply of a device", None),
    (None, None),
])
def test_parse_billing_unit_mg(description, expected):
    assert parse_billing_unit_mg(description) == expected


def test_class_definition_is_internally_consistent():
    frame = class_frame()
    assert frame["hcpcs_code"].is_unique
    assert (frame["labeled_dose_mg"] > 0).all()
    # Every biosimilar names the originator it references, and that
    # originator is itself in the class.
    biosimilars = frame[frame["entrant_type"] == "biosimilar"]
    assert biosimilars["reference_product"].notna().all()
    assert biosimilars["reference_product"].isin(frame["hcpcs_code"]).all()


def test_every_exclusion_carries_a_reason():
    """An exclusion without a stated reason is indistinguishable from
    cherry-picking, so the dataclass requires one."""
    for member in RETINAL_ANTI_VEGF:
        if not (member.include_in_class_totals and member.include_in_price_comparison):
            assert member.note, f"{member.display_name} is excluded with no reason given"


def test_cost_per_dose_is_hand_checkable(class_long):
    """Aflibercept bills per 1 mg and is dosed at 2 mg, so a dose is
    exactly twice the published per-unit figure."""
    row = class_long[
        (class_long["hcpcs_code"] == "J0178") & class_long["marketed"]
    ].sort_values("data_year").iloc[-1]
    assert row["billing_unit_mg"] == 1.0
    assert row["units_per_dose"] == 2.0
    assert row["cost_per_dose_usd"] == pytest.approx(
        row["avg_spend_per_dosage_unit"] * 2
    )


def test_normalization_changes_the_ranking(class_long):
    """The claim the project rests on: the raw column mis-ranks."""
    comparable = price_comparable(class_long)
    latest = comparable[comparable["data_year"] == comparable["data_year"].max()]
    by_unit = latest.sort_values("avg_spend_per_dosage_unit")["display_name"].tolist()
    by_dose = latest.sort_values("cost_per_dose_usd")["display_name"].tolist()
    assert by_unit != by_dose
    # The product needing the most billing units per dose is the one
    # the raw column understates most: it looks cheap per unit and is
    # not cheap per dose.
    most_units = latest.loc[latest["units_per_dose"].idxmax(), "display_name"]
    assert by_unit.index(most_units) < by_dose.index(most_units)


def test_class_totals_and_price_comparison_are_different_filters(class_long):
    """A product can be in the class without being price-comparable."""
    totals = class_members(class_long)
    priced = price_comparable(class_long)
    assert set(priced["display_name"]) < set(totals["display_name"])


def test_shared_oncology_code_is_out_of_class_totals(class_long):
    """Bevacizumab's spending is mostly oncology under the same HCPCS.

    Leaving it in would make oncology volume look like retinal share
    loss, so it is excluded from totals, not merely from pricing.
    """
    totals = class_members(class_long)
    assert "Bevacizumab" not in set(totals["display_name"])


def test_outlier_flagged_rows_are_kept_but_not_priced(class_long):
    flagged = class_long[class_long["outlier_flag"].fillna(False)]
    if flagged.empty:
        pytest.skip("no outlier-flagged rows in this release")
    priced = price_comparable(class_long)
    assert not priced["outlier_flag"].fillna(False).any()
