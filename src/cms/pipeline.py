"""
The ETL pipeline, as a function rather than a script.

Kept separate from the CLI so the test suite and the analysis can
build the same frames without shelling out or touching a database.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config
from .classes import THERAPEUTIC_CLASSES, class_frame, parse_billing_unit_mg
from .extract import find_raw_files, read_part_b, read_part_d
from .transform import add_derived_measures, to_long

#: Columns the warehouse table accepts, in order.
FACT_COLUMNS = [
    "program", "data_year", "hcpcs_code", "hcpcs_description",
    "brand_name", "generic_name", "manufacturer", "manufacturer_count",
    "has_multiple_names", "name_unavailable", "marketed",
    "total_spending_usd", "total_dosage_units", "total_claims",
    "total_beneficiaries", "avg_spend_per_dosage_unit",
    "avg_spend_per_claim", "avg_spend_per_beneficiary", "outlier_flag",
]


def build_facts(raw_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    """Read the raw CMS files and return warehouse-ready frames."""
    raw_dir = raw_dir or config.RAW_DIR
    files = find_raw_files(raw_dir)
    if not files:
        raise FileNotFoundError(
            f"No CMS CSVs found under {raw_dir}. See data/raw/README.md for "
            "the download instructions."
        )

    frames = []
    if "Part B" in files:
        frames.append(to_long(read_part_b(files["Part B"])))
    if "Part D" in files:
        frames.append(to_long(read_part_d(files["Part D"])))

    facts = pd.concat(frames, ignore_index=True)
    for column in FACT_COLUMNS:
        if column not in facts.columns:
            facts[column] = pd.NA
    facts = facts[FACT_COLUMNS]

    # Enforce the rule the schema also checks: an unmarketed year
    # carries no measures. Without this, a stray zero would turn a
    # pre-launch year into a real observation.
    measures = [c for c in FACT_COLUMNS if c.startswith(("total_", "avg_"))]
    facts.loc[~facts["marketed"], measures] = pd.NA

    classes = class_frame()
    classes["billing_unit_mg"] = _billing_units(facts, classes)
    classes = classes[[
        "therapeutic_class", "hcpcs_code", "display_name", "labeled_dose_mg",
        "billing_unit_mg", "entrant_type", "reference_product", "modality",
        "include_in_class_totals", "include_in_price_comparison", "exclusion_note",
    ]]

    return {"drug_spending": facts, "therapeutic_class": classes}


def _billing_units(facts: pd.DataFrame, classes: pd.DataFrame) -> pd.Series:
    """Resolve mg-per-billing-unit from the HCPCS descriptor.

    Parsed from the source data rather than typed into the class
    definition, so that a CMS descriptor change surfaces as a null to
    investigate instead of a silently stale constant.
    """
    descriptors = (
        facts[facts["program"] == "Part B"]
        .dropna(subset=["hcpcs_description"])
        .drop_duplicates("hcpcs_code")
        .set_index("hcpcs_code")["hcpcs_description"]
    )
    return classes["hcpcs_code"].map(descriptors).map(parse_billing_unit_mg)


def build_analysis_frame(raw_dir: Path | None = None) -> pd.DataFrame:
    """Part B facts with derived measures, for the Python analysis."""
    raw_dir = raw_dir or config.RAW_DIR
    files = find_raw_files(raw_dir)
    if "Part B" not in files:
        raise FileNotFoundError(f"No Part B file found under {raw_dir}.")
    return add_derived_measures(to_long(read_part_b(files["Part B"])))
