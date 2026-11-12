"""Paths, database connection, and dataset constants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = PROJECT_ROOT / "sql"
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
DOCS_DIR = PROJECT_ROOT / "docs"
CHART_DIR = DOCS_DIR / "charts"

#: Data years present in the release this project was built against.
#: CMS adds a year and drops the oldest each annual release, so this is
#: discovered from the file's columns at runtime rather than hard-coded
#: into the transforms -- this constant is only the expected default.
EXPECTED_YEARS = (2020, 2021, 2022, 2023, 2024)


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = os.getenv("PGHOST", "localhost")
    port: int = int(os.getenv("PGPORT", "5432"))
    database: str = os.getenv("PGDATABASE", "cms_drug_spending")
    user: str = os.getenv("PGUSER", "cms")
    password: str = os.getenv("PGPASSWORD", "cms")

    @property
    def url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )
