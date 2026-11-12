"""
The analysis: retinal anti-VEGF market structure, 2020-2024.

Run:
    python -m cms.analysis             # from the raw files
    python -m cms.analysis --from-db   # from the PostgreSQL views

Writes docs/findings.md, docs/data_quality.md and docs/charts/. Every
figure in the written output is computed here; none is typed in.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import config
from .classes import attach_class, class_members, price_comparable
from .extract import find_raw_files, read_part_b, read_part_d
from .pipeline import build_analysis_frame
from .quality import profile, to_markdown
from .transform import to_long

PALETTE = {
    "originator": "#1f4e5f",
    "biosimilar": "#c9711f",
    "line-extension": "#4a6fa5",
    "grid": "#8a8d91",
    "down": "#a4453a",
    "up": "#3c7a5e",
}
SERIES_COLORS = [
    "#1f4e5f", "#c9711f", "#4a6fa5", "#8a5a83",
    "#3c7a5e", "#a4453a", "#7a7246", "#55606b",
]


def _style(ax, title: str, ylabel: str = "", subtitle: str = "") -> None:
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left", pad=18 if subtitle else 12)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8.5,
                color="#55606b", va="bottom")
    ax.set_ylabel(ylabel, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25, linewidth=0.7)
    ax.tick_params(labelsize=8)


def _save(fig, name: str) -> Path:
    config.CHART_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CHART_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------

def chart_price_axis_comparison(latest: pd.DataFrame) -> Path:
    """The normalization, shown side by side.

    Left: the column as published. Right: the same drugs on a common
    dose axis. The reordering is the argument for the whole module.
    """
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    raw = latest.sort_values("avg_spend_per_dosage_unit")
    axes[0].barh(raw["display_name"], raw["avg_spend_per_dosage_unit"],
                 color=PALETTE["grid"])
    _style(axes[0], "As published: spending per dosage unit",
           "$ per billing unit", "Billing units differ by code — not comparable")
    for i, v in enumerate(raw["avg_spend_per_dosage_unit"]):
        axes[0].text(v, i, f" {v:,.0f}", va="center", fontsize=8)

    dose = latest.sort_values("cost_per_dose_usd")
    colors = [PALETTE[t] for t in dose["entrant_type"]]
    axes[1].barh(dose["display_name"], dose["cost_per_dose_usd"], color=colors)
    _style(axes[1], "Normalized: cost per administered dose",
           "$ per labeled dose", "Per-unit price × billing units in one dose")
    for i, v in enumerate(dose["cost_per_dose_usd"]):
        axes[1].text(v, i, f" {v:,.0f}", va="center", fontsize=8)
    return _save(fig, "01_price_axis_comparison.png")


def chart_class_spending(share: pd.DataFrame) -> Path:
    pivot = (share.pivot_table(index="data_year", columns="display_name",
                               values="total_spending_usd", aggfunc="sum")
             .fillna(0) / 1e9)
    order = pivot.sum().sort_values(ascending=False).index
    pivot = pivot[order]
    fig, ax = plt.subplots(figsize=(9, 5))
    bottom = np.zeros(len(pivot))
    for i, column in enumerate(pivot.columns):
        ax.bar(pivot.index.astype(str), pivot[column], bottom=bottom,
               color=SERIES_COLORS[i % len(SERIES_COLORS)], label=column, width=0.6)
        bottom += pivot[column].to_numpy()
    _style(ax, "Medicare Part B spending on retinal anti-VEGF therapies",
           "$ billions", "Class total shown above each bar")
    for x, total in zip(range(len(pivot)), bottom):
        ax.text(x, total * 1.01, f"${total:,.2f}B", ha="center", fontsize=8.5)
    ax.legend(frameon=False, fontsize=8, ncols=2)
    ax.set_ylim(0, bottom.max() * 1.15)
    return _save(fig, "02_class_spending.png")


def chart_share_shift(share: pd.DataFrame) -> Path:
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, (name, group) in enumerate(share.groupby("display_name")):
        group = group.sort_values("data_year")
        ax.plot(group["data_year"], group["beneficiary_share"] * 100,
                marker="o", markersize=4, linewidth=2,
                color=SERIES_COLORS[i % len(SERIES_COLORS)], label=name)
    _style(ax, "Share of treated beneficiaries", "% of class beneficiaries",
           "Adoption, unweighted by price")
    ax.legend(frameon=False, fontsize=8, ncols=2)
    ax.set_xticks(sorted(share["data_year"].unique()))
    return _save(fig, "03_beneficiary_share.png")


def chart_launch_curves(class_long: pd.DataFrame) -> Path:
    """Entrants indexed to their own first year, not the calendar."""
    marketed = class_members(class_long)
    marketed = marketed[marketed["marketed"]].copy()
    first = marketed.groupby("display_name")["data_year"].transform("min")
    marketed["years_since_launch"] = marketed["data_year"] - first
    entrants = marketed[first > marketed["data_year"].min()]

    fig, ax = plt.subplots(figsize=(9, 5))
    for i, (name, group) in enumerate(entrants.groupby("display_name")):
        group = group.sort_values("years_since_launch")
        ax.plot(group["years_since_launch"], group["total_beneficiaries"] / 1e3,
                marker="o", markersize=5, linewidth=2,
                color=SERIES_COLORS[i % len(SERIES_COLORS)], label=name)
    _style(ax, "Launch trajectories, indexed to first year of Medicare spending",
           "Beneficiaries treated (thousands)",
           "Calendar years cannot compare a 2022 entrant with a 2024 one")
    ax.set_xlabel("Years since first Medicare spending", fontsize=9)
    ax.set_xticks(sorted(entrants["years_since_launch"].unique()))
    ax.legend(frameon=False, fontsize=8)
    return _save(fig, "04_launch_trajectories.png")


def chart_biosimilar_erosion(class_long: pd.DataFrame, reference: str) -> Path:
    """The incumbent and its biosimilars, on cost per dose."""
    members = class_long[
        (class_long["hcpcs_code"] == reference)
        | (class_long["reference_product"] == reference)
    ]
    members = members[members["marketed"] & members["include_in_price_comparison"]]

    fig, ax = plt.subplots(figsize=(9, 5))
    for i, (name, group) in enumerate(members.groupby("display_name")):
        group = group.sort_values("data_year")
        style = "--" if group["entrant_type"].iloc[0] == "biosimilar" else "-"
        ax.plot(group["data_year"], group["cost_per_dose_usd"], linestyle=style,
                marker="o", markersize=5, linewidth=2,
                color=SERIES_COLORS[i % len(SERIES_COLORS)], label=name)

    entry_year = members.loc[members["entrant_type"] == "biosimilar",
                             "data_year"].min()
    if pd.notna(entry_year):
        ax.axvline(entry_year, color=PALETTE["down"], linewidth=1.2, linestyle=":")
        ax.text(entry_year, ax.get_ylim()[1] * 0.97, " first biosimilar",
                color=PALETTE["down"], fontsize=8, va="top")
    _style(ax, "Cost per administered dose: originator and its biosimilars",
           "$ per labeled dose", "Dashed = biosimilar")
    ax.set_xticks(sorted(members["data_year"].unique()))
    ax.legend(frameon=False, fontsize=8)
    return _save(fig, "05_biosimilar_erosion.png")


def chart_spending_bridge(bridge: pd.DataFrame, year: int) -> Path:
    """Why class spending moved: volume or price."""
    top = bridge.reindex(bridge["spending_change_usd"].abs()
                         .sort_values(ascending=False).index).head(7)
    top = top.iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.barh(top["display_name"], top["volume_effect_usd"] / 1e6,
            color=PALETTE["originator"], label="Volume effect")
    ax.barh(top["display_name"], top["price_effect_usd"] / 1e6,
            left=top["volume_effect_usd"] / 1e6,
            color=PALETTE["biosimilar"], label="Price effect")
    ax.barh(top["display_name"], top["joint_effect_usd"] / 1e6,
            left=(top["volume_effect_usd"] + top["price_effect_usd"]) / 1e6,
            color=PALETTE["grid"], label="Joint")
    ax.axvline(0, color="#333", linewidth=0.8)
    _style(ax, f"What moved spending in {year}", "$ millions vs prior year",
           "Volume, price and their interaction sum to the total change")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    return _save(fig, "06_spending_bridge.png")


def chart_concentration(concentration: pd.DataFrame) -> Path:
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(concentration["data_year"].astype(str), concentration["hhi_spending"],
           color=PALETTE["originator"], width=0.55)
    _style(ax, "Class concentration (HHI on spending share)", "HHI, 0–10,000 scale",
           "Above 2,500 is 'highly concentrated' under the DOJ/FTC merger guidelines")
    ax.axhline(2500, color=PALETTE["down"], linestyle="--", linewidth=1.2)
    ax.text(0, 2560, "highly concentrated threshold", fontsize=8,
            color=PALETTE["down"])
    for x, (hhi, n) in enumerate(zip(concentration["hhi_spending"],
                                     concentration["products_marketed"])):
        ax.text(x, hhi * 1.01, f"{hhi:,.0f}\n{n} products", ha="center", fontsize=8)
    ax.set_ylim(0, concentration["hhi_spending"].max() * 1.2)
    return _save(fig, "07_class_concentration.png")


# ---------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------

def class_share(class_long: pd.DataFrame) -> pd.DataFrame:
    """Share of the class, over products that are genuinely in it."""
    marketed = class_members(class_long)
    marketed = marketed[marketed["marketed"]].copy()
    totals = marketed.groupby("data_year")[["total_spending_usd",
                                            "total_beneficiaries"]].transform("sum")
    marketed["spending_share"] = marketed["total_spending_usd"] / totals["total_spending_usd"]
    marketed["beneficiary_share"] = (
        marketed["total_beneficiaries"] / totals["total_beneficiaries"]
    )
    return marketed


def concentration(share: pd.DataFrame) -> pd.DataFrame:
    return (
        share.groupby("data_year")
        .apply(lambda g: pd.Series({
            "products_marketed": g["display_name"].nunique(),
            "class_spending_usd": g["total_spending_usd"].sum(),
            "hhi_spending": float((g["spending_share"] * 100).pow(2).sum()),
            "leader_spending_share": g["spending_share"].max(),
            "leader": g.loc[g["spending_share"].idxmax(), "display_name"],
        }), include_groups=False)
        .reset_index()
    )


def spending_bridge(class_long: pd.DataFrame, year: int) -> pd.DataFrame:
    """Volume/price decomposition on the implied price.

    Implied price, not the published column, because only
    spending / units satisfies the identity the decomposition rests on.
    """
    frame = class_members(class_long)
    frame = frame[frame["marketed"]].copy()
    frame["implied_price"] = frame["total_spending_usd"] / frame["total_dosage_units"]
    frame = frame.sort_values(["hcpcs_code", "data_year"])
    grouped = frame.groupby("hcpcs_code")
    frame["prior_units"] = grouped["total_dosage_units"].shift(1)
    frame["prior_price"] = grouped["implied_price"].shift(1)
    frame["prior_spending"] = grouped["total_spending_usd"].shift(1)

    current = frame[(frame["data_year"] == year) & frame["prior_units"].notna()].copy()
    current["spending_change_usd"] = (
        current["total_spending_usd"] - current["prior_spending"]
    )
    current["volume_effect_usd"] = (
        (current["total_dosage_units"] - current["prior_units"]) * current["prior_price"]
    )
    current["price_effect_usd"] = (
        (current["implied_price"] - current["prior_price"]) * current["prior_units"]
    )
    current["joint_effect_usd"] = (
        (current["total_dosage_units"] - current["prior_units"])
        * (current["implied_price"] - current["prior_price"])
    )
    return current


def erosion_summary(class_long: pd.DataFrame, reference: str) -> dict:
    """What happened to the incumbent after biosimilar entry."""
    members = class_long[
        (class_long["hcpcs_code"] == reference)
        | (class_long["reference_product"] == reference)
    ]
    members = members[members["marketed"]]
    entry_year = members.loc[members["entrant_type"] == "biosimilar", "data_year"].min()
    incumbent = members[members["hcpcs_code"] == reference].set_index("data_year")
    baseline_year = entry_year - 1
    latest_year = incumbent.index.max()
    if baseline_year not in incumbent.index:
        return {}

    biosimilars = members[members["entrant_type"] == "biosimilar"]
    latest_biosimilars = biosimilars[biosimilars["data_year"] == latest_year]

    return {
        "reference_name": incumbent["display_name"].iloc[0],
        "entry_year": int(entry_year),
        "baseline_year": int(baseline_year),
        "latest_year": int(latest_year),
        "baseline_cost_per_dose": float(incumbent.loc[baseline_year, "cost_per_dose_usd"]),
        "latest_cost_per_dose": float(incumbent.loc[latest_year, "cost_per_dose_usd"]),
        "cost_per_dose_change": float(
            incumbent.loc[latest_year, "cost_per_dose_usd"]
            / incumbent.loc[baseline_year, "cost_per_dose_usd"] - 1
        ),
        "baseline_beneficiaries": float(incumbent.loc[baseline_year, "total_beneficiaries"]),
        "latest_beneficiaries": float(incumbent.loc[latest_year, "total_beneficiaries"]),
        "beneficiary_change": float(
            incumbent.loc[latest_year, "total_beneficiaries"]
            / incumbent.loc[baseline_year, "total_beneficiaries"] - 1
        ),
        "spending_change": float(
            incumbent.loc[latest_year, "total_spending_usd"]
            / incumbent.loc[baseline_year, "total_spending_usd"] - 1
        ),
        "biosimilar_count": int(latest_biosimilars["display_name"].nunique()),
        "biosimilars_above_originator": int(
            (latest_biosimilars["cost_per_dose_usd"]
             > incumbent.loc[latest_year, "cost_per_dose_usd"]).sum()
        ),
        "biosimilar_beneficiary_share": float(
            latest_biosimilars["total_beneficiaries"].sum()
            / (latest_biosimilars["total_beneficiaries"].sum()
               + incumbent.loc[latest_year, "total_beneficiaries"])
        ),
    }


# ---------------------------------------------------------------------
# Written output
# ---------------------------------------------------------------------

def _pct(value: float) -> str:
    return f"{value:+.1%}"


def _usd_m(value: float) -> str:
    """Finance convention: negatives in parentheses, not with a minus."""
    return f"(${abs(value)/1e6:,.0f}M)" if value < 0 else f"${value/1e6:,.0f}M"


def write_findings(
    class_long: pd.DataFrame,
    share: pd.DataFrame,
    conc: pd.DataFrame,
    bridge: pd.DataFrame,
    erosion: dict,
    latest: pd.DataFrame,
    charts: list[Path],
    years: tuple[int, ...],
) -> Path:
    latest_year = years[-1]
    first_year = years[0]

    class_first = share[share["data_year"] == first_year]["total_spending_usd"].sum()
    class_latest = share[share["data_year"] == latest_year]["total_spending_usd"].sum()

    by_dose = latest.sort_values("cost_per_dose_usd", ascending=False)
    by_unit = latest.sort_values("avg_spend_per_dosage_unit", ascending=False)

    entrants = (
        class_members(class_long).loc[lambda d: d["marketed"]]
        .groupby("display_name")["data_year"].min()
        .loc[lambda s: s > first_year].sort_values()
    )

    biggest_gain = bridge.loc[bridge["spending_change_usd"].idxmax()]
    biggest_loss = bridge.loc[bridge["spending_change_usd"].idxmin()]

    conc_first = conc.iloc[0]
    conc_last = conc.iloc[-1]

    lines = [
        f"# Retinal anti-VEGF therapies in Medicare Part B, {first_year}–{latest_year}",
        "",
        "Generated by `python -m cms.analysis`. Every figure is computed from the",
        "CMS annual files; none is typed in.",
        "",
        "## Headline",
        "",
        f"Class spending moved from **${class_first/1e9:,.2f}B** in {first_year} to "
        f"**${class_latest/1e9:,.2f}B** in {latest_year} "
        f"(**{_pct(class_latest/class_first - 1)}**), while the number of products "
        f"billing Medicare rose from **{int(conc_first['products_marketed'])}** to "
        f"**{int(conc_last['products_marketed'])}**.",
        "",
        "## 1. The published price column is not a price",
        "",
        "CMS reports average spending per *dosage unit*, and for Part B a dosage unit",
        "is the HCPCS billing unit — which is defined per code. Within this class one",
        "code bills per 1 mg and another per 0.1 mg, so the published figures are in",
        "different units and cannot be compared to each other.",
        "",
        f"On the raw column, **{by_unit['display_name'].iloc[0]}** looks like the most",
        f"expensive product in {latest_year} at "
        f"**${by_unit['avg_spend_per_dosage_unit'].iloc[0]:,.0f}** per unit and "
        f"**{by_unit['display_name'].iloc[-1]}** the cheapest at "
        f"**${by_unit['avg_spend_per_dosage_unit'].iloc[-1]:,.0f}**.",
        "",
        "Multiplying by the billing units in one labeled dose puts them on a common",
        "axis — cost per administered dose — and reorders them:",
        "",
        "| Product | Type | Published $/unit | Units per dose | $ per dose |",
        "|---|---|---|---|---|",
    ]
    for _, row in by_dose.iterrows():
        lines.append(
            f"| {row['display_name']} | {row['entrant_type']} | "
            f"${row['avg_spend_per_dosage_unit']:,.2f} | "
            f"{row['units_per_dose']:,.1f} | **${row['cost_per_dose_usd']:,.0f}** |"
        )

    lines += [
        "",
        f"The spread between the most and least expensive product narrows from "
        f"**{by_unit['avg_spend_per_dosage_unit'].iloc[0] / by_unit['avg_spend_per_dosage_unit'].iloc[-1]:,.1f}×** "
        f"on the raw column to "
        f"**{by_dose['cost_per_dose_usd'].iloc[0] / by_dose['cost_per_dose_usd'].iloc[-1]:,.1f}×** "
        "per dose. Any procurement or market-access conclusion drawn from the raw",
        "column is drawn from the wrong number.",
        "",
        "## 2. Competitive entry reset the class",
        "",
    ]
    if len(entrants):
        lines.append("Products that began billing Medicare after "
                     f"{first_year}:")
        lines.append("")
        for name, year in entrants.items():
            lines.append(f"- **{name}** — first Medicare spending in {int(year)}")
        lines.append("")

    lines += [
        f"Concentration fell from an HHI of **{conc_first['hhi_spending']:,.0f}** in "
        f"{first_year} to **{conc_last['hhi_spending']:,.0f}** in {latest_year}. The "
        f"leader's share of class spending went from "
        f"**{conc_first['leader_spending_share']:.0%}** "
        f"({conc_first['leader']}) to **{conc_last['leader_spending_share']:.0%}** "
        f"({conc_last['leader']}).",
        "",
        "Both readings still sit above the 2,500 threshold the DOJ/FTC merger",
        "guidelines treat as highly concentrated — this is a class that became more",
        "competitive without becoming competitive.",
        "",
        f"## 3. What moved spending in {latest_year}",
        "",
        "Decomposing each product's change in spending into volume, price and their",
        "interaction (on the implied price, spending ÷ units, which is the only",
        "measure that satisfies the identity the decomposition rests on):",
        "",
        f"- Largest increase: **{biggest_gain['display_name']}**, "
        f"**{_usd_m(biggest_gain['spending_change_usd'])}**, of which "
        f"**{_usd_m(biggest_gain['volume_effect_usd'])}** is volume and "
        f"**{_usd_m(biggest_gain['price_effect_usd'])}** is price.",
        f"- Largest decrease: **{biggest_loss['display_name']}**, "
        f"**{_usd_m(biggest_loss['spending_change_usd'])}**, of which "
        f"**{_usd_m(biggest_loss['volume_effect_usd'])}** is volume and "
        f"**{_usd_m(biggest_loss['price_effect_usd'])}** is price.",
        "",
        "The distinction matters: a decline driven by volume is a share problem, and",
        "one driven by price is a margin problem. They call for different responses",
        "and they are not visible in a spending total.",
        "",
    ]

    if erosion:
        lines += [
            "## 4. What biosimilar entry did to the incumbent",
            "",
            f"Biosimilars referencing **{erosion['reference_name']}** first billed "
            f"Medicare in **{erosion['entry_year']}**. Measured against "
            f"{erosion['baseline_year']}, the last full year before entry, by "
            f"{erosion['latest_year']} the originator's:",
            "",
            f"- cost per administered dose fell "
            f"**{_pct(erosion['cost_per_dose_change'])}** "
            f"(${erosion['baseline_cost_per_dose']:,.0f} → "
            f"${erosion['latest_cost_per_dose']:,.0f})",
            f"- treated beneficiaries fell **{_pct(erosion['beneficiary_change'])}** "
            f"({erosion['baseline_beneficiaries']:,.0f} → "
            f"{erosion['latest_beneficiaries']:,.0f})",
            f"- total spending fell **{_pct(erosion['spending_change'])}**",
            "",
            "The counterintuitive part is where the biosimilars ended up. Of the "
            f"**{erosion['biosimilar_count']}** biosimilars on the market in "
            f"{erosion['latest_year']}, "
            f"**{erosion['biosimilars_above_originator']}** cost *more* per "
            "administered dose than the originator they were meant to undercut — "
            "the incumbent cut price faster than its challengers did, and still lost "
            f"**{erosion['biosimilar_beneficiary_share']:.0%}** of the molecule's "
            "treated beneficiaries to them.",
            "",
            "For anyone modeling a launch into a class like this, that is the number",
            "worth carrying: price defence did not retain volume.",
            "",
        ]

    lines += ["## Charts", ""]
    for path in charts:
        lines.append(f"![{path.stem}](charts/{path.name})")
        lines.append("")

    lines += [
        "---",
        "",
        "Source: CMS Medicare Part B and Part D Spending by Drug, annual releases.",
        "U.S. government works, public domain. Labeled doses are from the products'",
        "FDA prescribing information and are listed in `src/cms/classes.py`.",
        "",
        "Dosing interval is deliberately not modeled, so cost per dose is measured",
        "and cost per patient-year is not: label intervals differ across these",
        "products and real-world treat-and-extend practice differs from the label.",
        "Asserting an interval would convert a measured price into an assumed one.",
    ]

    config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DOCS_DIR / "findings.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the CMS drug pricing analysis.")
    parser.add_argument("--from-db", action="store_true",
                        help="Read the PostgreSQL views instead of the raw files.")
    args = parser.parse_args(argv)

    if args.from_db:
        from .db import get_engine, read_sql

        engine = get_engine()
        class_long = read_sql(engine, "SELECT * FROM cms.v_class_drug_year")
        class_long["reference_product"] = class_long["reference_product"].astype("object")
    else:
        print("Reading CMS files ...")
        part_b = build_analysis_frame()
        class_long = attach_class(part_b)

    years = tuple(sorted(class_long["data_year"].unique()))
    latest_year = years[-1]

    print("Computing ...")
    share = class_share(class_long)
    conc = concentration(share)
    bridge = spending_bridge(class_long, latest_year)
    comparable = price_comparable(class_long)
    latest = comparable[comparable["data_year"] == latest_year]
    erosion = erosion_summary(class_long, reference="J2778")

    print("Building charts ...")
    charts = [
        chart_price_axis_comparison(latest),
        chart_class_spending(share),
        chart_share_shift(share),
        chart_launch_curves(class_long),
        chart_biosimilar_erosion(class_long, reference="J2778"),
        chart_spending_bridge(bridge, latest_year),
        chart_concentration(conc),
    ]

    findings = write_findings(class_long, share, conc, bridge, erosion, latest,
                              charts, years)

    print("Profiling data quality ...")
    files = find_raw_files(config.RAW_DIR)
    profiles = {}
    if "Part B" in files:
        profiles["Part B"] = profile(to_long(read_part_b(files["Part B"])))
    if "Part D" in files:
        profiles["Part D"] = profile(to_long(read_part_d(files["Part D"])))
    quality_path = config.DOCS_DIR / "data_quality.md"
    quality_path.write_text(to_markdown(profiles), encoding="utf-8")

    print(f"\nWrote {findings.name}, {quality_path.name} and {len(charts)} charts.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
