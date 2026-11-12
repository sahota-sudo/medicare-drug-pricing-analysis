"""
Build the warehouse: apply DDL, transform the raw files, load.

    python -m cms.load --reset

--reset drops and rebuilds the schema so a failed load never leaves a
half-populated warehouse that still answers queries.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .pipeline import build_facts

LOAD_ORDER = ["therapeutic_class", "drug_spending"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the CMS warehouse.")
    parser.add_argument("--reset", action="store_true",
                        help="Drop and recreate the cms schema before loading.")
    parser.add_argument("--csv-only", action="store_true",
                        help="Write the transformed tables to data/processed and stop.")
    args = parser.parse_args(argv)

    print("Transforming raw CMS files ...")
    tables = build_facts()
    for name, frame in tables.items():
        print(f"  {name:20s} {len(frame):>8,} rows")

    if args.csv_only:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        for name, frame in tables.items():
            path = config.PROCESSED_DIR / f"{name}.csv"
            frame.to_csv(path, index=False)
            print(f"  wrote {path.relative_to(config.PROJECT_ROOT)}")
        return 0

    # Imported here so --csv-only works with no database driver installed.
    from .db import apply_all_sql, get_engine, load_dataframe, read_sql

    engine = get_engine()
    if args.reset:
        print("Applying schema and views ...")
        for name in apply_all_sql(engine):
            print(f"  applied {name}")

    print("Loading ...")
    for name in LOAD_ORDER:
        rows = load_dataframe(engine, tables[name], name)
        print(f"  {name:20s} {rows:>8,} rows")

    checks = read_sql(engine, """
        SELECT
          (SELECT COUNT(*) FROM cms.drug_spending)          AS fact_rows,
          (SELECT COUNT(*) FROM cms.v_class_drug_year)      AS class_rows,
          (SELECT ROUND(SUM(total_spending_usd)/1e9, 2) FROM cms.v_spending
            WHERE program = 'Part B' AND data_year =
              (SELECT MAX(data_year) FROM cms.v_spending)) AS part_b_latest_usd_bn
    """)
    print("\nPost-load checks:")
    print(checks.to_string(index=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
