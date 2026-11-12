"""PostgreSQL helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError

from .config import DatabaseConfig, SQL_DIR


def get_engine(config: DatabaseConfig | None = None) -> Engine:
    return create_engine((config or DatabaseConfig()).url, future=True)


def split_statements(sql: str) -> list[str]:
    """Split a .sql file into individual statements.

    Semicolons inside string literals and inside ``--`` comments are
    not separators, so a plain ``sql.split(";")`` would cut a statement
    in half the first time either appears -- and both do appear in this
    project's comments. This walks the text instead, tracking whether
    it is inside a literal or a comment.

    It is not a general SQL parser: dollar-quoted bodies and ``/* */``
    blocks are not handled, because this project's SQL contains
    neither. Adding one would mean replacing this.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_comment = False
    index = 0

    while index < len(sql):
        char = sql[index]
        pair = sql[index:index + 2]

        if in_comment:
            if char == "\n":
                in_comment = False
                current.append(char)
            index += 1
            continue

        if in_string:
            current.append(char)
            if char == "'":
                # '' is an escaped quote, not the end of the literal.
                if sql[index + 1:index + 2] == "'":
                    current.append("'")
                    index += 2
                    continue
                in_string = False
            index += 1
            continue

        if pair == "--":
            in_comment = True
            index += 2
            continue

        if char == "'":
            in_string = True
            current.append(char)
            index += 1
            continue

        if char == ";":
            statements.append("".join(current).strip())
            current = []
            index += 1
            continue

        current.append(char)
        index += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def run_sql_file(engine: Engine, path: str | Path) -> None:
    """Run a .sql file as one transaction.

    exec_driver_sql rather than text(): the driver gets the file
    verbatim, so nothing in it is mistaken for a bind parameter.

    Drivers disagree about whether one execute() may carry several
    statements, so a rejection falls back to running them one at a
    time -- still inside a single transaction, so a failure part way
    through leaves no half-built schema.
    """
    sql = Path(path).read_text(encoding="utf-8")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
    except DatabaseError:
        with engine.begin() as conn:
            for statement in split_statements(sql):
                conn.exec_driver_sql(statement)


def apply_all_sql(engine: Engine, sql_dir: Path | None = None) -> list[str]:
    applied = []
    for path in sorted((sql_dir or SQL_DIR).glob("*.sql")):
        run_sql_file(engine, path)
        applied.append(path.name)
    return applied


def read_sql(engine: Engine, query: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql_query(query, conn)


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    """Convert pandas missing values to None.

    pandas nullable dtypes carry ``pd.NA``, which the PostgreSQL driver
    cannot adapt -- it raises rather than treating it as NULL. DuckDB
    accepts it, so this never shows up in the test suite; it only
    surfaces against a real database. Casting to object and replacing
    every missing value with None gives the driver plain Python values.
    """
    prepared = df.astype(object)
    return prepared.where(df.notna(), None)


def _safe_chunksize(df: pd.DataFrame, max_parameters: int = 30_000) -> int:
    """Rows per INSERT, bounded by PostgreSQL's parameter limit.

    ``method="multi"`` packs a whole chunk into one statement, and
    PostgreSQL refuses a statement with more than 65,535 bind
    parameters. Parameters are rows x columns, so a fixed row count is
    a bug waiting for a table with enough columns: 10,000 rows of a
    19-column table is 190,000 parameters and fails outright. The
    budget below is deliberately well under the limit.
    """
    columns = max(len(df.columns), 1)
    return max(1, max_parameters // columns)


def load_dataframe(engine: Engine, df: pd.DataFrame, table: str,
                   schema: str = "cms", chunksize: int | None = None) -> int:
    prepared = _prepare_for_insert(df)
    prepared.to_sql(
        table, engine, schema=schema, if_exists="append", index=False,
        chunksize=chunksize or _safe_chunksize(prepared), method="multi",
    )
    return len(prepared)
