-- =====================================================================
-- 02_views_pricing.sql  |  Price trends, growth, and the Part D
--                          double-count guard
-- =====================================================================
SET search_path TO cms, public;

-- ---------------------------------------------------------------
-- The safe base view. Part D ships an 'Overall' row per drug AND a
-- row per manufacturer; summing the raw table counts multi-source
-- drugs twice. Every view below reads this one, so the filter is
-- applied once and cannot be forgotten.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_spending AS
SELECT *
FROM drug_spending
WHERE program = 'Part B'
   OR manufacturer = 'Overall';

-- The complement: Part D attributed to a named manufacturer. These
-- sum to the 'Overall' row for each drug-year.
CREATE OR REPLACE VIEW v_spending_by_manufacturer AS
SELECT *
FROM drug_spending
WHERE program = 'Part D'
  AND manufacturer <> 'Overall';

-- ---------------------------------------------------------------
-- Drug-year fact with year-over-year movement, computed over the
-- drug's own identifying key.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_drug_year AS
WITH keyed AS (
    SELECT
        s.*,
        COALESCE(s.hcpcs_code, s.brand_name || '|' || COALESCE(s.generic_name, ''))
            AS drug_key
    FROM v_spending s
)
SELECT
    program,
    drug_key,
    data_year,
    hcpcs_code,
    hcpcs_description,
    brand_name,
    generic_name,
    manufacturer_count,
    marketed,
    outlier_flag,
    total_spending_usd,
    total_dosage_units,
    total_claims,
    total_beneficiaries,
    avg_spend_per_dosage_unit,
    avg_spend_per_beneficiary,
    LAG(total_spending_usd) OVER w        AS prior_spending_usd,
    LAG(avg_spend_per_dosage_unit) OVER w AS prior_price_per_unit,
    LAG(total_beneficiaries) OVER w       AS prior_beneficiaries,
    CASE WHEN LAG(total_spending_usd) OVER w > 0
         THEN total_spending_usd / LAG(total_spending_usd) OVER w - 1 END
                                          AS spending_yoy_pct,
    CASE WHEN LAG(avg_spend_per_dosage_unit) OVER w > 0
         THEN avg_spend_per_dosage_unit / LAG(avg_spend_per_dosage_unit) OVER w - 1 END
                                          AS price_yoy_pct,
    CASE WHEN LAG(total_beneficiaries) OVER w > 0
         THEN total_beneficiaries::NUMERIC / LAG(total_beneficiaries) OVER w - 1 END
                                          AS beneficiaries_yoy_pct,
    MIN(data_year) FILTER (WHERE marketed) OVER (PARTITION BY program, drug_key)
                                          AS first_marketed_year
FROM keyed
WINDOW w AS (PARTITION BY program, drug_key ORDER BY data_year);

-- ---------------------------------------------------------------
-- Published price vs implied price.
--
-- For Part B, average spending per dosage unit is exactly spending
-- divided by dosage units. For Part D it is not: CMS publishes a
-- weighted average across the manufacturers of a multi-source drug,
-- so the identity spending = units x price does not hold there.
--
-- This matters because a price/volume decomposition is built on that
-- identity. This view measures the divergence instead of assuming it
-- away, and the bridge below uses the implied price so that its
-- components reconcile by construction in both programs.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_price_measure_divergence AS
SELECT
    program,
    data_year,
    brand_name,
    hcpcs_code,
    avg_spend_per_dosage_unit                          AS published_price,
    total_spending_usd / NULLIF(total_dosage_units, 0) AS implied_price,
    ABS(total_spending_usd / NULLIF(total_dosage_units, 0)
        - avg_spend_per_dosage_unit)
        / NULLIF(ABS(avg_spend_per_dosage_unit), 0)    AS relative_gap
FROM v_drug_year
WHERE marketed
  AND total_dosage_units > 0
  AND avg_spend_per_dosage_unit > 0;

-- ---------------------------------------------------------------
-- Price change decomposition. A change in total spending is volume,
-- price, or both -- and the distinction decides whether a payer is
-- facing a utilization problem or a pricing problem.
--
--   volume = (units_t - units_t-1) * price_t-1
--   price  = (price_t - price_t-1) * units_t-1
--   joint  = (units_t - units_t-1) * (price_t - price_t-1)
--
-- Price here is the implied price (spending / units), not CMS's
-- published average, for the reason given above: only the implied
-- price satisfies the identity the decomposition rests on. The three
-- terms therefore sum exactly to the change in spending, which
-- tests/test_sql_views.py asserts rather than assumes.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_spending_bridge AS
WITH priced AS (
    SELECT
        program, drug_key, data_year, brand_name, hcpcs_code,
        total_spending_usd,
        total_dosage_units,
        total_spending_usd / NULLIF(total_dosage_units, 0) AS implied_price
    FROM v_drug_year
    WHERE marketed
),
lagged AS (
    SELECT
        p.*,
        LAG(total_spending_usd) OVER w AS prior_spending,
        LAG(total_dosage_units) OVER w AS prior_units,
        LAG(implied_price)      OVER w AS prior_price
    FROM priced p
    WINDOW w AS (PARTITION BY program, drug_key ORDER BY data_year)
)
SELECT
    program,
    drug_key,
    data_year,
    brand_name,
    hcpcs_code,
    prior_spending,
    total_spending_usd,
    total_spending_usd - prior_spending                     AS spending_change_usd,
    (total_dosage_units - prior_units) * prior_price        AS volume_effect_usd,
    (implied_price - prior_price) * prior_units             AS price_effect_usd,
    (total_dosage_units - prior_units)
        * (implied_price - prior_price)                     AS joint_effect_usd
FROM lagged
WHERE prior_spending IS NOT NULL
  AND total_spending_usd IS NOT NULL
  AND prior_units > 0;

-- ---------------------------------------------------------------
-- Compound growth over each drug's own marketed span.
--
-- A five-year CAGR is meaningless for a drug that launched in year
-- four, so the span is measured per drug and reported alongside the
-- rate. Drugs CMS flagged as outliers in either endpoint year are
-- marked, not dropped: the caller decides.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_price_cagr AS
WITH bounds AS (
    SELECT
        program, drug_key, brand_name, hcpcs_code,
        MIN(data_year) FILTER (WHERE marketed) AS first_year,
        MAX(data_year) FILTER (WHERE marketed) AS last_year,
        COUNT(*) FILTER (WHERE marketed)       AS marketed_years,
        BOOL_OR(outlier_flag)                  AS any_outlier_year
    FROM v_drug_year
    GROUP BY 1,2,3,4
),
endpoints AS (
    SELECT
        b.*,
        f.avg_spend_per_dosage_unit AS first_price,
        l.avg_spend_per_dosage_unit AS last_price,
        f.total_spending_usd        AS first_spending,
        l.total_spending_usd        AS last_spending
    FROM bounds b
    JOIN v_drug_year f ON f.program = b.program AND f.drug_key = b.drug_key
                      AND f.data_year = b.first_year
    JOIN v_drug_year l ON l.program = b.program AND l.drug_key = b.drug_key
                      AND l.data_year = b.last_year
)
SELECT
    program,
    drug_key,
    brand_name,
    hcpcs_code,
    first_year,
    last_year,
    last_year - first_year AS span_years,
    marketed_years,
    marketed_years = (last_year - first_year + 1) AS continuous,
    any_outlier_year,
    first_price,
    last_price,
    CASE WHEN last_year > first_year AND first_price > 0
         THEN POWER(last_price / first_price,
                    1.0 / (last_year - first_year)) - 1 END AS price_cagr,
    CASE WHEN last_year > first_year AND first_spending > 0
         THEN POWER(last_spending / first_spending,
                    1.0 / (last_year - first_year)) - 1 END AS spending_cagr
FROM endpoints;
