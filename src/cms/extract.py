"""
Read the raw CMS files and verify they are the shape we think they are.

CMS republishes these files annually: a year is added, the oldest is
dropped, and column names move with it (Tot_Spndng_2020 disappears,
Tot_Spndng_2025 appears). Hard-coding years would mean the pipeline
breaks silently -- producing a shorter trend rather than an error. So
the years are discovered from the header, and the only hard assertion
is on the columns whose names do not carry a year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

#: Columns that must exist regardless of which years are published.
PART_B_KEY_COLUMNS = ["HCPCS_Cd", "HCPCS_Desc", "Brnd_Name", "Gnrc_Name"]
PART_D_KEY_COLUMNS = ["Brnd_Name", "Gnrc_Name", "Tot_Mftr", "Mftr_Name"]

#: Per-year measures, named with a {year} placeholder. Part B and Part D
#: differ in one name: Part D weights its per-unit average across
#: manufacturers, and says so in the column name.
PART_B_MEASURES = {
    "total_spending_usd": "Tot_Spndng_{year}",
    "total_dosage_units": "Tot_Dsg_Unts_{year}",
    "total_claims": "Tot_Clms_{year}",
    "total_beneficiaries": "Tot_Benes_{year}",
    "avg_spend_per_dosage_unit": "Avg_Spndng_Per_Dsg_Unt_{year}",
    "avg_spend_per_claim": "Avg_Spndng_Per_Clm_{year}",
    "avg_spend_per_beneficiary": "Avg_Spndng_Per_Bene_{year}",
    "outlier_flag": "Outlier_Flag_{year}",
}

PART_D_MEASURES = {
    **PART_B_MEASURES,
    "avg_spend_per_dosage_unit": "Avg_Spnd_Per_Dsg_Unt_Wghtd_{year}",
    "avg_spend_per_claim": "Avg_Spnd_Per_Clm_{year}",
    "avg_spend_per_beneficiary": "Avg_Spnd_Per_Bene_{year}",
}


class SchemaError(ValueError):
    """Raised when a CMS file does not match the expected structure."""


@dataclass(frozen=True)
class RawDataset:
    """A loaded CMS file plus what we discovered about it."""

    program: str                 # 'Part B' or 'Part D'
    frame: pd.DataFrame
    years: tuple[int, ...]
    source_file: str

    @property
    def measures(self) -> dict[str, str]:
        return PART_B_MEASURES if self.program == "Part B" else PART_D_MEASURES


def discover_years(columns: list[str]) -> tuple[int, ...]:
    """Find the data years published in this release, from the header."""
    years = sorted(
        {int(match.group(1)) for col in columns
         if (match := re.search(r"^Tot_Spndng_(\d{4})$", col))}
    )
    if not years:
        raise SchemaError(
            "No Tot_Spndng_<year> columns found. This does not look like a "
            "CMS drug spending file."
        )
    gaps = [b - a for a, b in zip(years, years[1:])]
    if any(gap != 1 for gap in gaps):
        raise SchemaError(f"Data years are not contiguous: {years}")
    return tuple(years)


def _read(path: Path, program: str, key_columns: list[str],
          measures: dict[str, str]) -> RawDataset:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download the annual {program} file from "
            "https://www.cms.gov/data-research/statistics-trends-and-reports/"
            "cms-drug-spending and place it in data/raw/."
        )
    # Everything is read as text first: brand names contain commas and
    # quotes, and a column read as float once and object the next time
    # is a debugging session nobody needs.
    frame = pd.read_csv(path, dtype=str, keep_default_na=True)
    frame.columns = [c.strip() for c in frame.columns]

    missing = [c for c in key_columns if c not in frame.columns]
    if missing:
        raise SchemaError(f"{path.name} is missing expected columns: {missing}")

    years = discover_years(list(frame.columns))

    absent = [
        template.format(year=year)
        for template in measures.values()
        for year in years
        if template.format(year=year) not in frame.columns
    ]
    if absent:
        raise SchemaError(
            f"{path.name} is missing {len(absent)} per-year columns, "
            f"starting with {absent[:3]}."
        )
    return RawDataset(program=program, frame=frame, years=years,
                      source_file=path.name)


def read_part_b(path: str | Path) -> RawDataset:
    return _read(Path(path), "Part B", PART_B_KEY_COLUMNS, PART_B_MEASURES)


def read_part_d(path: str | Path) -> RawDataset:
    return _read(Path(path), "Part D", PART_D_KEY_COLUMNS, PART_D_MEASURES)


def find_raw_files(raw_dir: Path) -> dict[str, Path]:
    """Locate the Part B and Part D CSVs by CMS's own naming convention.

    CMS file names carry a release year and a data year
    (DSD_PTB_RY26_P06_V10_DYT24_HCPCS.csv), and the user is likely to
    have renamed or not renamed them. Matching on the PTB / PTD segment
    is the stable part.
    """
    found: dict[str, Path] = {}
    for path in sorted(raw_dir.glob("**/*.csv")):
        name = path.name.upper()
        if "PTB" in name and "Part B" not in found:
            found["Part B"] = path
        elif "PTD" in name and "Part D" not in found:
            found["Part D"] = path
    return found
