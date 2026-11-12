"""
The SQL is tested against the real CMS data.

Each test states a property that must hold for any release of the
files, so a future CMS release that changes the data surfaces here
rather than in a chart.
"""

from __future__ import annotations

import pytest


def q(warehouse, sql):
    return warehouse.execute(sql).fetchdf()


def test_views_exist(warehouse):
    views = q(warehouse, """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'cms' AND table_type = 'VIEW'
    """)["table_name"].tolist()
    for expected in [
        "v_spending", "v_drug_year", "v_spending_bridge", "v_price_cagr",
        "v_class_drug_year", "v_class_share", "v_class_concentration",
        "v_launch_trajectory", "v_biosimilar_erosion", "v_top_drugs",
    ]:
        assert expected in views, f"missing view {expected}"


def test_base_view_excludes_part_d_manufacturer_rows(warehouse):
    """The double-count guard: v_spending must hold only 'Overall' for Part D."""
    leaked = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_spending
        WHERE program = 'Part D' AND manufacturer <> 'Overall'
    """)["n"].iloc[0]
    assert leaked == 0


def test_manufacturer_rows_sum_to_overall(warehouse):
    """CMS's per-manufacturer rows reconcile to its Overall row.

    This is what proves the 'Overall' filter is a de-duplication and
    not a sampling decision that discards spending.
    """
    gap = q(warehouse, """
        WITH overall AS (
            SELECT data_year, SUM(total_spending_usd) AS total
            FROM cms.v_spending WHERE program = 'Part D' GROUP BY 1
        ),
        by_mftr AS (
            SELECT data_year, SUM(total_spending_usd) AS total
            FROM cms.v_spending_by_manufacturer GROUP BY 1
        )
        SELECT MAX(ABS(o.total - m.total) / o.total) AS max_rel_gap
        FROM overall o JOIN by_mftr m USING (data_year)
    """)["max_rel_gap"].iloc[0]
    assert gap < 0.0001


def test_unmarketed_years_carry_no_measures(warehouse):
    """A blank year must stay blank; a zero would be a launch that
    never happened."""
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.drug_spending
        WHERE NOT marketed AND (
            total_spending_usd IS NOT NULL OR total_dosage_units IS NOT NULL
            OR total_beneficiaries IS NOT NULL
        )
    """)["n"].iloc[0]
    assert bad == 0


def test_spending_bridge_components_reconcile(warehouse):
    """volume + price + joint must equal the change in spending."""
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_spending_bridge
        WHERE volume_effect_usd IS NOT NULL
          AND ABS(spending_change_usd
                  - (volume_effect_usd + price_effect_usd + joint_effect_usd))
              > GREATEST(ABS(spending_change_usd) * 1e-6, 1.0)
    """)["n"].iloc[0]
    assert bad == 0


def test_cagr_span_is_the_drugs_own_marketed_span(warehouse):
    """A drug that launched mid-window must not get a five-year CAGR."""
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_price_cagr
        WHERE span_years > last_year - first_year
           OR marketed_years > span_years + 1
    """)["n"].iloc[0]
    assert bad == 0
    late = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_price_cagr WHERE first_year > 2020
    """)["n"].iloc[0]
    assert late > 0, "expected some products to have entered mid-window"


def test_class_dose_normalization_is_applied(warehouse):
    """cost per dose = per-unit price x billing units in a labeled dose."""
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_class_drug_year
        WHERE cost_per_dose_usd IS NOT NULL
          AND ABS(cost_per_dose_usd
                  - avg_spend_per_dosage_unit * units_per_dose) > 0.01
    """)["n"].iloc[0]
    assert bad == 0


def test_raw_unit_price_and_dose_price_rank_differently(warehouse):
    """The reason the normalization exists.

    If ranking by raw per-unit price and by cost per dose produced the
    same ordering, the whole classes module would be ceremony. They do
    not, and this test fails loudly if a future release makes it so --
    at which point the claim in the README needs revisiting.
    """
    latest = q(warehouse, """
        SELECT display_name, avg_spend_per_dosage_unit, cost_per_dose_usd
        FROM cms.v_class_drug_year
        WHERE marketed AND include_in_price_comparison
          AND data_year = (SELECT MAX(data_year) FROM cms.v_class_drug_year)
    """)
    by_unit = latest.sort_values("avg_spend_per_dosage_unit", ascending=False)
    by_dose = latest.sort_values("cost_per_dose_usd", ascending=False)
    assert by_unit["display_name"].tolist() != by_dose["display_name"].tolist()


def test_class_shares_sum_to_one(warehouse):
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM (
            SELECT data_year, SUM(spending_share) AS s, SUM(beneficiary_share) AS b
            FROM cms.v_class_share GROUP BY 1
        ) t WHERE ABS(s - 1) > 1e-6 OR ABS(b - 1) > 1e-6
    """)["n"].iloc[0]
    assert bad == 0


def test_hhi_is_in_range_and_falls_as_entrants_arrive(warehouse):
    hhi = q(warehouse, """
        SELECT data_year, hhi_spending, products_marketed
        FROM cms.v_class_concentration ORDER BY data_year
    """)
    assert hhi["hhi_spending"].between(0, 10_000).all()
    assert hhi["products_marketed"].is_monotonic_increasing
    assert hhi["hhi_spending"].iloc[-1] < hhi["hhi_spending"].iloc[0]


def test_launch_trajectory_starts_at_year_zero(warehouse):
    bad = q(warehouse, """
        SELECT COUNT(*) AS n FROM cms.v_launch_trajectory
        WHERE years_since_launch < 0
    """)["n"].iloc[0]
    assert bad == 0
    starts = q(warehouse, """
        SELECT display_name, MIN(years_since_launch) AS first
        FROM cms.v_launch_trajectory GROUP BY 1
    """)
    assert (starts["first"] == 0).all()


def test_biosimilar_erosion_has_a_reference_baseline(warehouse):
    rows = q(warehouse, """
        SELECT display_name, entrant_type, years_since_biosimilar_entry,
               cost_per_dose_vs_baseline, beneficiaries_vs_baseline
        FROM cms.v_biosimilar_erosion
        WHERE entrant_type = 'originator' AND years_since_biosimilar_entry > 0
    """)
    assert len(rows) > 0
    # The incumbent must have lost both price and patients after entry.
    assert (rows["cost_per_dose_vs_baseline"] < 0).all()
    assert (rows["beneficiaries_vs_baseline"] < 0).all()


def test_top_drugs_cumulative_share_is_monotonic(warehouse):
    bad = q(warehouse, """
        WITH t AS (
            SELECT program, data_year, spending_rank, cumulative_share,
                   LAG(cumulative_share) OVER (
                       PARTITION BY program, data_year ORDER BY spending_rank
                   ) AS prior
            FROM cms.v_top_drugs
        )
        SELECT COUNT(*) AS n FROM t
        WHERE prior IS NOT NULL AND cumulative_share < prior - 1e-9
    """)["n"].iloc[0]
    assert bad == 0


def test_part_b_price_identity_holds_to_published_precision(warehouse):
    """Part B: spending / units reproduces the published price.

    The tolerance is 1e-4 rather than zero because CMS rounds the
    published per-unit figure; the residual is rounding, not a
    different statistic. Part D, below, is a different statistic.
    """
    worst = q(warehouse, """
        SELECT MAX(relative_gap) AS worst FROM cms.v_price_measure_divergence
        WHERE program = 'Part B'
    """)["worst"].iloc[0]
    assert worst < 1e-4


def test_part_d_published_price_is_a_weighted_average(warehouse):
    """Part D: the identity does NOT hold, by construction.

    CMS publishes a manufacturer-weighted average per dosage unit, so
    spending / units is a different statistic. This test pins that
    down, because any analysis that silently assumes the identity --
    a price/volume bridge, for one -- is wrong for Part D.
    """
    share = q(warehouse, """
        SELECT AVG(CASE WHEN relative_gap > 0.001 THEN 1.0 ELSE 0.0 END) AS diverging
        FROM cms.v_price_measure_divergence WHERE program = 'Part D'
    """)["diverging"].iloc[0]
    assert share > 0.1, (
        "Part D published and implied prices now agree; the weighted-average "
        "caveat in the README may no longer apply."
    )
