# Raw data

The two CSVs in this folder are published by the Centers for Medicare &
Medicaid Services as **Medicare Part B Spending by Drug** and **Medicare
Part D Spending by Drug**. They are U.S. government works in the public
domain, and they are committed here (5.6 MB total) so the project runs
immediately after a clone.

| File | Program | Grain | Rows |
|---|---|---|---|
| `DSD_PTB_RY26_P06_V10_DYT24_HCPCS.csv` | Part B | HCPCS code | 799 |
| `DSD_PTD_RY26_P04_V10_DY24_BGM.csv` | Part D | Brand / generic / manufacturer | 14,536 |

Each file is wide: one row per drug with five data years of measures
spread across columns. `src/cms/transform.py` reshapes them to one row
per drug-year.

## Refreshing

CMS republishes annually, adding a year and dropping the oldest. Download
the current annual files from:

https://www.cms.gov/data-research/statistics-trends-and-reports/cms-drug-spending

Drop them in this folder — the loader finds them by the `PTB` / `PTD`
segment in the file name, and the data years are read from the header
rather than hard-coded, so a new release needs no code change. If the
header changes shape, `src/cms/extract.py` raises rather than producing a
shorter trend silently.

## Supporting documents

The methodology PDFs and data dictionaries are worth reading before
changing anything in `transform.py`; they are what the handling of blank
years, outlier flags and the `*` / `**` name markers is based on. They
are not committed here (they are not needed to run anything) but are
linked from the same CMS page.
