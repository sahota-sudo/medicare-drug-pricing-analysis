"""
Reshape the CMS files from wide to long, and handle the traps.

The files arrive one row per drug with five years of measures spread
across columns. Every analysis wants the opposite: one row per drug
per year. The unpivot is routine. What is not routine is what the
blanks and the flags mean, and getting those wrong produces numbers
that look plausible and are wrong:

  * A blank year is not a zero and not missing data. It means the drug
    had no Medicare spending in that year -- usually because it had not
    launched yet, occasionally because it left the market. Filling
    blanks with zero makes a 2022 launch look like a drug that
    collapsed in 2020; dropping the rows loses the launch date.

  * Part D ships an 'Overall' row per drug AND a row per manufacturer.
    Summing the file without filtering double-counts every
    multi-source drug -- and by a different factor for each, since the
    manufacturer count varies from 1 to 46.

  * A '*' on a name means several brand or generic names share one
    HCPCS code; '**' means CMS had no name and substituted the HCPCS
    short description. Joining on raw names silently fails on both.
"""

from __future__ import annotations

import pandas as pd

from .extract import RawDataset

NUMERIC_MEASURES = [
    "total_spending_usd",
    "total_dosage_units",
    "total_claims",
    "total_beneficiaries",
    "avg_spend_per_dosage_unit",
    "avg_spend_per_claim",
    "avg_spend_per_beneficiary",
]


def clean_name(name: object) -> str | None:
    """Strip CMS's name-ambiguity markers, preserving the name itself."""
    if not isinstance(name, str):
        return None
    return name.rstrip("*").strip() or None


def name_flags(name: object) -> tuple[bool, bool]:
    """(multiple_names, name_unavailable) from the trailing asterisks.

    Order matters: '**' must be tested before '*'.
    """
    if not isinstance(name, str):
        return (False, False)
    stripped = name.strip()
    if stripped.endswith("**"):
        return (False, True)
    if stripped.endswith("*"):
        return (True, False)
    return (False, False)


def to_long(dataset: RawDataset) -> pd.DataFrame:
    """One row per drug per year, with blanks preserved as nulls."""
    frame = dataset.frame
    measures = dataset.measures

    if dataset.program == "Part B":
        id_columns = {
            "hcpcs_code": frame["HCPCS_Cd"].str.strip(),
            "hcpcs_description": frame["HCPCS_Desc"].str.strip(),
            "brand_name_raw": frame["Brnd_Name"],
            "generic_name_raw": frame["Gnrc_Name"],
            "manufacturer": None,
            "manufacturer_count": None,
        }
    else:
        id_columns = {
            "hcpcs_code": None,
            "hcpcs_description": None,
            "brand_name_raw": frame["Brnd_Name"],
            "generic_name_raw": frame["Gnrc_Name"],
            "manufacturer": frame["Mftr_Name"].str.strip(),
            "manufacturer_count": pd.to_numeric(frame["Tot_Mftr"], errors="coerce"),
        }

    identity = pd.DataFrame(
        {k: v for k, v in id_columns.items() if v is not None}, index=frame.index
    )
    identity["brand_name"] = identity["brand_name_raw"].map(clean_name)
    identity["generic_name"] = identity["generic_name_raw"].map(clean_name)
    flags = identity["brand_name_raw"].map(name_flags)
    identity["has_multiple_names"] = [f[0] for f in flags]
    identity["name_unavailable"] = [f[1] for f in flags]
    identity = identity.drop(columns=["brand_name_raw", "generic_name_raw"])

    years = []
    for year in dataset.years:
        block = identity.copy()
        block["data_year"] = year
        for target, template in measures.items():
            source = template.format(year=year)
            values = pd.to_numeric(frame[source], errors="coerce")
            if target == "outlier_flag":
                # Nullable boolean: the flag is genuinely absent for a
                # handful of Part D rows, which is different from False.
                block[target] = values.map({1: True, 0: False}).astype("boolean")
            else:
                block[target] = values
        years.append(block)

    long = pd.concat(years, ignore_index=True)
    long["program"] = dataset.program

    # A year with no spending is a year the drug was not on the market.
    # Recording that explicitly is what lets downstream code compute a
    # launch year without re-deriving this rule every time.
    long["marketed"] = long["total_spending_usd"].notna()

    ordered = [
        "program", "data_year", "hcpcs_code", "hcpcs_description",
        "brand_name", "generic_name", "manufacturer", "manufacturer_count",
        "has_multiple_names", "name_unavailable", "marketed",
        *NUMERIC_MEASURES, "outlier_flag",
    ]
    return long[[c for c in ordered if c in long.columns]]


def part_d_market_total(long: pd.DataFrame) -> pd.DataFrame:
    """Part D rows that represent the whole drug, not one manufacturer.

    Use this for market sizing. Using the manufacturer rows instead,
    or failing to exclude 'Overall', double-counts multi-source drugs.
    """
    if "manufacturer" not in long.columns:
        return long
    return long[long["manufacturer"].eq("Overall")].copy()


def part_d_by_manufacturer(long: pd.DataFrame) -> pd.DataFrame:
    """Part D rows attributed to a named manufacturer.

    The complement of `part_d_market_total`. These sum to the Overall
    row for each drug-year, which `tests/test_transform.py` asserts.
    """
    if "manufacturer" not in long.columns:
        return long.iloc[0:0].copy()
    return long[~long["manufacturer"].eq("Overall")].copy()


def add_derived_measures(long: pd.DataFrame) -> pd.DataFrame:
    """Year-over-year change and claims-per-beneficiary.

    CMS ships its own precomputed YoY change and five-year CAGR
    columns. They are recomputed here instead of imported, for two
    reasons: they only exist for the most recent year pair, and
    recomputing them is the only way to know that this pipeline's
    grouping keys reproduce CMS's published figures. The reconciliation
    is asserted in the test suite.
    """
    out = long.sort_values(["program", "brand_name", "hcpcs_code", "data_year"]
                           if "hcpcs_code" in long.columns
                           else ["program", "brand_name", "manufacturer", "data_year"])
    keys = ["program", "hcpcs_code"] if "hcpcs_code" in out.columns \
        else ["program", "brand_name", "generic_name", "manufacturer"]

    grouped = out.groupby(keys, dropna=False)
    out["prior_spending_usd"] = grouped["total_spending_usd"].shift(1)
    out["prior_price_per_unit"] = grouped["avg_spend_per_dosage_unit"].shift(1)
    out["prior_beneficiaries"] = grouped["total_beneficiaries"].shift(1)

    out["spending_yoy_pct"] = (
        out["total_spending_usd"] / out["prior_spending_usd"] - 1
    )
    out["price_yoy_pct"] = (
        out["avg_spend_per_dosage_unit"] / out["prior_price_per_unit"] - 1
    )
    out["beneficiaries_yoy_pct"] = (
        out["total_beneficiaries"] / out["prior_beneficiaries"] - 1
    )
    out["claims_per_beneficiary"] = (
        out["total_claims"] / out["total_beneficiaries"]
    )

    # First year with spending, carried on every row of that drug, so a
    # launch cohort can be built without a second pass.
    marketed_years = out[out["marketed"]].groupby(keys, dropna=False)["data_year"]
    first_year = marketed_years.min().rename("first_marketed_year")
    out = out.merge(first_year, on=keys, how="left")
    out["years_since_first_marketed"] = out["data_year"] - out["first_marketed_year"]
    return out.reset_index(drop=True)
