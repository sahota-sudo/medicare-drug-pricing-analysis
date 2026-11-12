"""
Regression tests for the PostgreSQL load path.

Neither of these failures is reachable from the DuckDB test harness:
DuckDB accepts pandas' missing-value sentinel and has no bind-parameter
ceiling. They only appear against a real database, which is exactly the
kind of bug worth pinning with a test rather than rediscovering in CI.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cms.db import _prepare_for_insert, _safe_chunksize, split_statements
from cms.pipeline import build_facts


@pytest.fixture(scope="module")
def tables():
    return build_facts()


def test_no_pandas_sentinels_survive_preparation(tables):
    """pd.NA reaches the driver as an unadaptable type, not as NULL."""
    for name, frame in tables.items():
        prepared = _prepare_for_insert(frame)
        for column in prepared.columns:
            values = prepared[column]
            assert not values.map(lambda v: v is pd.NA).any(), \
                f"{name}.{column} still carries pd.NA"
            assert not values.map(
                lambda v: isinstance(v, float) and v != v
            ).any(), f"{name}.{column} still carries NaN"


def test_missing_values_become_none(tables):
    frame = tables["drug_spending"]
    prepared = _prepare_for_insert(frame)
    unmarketed = prepared[~prepared["marketed"].astype(bool)]
    assert len(unmarketed) > 0
    assert unmarketed["total_spending_usd"].map(lambda v: v is None).all()


def test_chunksize_respects_the_parameter_ceiling(tables):
    """PostgreSQL refuses a statement with more than 65,535 parameters.

    method="multi" sends chunksize x columns parameters per statement,
    so the row count has to be derived from the column count.
    """
    for frame in tables.values():
        prepared = _prepare_for_insert(frame)
        parameters = _safe_chunksize(prepared) * len(prepared.columns)
        assert parameters < 65_535


def test_chunksize_shrinks_as_columns_grow():
    narrow = pd.DataFrame({"a": [1], "b": [2]})
    wide = pd.DataFrame({str(i): [i] for i in range(50)})
    assert _safe_chunksize(narrow) > _safe_chunksize(wide)
    assert _safe_chunksize(wide) >= 1


def test_statement_splitter_handles_the_projects_sql():
    from cms.config import SQL_DIR

    for path in sorted(SQL_DIR.glob("*.sql")):
        statements = split_statements(path.read_text())
        assert statements
        assert not any(s.strip().endswith(";") for s in statements)
        # Nothing should be split mid-literal: every statement that
        # opens a quote must close it.
        for statement in statements:
            assert statement.count("'") % 2 == 0
