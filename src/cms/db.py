"""PostgreSQL helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .config import DatabaseConfig, SQL_DIR


def get_engine(config: DatabaseConfig | None = None) -> Engine:
    return create_engine((config or DatabaseConfig()).url, future=True)


def run_sql_file(engine: Engine, path: str | Path) -> None:
    """Run a .sql file as one transaction.

    exec_driver_sql rather than text(): the driver gets the file
    verbatim, so multi-statement DDL runs as written and nothing in it
    is mistaken for a bind parameter.
    """
    sql = Path(path).read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)


def apply_all_sql(engine: Engine, sql_dir: Path | None = None) -> list[str]:
    applied = []
    for path in sorted((sql_dir or SQL_DIR).glob("*.sql")):
        run_sql_file(engine, path)
        applied.append(path.name)
    return applied


def read_sql(engine: Engine, query: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql_query(query, conn)


def load_dataframe(engine: Engine, df: pd.DataFrame, table: str,
                   schema: str = "cms", chunksize: int = 10_000) -> int:
    df.to_sql(table, engine, schema=schema, if_exists="append",
              index=False, chunksize=chunksize, method="multi")
    return len(df)
