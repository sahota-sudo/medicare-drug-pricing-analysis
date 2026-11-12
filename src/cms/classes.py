"""
Therapeutic class definitions and billing-unit normalization.

This module exists because of one sentence in the CMS data dictionary:
for Part B, the quantity of a drug in a dosage unit is the same as the
quantity in a HCPCS billing unit. Billing units are defined per code,
not per class, so the "average spending per dosage unit" column is in
different units for every drug.

Within the retinal anti-VEGF class, for example, one code bills per
1 mg and another bills per 0.1 mg. Comparing those two numbers
directly -- which is what a GROUP BY on the raw column does -- makes
the second drug look like a tenth the price of the first. The correct
comparison is cost per administered dose, which requires two things
the file does not contain: the milligrams in a billing unit (parsed
from the code description) and the labeled dose (supplied here, from
the products' prescribing information).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

#: Matches the dose amount at the end of a HCPCS descriptor, e.g.
#: "Injection, aflibercept, 1 mg" or "Injection, faricimab-svoa, 0.1 mg".
_UNIT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|microgram|milligram|unit|units|ml)\b",
    re.IGNORECASE,
)

_TO_MG = {"mg": 1.0, "milligram": 1.0, "mcg": 0.001, "microgram": 0.001}


def parse_billing_unit_mg(description: object) -> float | None:
    """Milligrams in one HCPCS billing unit, read from the descriptor.

    Returns None when the descriptor is not expressed in mass units
    (units of activity, millilitres, or per-device codes). Those drugs
    cannot be put on a common price axis with the rest and are excluded
    from cross-drug comparison rather than silently mis-scaled.
    """
    if not isinstance(description, str):
        return None
    match = _UNIT_PATTERN.search(description)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).lower()
    factor = _TO_MG.get(unit)
    return amount * factor if factor else None


@dataclass(frozen=True)
class ClassMember:
    """One product in a therapeutic class.

    labeled_dose_mg is the dose per administration from the product's
    FDA prescribing information for its principal indication. It is
    supplied here because CMS publishes spending, not posology; it is
    the one external input this project needs, and it is kept in one
    visible table rather than scattered through the analysis.

    entrant_type distinguishes the originator molecules from biosimilar
    entrants, which is what makes the erosion analysis possible.
    """

    hcpcs_code: str
    display_name: str
    labeled_dose_mg: float
    entrant_type: str          # 'originator' | 'biosimilar' | 'line-extension'
    reference_product: str | None = None
    modality: str = "intravitreal injection"
    include_in_class_totals: bool = True
    include_in_price_comparison: bool = True
    note: str = ""


#: Retinal anti-VEGF therapies reimbursed under Medicare Part B.
#:
#: Doses are the labeled dose per eye per administration. Dosing
#: interval deliberately is NOT modeled: the products differ in label
#: interval and in real-world treat-and-extend practice, and asserting
#: an interval would convert a measured price into an assumed annual
#: cost. Cost per injection is measured; cost per year is not.
RETINAL_ANTI_VEGF: tuple[ClassMember, ...] = (
    ClassMember("J0178", "Aflibercept 2 mg", 2.0, "originator"),
    ClassMember("J0177", "Aflibercept HD 8 mg", 8.0, "line-extension",
                reference_product="J0178"),
    ClassMember("J2777", "Faricimab", 6.0, "originator"),
    ClassMember("J2778", "Ranibizumab", 0.5, "originator"),
    ClassMember("J0179", "Brolucizumab", 6.0, "originator"),
    ClassMember("Q5124", "Ranibizumab-nuna", 0.5, "biosimilar",
                reference_product="J2778"),
    ClassMember("Q5128", "Ranibizumab-eqrn", 0.5, "biosimilar",
                reference_product="J2778"),
    ClassMember(
        "J2779", "Ranibizumab ocular implant", 2.0, "line-extension",
        reference_product="J2778", modality="ocular implant refill",
        include_in_price_comparison=False,
        note="Refill-exchange of a surgically placed implant. Cost per refill "
             "is not comparable to cost per injection, and the device and "
             "insertion procedure are billed separately.",
    ),
    ClassMember(
        "J9035", "Bevacizumab", 1.25, "originator",
        include_in_class_totals=False, include_in_price_comparison=False,
        note="Billed overwhelmingly for oncology indications at systemic doses; "
             "ophthalmic use is a compounded repackage of the same code. Its "
             "spending cannot be attributed to the retinal class, so it is "
             "excluded from class totals, share and concentration as well as "
             "from price comparison. Excluding it matters: it is 14% of a naive "
             "class total in 2020 and 3% by 2024, so leaving it in would make "
             "oncology volume look like retinal share loss.",
    ),
)

THERAPEUTIC_CLASSES = {"Retinal anti-VEGF": RETINAL_ANTI_VEGF}


def class_frame(class_name: str = "Retinal anti-VEGF") -> pd.DataFrame:
    """The class definition as a DataFrame, for joining to the facts."""
    members = THERAPEUTIC_CLASSES[class_name]
    return pd.DataFrame(
        [
            {
                "hcpcs_code": m.hcpcs_code,
                "therapeutic_class": class_name,
                "display_name": m.display_name,
                "labeled_dose_mg": m.labeled_dose_mg,
                "entrant_type": m.entrant_type,
                "reference_product": m.reference_product,
                "modality": m.modality,
                "include_in_class_totals": m.include_in_class_totals,
                "include_in_price_comparison": m.include_in_price_comparison,
                "exclusion_note": m.note,
            }
            for m in members
        ]
    )


def attach_class(long: pd.DataFrame, class_name: str = "Retinal anti-VEGF") -> pd.DataFrame:
    """Join a class definition and derive dose-normalized price.

    Adds:
      billing_unit_mg    - mg in one billing unit, from the descriptor
      units_per_dose     - billing units consumed by one labeled dose
      cost_per_dose_usd  - the only price figure comparable across the class
    """
    definition = class_frame(class_name)
    merged = long.merge(definition, on="hcpcs_code", how="inner")

    merged["billing_unit_mg"] = merged["hcpcs_description"].map(parse_billing_unit_mg)
    merged["units_per_dose"] = merged["labeled_dose_mg"] / merged["billing_unit_mg"]
    merged["cost_per_dose_usd"] = (
        merged["avg_spend_per_dosage_unit"] * merged["units_per_dose"]
    )

    # A dose that is not a whole number of billing units is legitimate
    # (0.5 mg at 0.1 mg per unit is 5 units; 1.25 mg at 10 mg per unit
    # is 0.125 units) but worth surfacing, because it usually means the
    # code is shared with a different route of administration.
    merged["fractional_billing_unit"] = merged["units_per_dose"] < 1
    return merged


def class_members(class_long: pd.DataFrame) -> pd.DataFrame:
    """Rows that count toward class spending, share and concentration.

    Distinct from `price_comparable`: a product can legitimately be
    part of the class while not being priced on the same axis (an
    implant refill is real retinal volume but is not an injection).
    What is excluded here is volume that is not the class's at all.
    """
    return class_long[class_long["include_in_class_totals"]].copy()


def price_comparable(class_long: pd.DataFrame) -> pd.DataFrame:
    """Rows eligible for cross-drug price comparison.

    Excludes products flagged in the class definition (different
    modality or a shared code), rows CMS flagged as outliers, and rows
    with no parsed billing unit.
    """
    return class_long[
        class_long["include_in_class_totals"]
        & class_long["include_in_price_comparison"]
        & class_long["billing_unit_mg"].notna()
        & ~class_long["outlier_flag"].fillna(False)
    ].copy()
