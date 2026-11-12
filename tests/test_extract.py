"""Extraction tests: the file must be what we think it is, or fail loudly."""

from __future__ import annotations

import pandas as pd
import pytest

from cms.extract import SchemaError, discover_years, find_raw_files, read_part_b
from cms.config import RAW_DIR


def test_discover_years_reads_the_header():
    columns = ["HCPCS_Cd", "Tot_Spndng_2020", "Tot_Spndng_2021", "Tot_Spndng_2022"]
    assert discover_years(columns) == (2020, 2021, 2022)


def test_discover_years_rejects_a_file_with_no_year_columns():
    with pytest.raises(SchemaError, match="does not look like"):
        discover_years(["Brnd_Name", "Spending"])


def test_discover_years_rejects_a_gap():
    """A missing year would silently shorten every trend in the project."""
    with pytest.raises(SchemaError, match="not contiguous"):
        discover_years(["Tot_Spndng_2020", "Tot_Spndng_2022"])


def test_missing_file_names_the_download_source():
    with pytest.raises(FileNotFoundError, match="cms.gov"):
        read_part_b("does/not/exist.csv")


def test_find_raw_files_matches_cms_naming():
    found = find_raw_files(RAW_DIR)
    if not found:
        pytest.skip("No CMS CSVs in data/raw")
    assert "Part B" in found
    assert "PTB" in found["Part B"].name.upper()


def test_part_b_file_has_the_expected_shape():
    found = find_raw_files(RAW_DIR)
    if "Part B" not in found:
        pytest.skip("No Part B file in data/raw")
    dataset = read_part_b(found["Part B"])
    assert dataset.program == "Part B"
    assert len(dataset.years) >= 2
    assert dataset.frame["HCPCS_Cd"].is_unique
    # Everything is read as text so that a column's dtype cannot change
    # between releases (pandas renders that as object or StringDtype
    # depending on version; what matters is that it is not numeric).
    column = dataset.frame["Tot_Spndng_" + str(dataset.years[-1])]
    assert not pd.api.types.is_numeric_dtype(column)
