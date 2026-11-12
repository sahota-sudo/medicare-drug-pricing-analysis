-- =====================================================================
-- 03_views_market.sql  |  Class-level market structure: share,
--                         concentration, entry, and erosion
-- =====================================================================
SET search_path TO cms, public;

-- ---------------------------------------------------------------
-- A therapeutic class with dose-normalized economics.
--
-- `avg_spend_per_dosage_unit` is denominated in each code's own
-- billing unit, so it is not comparable across drugs. Multiplying by
-- the billing units in one labeled dose puts every product on the
-- same axis: cost per administered dose.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_class_drug_year AS
SELECT
    tc.therapeutic_class,
    tc.display_name,
    tc.entrant_type,
    tc.reference_product,
    tc.modality,
    tc.include_in_class_totals,
    tc.include_in_price_comparison,
    d.hcpcs_code,
    d.brand_name,
    d.data_year,
    d.marketed,
    d.outlier_flag,
    d.total_spending_usd,
    d.total_dosage_units,
    d.total_claims,
    d.total_beneficiaries,
    d.avg_spend_per_dosage_unit,
    tc.labeled_dose_mg,
    tc.billing_unit_mg,
    tc.labeled_dose_mg / tc.billing_unit_mg                AS units_per_dose,
    d.avg_spend_per_dosage_unit * tc.labeled_dose_mg
        / tc.billing_unit_mg                              AS cost_per_dose_usd,
    CASE WHEN d.total_claims > 0
         THEN d.total_dosage_units / d.total_claims END    AS units_per_claim
FROM v_drug_year d
JOIN therapeutic_class tc ON tc.hcpcs_code = d.hcpcs_code
WHERE d.program = 'Part B';

-- ---------------------------------------------------------------
-- Share of class spending and of treated beneficiaries.
--
-- Beneficiary share and spending share answer different questions:
-- the first is adoption, the second is adoption weighted by price.
-- A product can gain patients and lose spending share, which is what
-- a cheaper entrant looks like.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_class_share AS
SELECT
    therapeutic_class,
    data_year,
    display_name,
    entrant_type,
    total_spending_usd,
    total_beneficiaries,
    cost_per_dose_usd,
    SUM(total_spending_usd) OVER (PARTITION BY therapeutic_class, data_year)
        AS class_spending_usd,
    SUM(total_beneficiaries) OVER (PARTITION BY therapeutic_class, data_year)
        AS class_beneficiaries,
    total_spending_usd
        / NULLIF(SUM(total_spending_usd)
                 OVER (PARTITION BY therapeutic_class, data_year), 0)
        AS spending_share,
    total_beneficiaries
        / NULLIF(SUM(total_beneficiaries)
                 OVER (PARTITION BY therapeutic_class, data_year), 0)
        AS beneficiary_share
FROM v_class_drug_year
WHERE marketed AND include_in_class_totals;

-- ---------------------------------------------------------------
-- Class concentration over time.
--
-- HHI on spending share, expressed on the 0-10,000 scale the
-- antitrust agencies use. Reported alongside the leader's share,
-- because a falling HHI driven by one entrant reads differently from
-- one driven by five.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_class_concentration AS
SELECT
    therapeutic_class,
    data_year,
    COUNT(*)                                   AS products_marketed,
    SUM(class_spending_usd) / COUNT(*)         AS class_spending_usd,
    ROUND(SUM(POWER(spending_share * 100, 2))) AS hhi_spending,
    MAX(spending_share)                        AS leader_spending_share,
    SUM(CASE WHEN entrant_type = 'biosimilar' THEN spending_share ELSE 0 END)
                                               AS biosimilar_spending_share
FROM v_class_share
GROUP BY 1, 2;

-- ---------------------------------------------------------------
-- Launch trajectories, indexed to each product's first marketed year.
--
-- Calendar years cannot compare a 2022 launch with a 2024 launch.
-- Years-since-launch can, and it is how a commercial team benchmarks
-- its own uptake against the class.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_launch_trajectory AS
WITH first_year AS (
    SELECT therapeutic_class, hcpcs_code, display_name,
           MIN(data_year) AS launch_year
    FROM v_class_drug_year
    WHERE marketed AND include_in_class_totals
    GROUP BY 1, 2, 3
)
SELECT
    c.therapeutic_class,
    c.display_name,
    c.entrant_type,
    f.launch_year,
    c.data_year,
    c.data_year - f.launch_year AS years_since_launch,
    c.total_spending_usd,
    c.total_beneficiaries,
    c.cost_per_dose_usd,
    SUM(c.total_spending_usd) OVER (
        PARTITION BY c.hcpcs_code ORDER BY c.data_year
    ) AS cumulative_spending_usd
FROM v_class_drug_year c
JOIN first_year f ON f.hcpcs_code = c.hcpcs_code
                 AND f.therapeutic_class = c.therapeutic_class
WHERE c.marketed AND c.include_in_class_totals;

-- ---------------------------------------------------------------
-- Biosimilar erosion: what happened to the reference product.
--
-- Each biosimilar is joined to the originator it references, and both
-- are indexed to the year the first biosimilar for that originator
-- appeared. This is the natural experiment in the dataset -- the one
-- place where the effect of competitive entry on an incumbent's price
-- and volume can be measured rather than assumed.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_biosimilar_erosion AS
WITH entry AS (
    SELECT reference_product, MIN(data_year) AS first_biosimilar_year
    FROM v_class_drug_year
    WHERE entrant_type = 'biosimilar' AND marketed AND include_in_class_totals
      AND reference_product IS NOT NULL
    GROUP BY 1
),
reference_baseline AS (
    SELECT c.hcpcs_code, c.avg_spend_per_dosage_unit AS baseline_price,
           c.cost_per_dose_usd AS baseline_cost_per_dose,
           c.total_spending_usd AS baseline_spending,
           c.total_beneficiaries AS baseline_beneficiaries
    FROM v_class_drug_year c
    JOIN entry e ON e.reference_product = c.hcpcs_code
    WHERE c.data_year = e.first_biosimilar_year - 1
)
SELECT
    c.therapeutic_class,
    c.display_name,
    c.entrant_type,
    COALESCE(c.reference_product, c.hcpcs_code) AS reference_product,
    c.data_year,
    e.first_biosimilar_year,
    c.data_year - e.first_biosimilar_year AS years_since_biosimilar_entry,
    c.cost_per_dose_usd,
    c.total_spending_usd,
    c.total_beneficiaries,
    b.baseline_cost_per_dose,
    b.baseline_spending,
    b.baseline_beneficiaries,
    c.cost_per_dose_usd / NULLIF(b.baseline_cost_per_dose, 0) - 1
        AS cost_per_dose_vs_baseline,
    c.total_spending_usd / NULLIF(b.baseline_spending, 0) - 1
        AS spending_vs_baseline,
    c.total_beneficiaries / NULLIF(b.baseline_beneficiaries, 0) - 1
        AS beneficiaries_vs_baseline
FROM v_class_drug_year c
JOIN entry e ON e.reference_product = COALESCE(c.reference_product, c.hcpcs_code)
LEFT JOIN reference_baseline b ON b.hcpcs_code = e.reference_product
WHERE c.marketed;

-- ---------------------------------------------------------------
-- Program-level context: the largest drugs, and how much of Medicare
-- spending the top handful represent.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_top_drugs AS
SELECT
    program,
    data_year,
    brand_name,
    generic_name,
    hcpcs_code,
    total_spending_usd,
    total_beneficiaries,
    avg_spend_per_beneficiary,
    RANK() OVER (PARTITION BY program, data_year
                 ORDER BY total_spending_usd DESC) AS spending_rank,
    total_spending_usd
        / SUM(total_spending_usd) OVER (PARTITION BY program, data_year)
        AS share_of_program,
    SUM(total_spending_usd) OVER (
        PARTITION BY program, data_year ORDER BY total_spending_usd DESC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) / SUM(total_spending_usd) OVER (PARTITION BY program, data_year)
        AS cumulative_share
FROM v_drug_year
WHERE marketed;
