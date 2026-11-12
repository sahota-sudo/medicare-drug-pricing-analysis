"""
Shared fixtures.

The SQL in sql/ targets PostgreSQL, but the analytical layer is
portable SQL: views, window functions, CASE. DuckDB runs those
unchanged, so the suite loads the real CMS data, applies the real view
files and asserts on the results -- no database server in CI, and the
views under test are the same text that ships to Postgres.

Only the DDL needs translating, which `_translate_ddl` does visibly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cms.config import RAW_DIR  # noqa: E402
from cms.pipeline import build_facts  # noqa: E402

SQL_DIR = ROOT / "sql"


def _translate_ddl(sql: str) -> str:
    sql = re.sub(r"\bBIGSERIAL\b", "BIGINT", sql, flags=re.I)
    sql = re.sub(r"\bSERIAL\b", "INTEGER", sql, flags=re.I)
    sql = re.sub(r"\bTIMESTAMPTZ NOT NULL DEFAULT now\(\)", "TIMESTAMP", sql, flags=re.I)
    sql = re.sub(r"SET search_path TO [^;]+;", "", sql, flags=re.I)
    sql = sql.replace("DROP SCHEMA IF EXISTS cms CASCADE;", "")
    sql = sql.replace("CREATE SCHEMA cms;", "CREATE SCHEMA IF NOT EXISTS cms;")
    return sql


@pytest.fixture(scope="session")
def facts():
    if not any(RAW_DIR.glob("**/*.csv")):
        pytest.skip("No CMS CSVs in data/raw -- see data/raw/README.md")
    return build_facts()


@pytest.fixture(scope="session")
def warehouse(facts):
    con = duckdb.connect()
    # Unqualified CREATE TABLE in the DDL must land in the cms schema,
    # as it does in Postgres via search_path.
    con.execute("CREATE SCHEMA IF NOT EXISTS cms;")
    con.execute("USE cms;")
    con.execute(_translate_ddl((SQL_DIR / "01_schema.sql").read_text()))

    keys = {"drug_spending": "drug_spending_id",
            "therapeutic_class": "therapeutic_class_id"}
    for table, frame in facts.items():
        df = frame.copy()
        df.insert(0, keys[table], range(1, len(df) + 1))
        con.register(f"stage_{table}", df)
        cols = ", ".join(df.columns)
        con.execute(
            f"INSERT INTO cms.{table} ({cols}) SELECT {cols} FROM stage_{table};"
        )

    for path in sorted(SQL_DIR.glob("0[2-9]_*.sql")):
        con.execute(_translate_ddl(path.read_text()))

    yield con
    con.close()
