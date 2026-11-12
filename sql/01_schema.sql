-- =====================================================================
-- 01_schema.sql  |  Warehouse for CMS Medicare drug spending
-- Target: PostgreSQL 14+
--
-- Source: CMS Medicare Part B and Part D Spending by Drug, annual
--         releases. U.S. government works, public domain.
--
-- Design notes
--  * The CMS files are wide (one row per drug, five years of measures
--    across columns). They are landed long -- one row per drug-year --
--    because every analytical question is a time series and because a
--    new annual release adds a year rather than a schema migration.
--  * `marketed` carries the meaning of a blank year: no Medicare
--    spending, normally because the drug had not launched. It is not
--    a zero, and the column exists so that no query has to guess.
--  * Part B is identified by HCPCS code; Part D has no HCPCS and is
--    identified by brand/generic plus manufacturer. One table holds
--    both, with `program` discriminating, because every pricing view
--    applies to both and duplicating them would guarantee drift.
-- =====================================================================

DROP SCHEMA IF EXISTS cms CASCADE;
CREATE SCHEMA cms;
SET search_path TO cms, public;

CREATE TABLE drug_spending (
    drug_spending_id    BIGSERIAL PRIMARY KEY,
    program             TEXT    NOT NULL CHECK (program IN ('Part B', 'Part D')),
    data_year           INTEGER NOT NULL CHECK (data_year BETWEEN 2000 AND 2100),

    -- Identity. hcpcs_code is Part B only; manufacturer is Part D only.
    hcpcs_code          TEXT,
    hcpcs_description   TEXT,
    brand_name          TEXT,
    generic_name        TEXT,
    manufacturer        TEXT,
    manufacturer_count  INTEGER,

    -- CMS name-ambiguity markers, parsed off the names themselves.
    has_multiple_names  BOOLEAN NOT NULL DEFAULT FALSE,
    name_unavailable    BOOLEAN NOT NULL DEFAULT FALSE,

    -- FALSE means the drug had no Medicare spending in this year.
    marketed            BOOLEAN NOT NULL,

    total_spending_usd          NUMERIC(18,2),
    total_dosage_units          NUMERIC(20,2),
    total_claims                NUMERIC(14,0),
    total_beneficiaries         NUMERIC(14,0),
    avg_spend_per_dosage_unit   NUMERIC(16,6),
    avg_spend_per_claim         NUMERIC(16,4),
    avg_spend_per_beneficiary   NUMERIC(16,4),

    -- CMS sets this when average spending per dosage unit for the
    -- drug-year is substantially affected by outlier records.
    outlier_flag        BOOLEAN,

    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An unmarketed year must carry no measures, and a marketed one
    -- must carry spending. This is the rule the loader enforces; the
    -- database enforces it again so a hand-written INSERT cannot
    -- quietly reintroduce zero-filled rows.
    CONSTRAINT marketed_implies_spending CHECK (
        (marketed AND total_spending_usd IS NOT NULL)
        OR (NOT marketed AND total_spending_usd IS NULL)
    )
);

-- Therapeutic class membership and the dose information CMS does not
-- publish. Kept as data rather than code so the class can be extended
-- without touching a query.
CREATE TABLE therapeutic_class (
    therapeutic_class_id SERIAL PRIMARY KEY,
    therapeutic_class    TEXT    NOT NULL,
    hcpcs_code           TEXT    NOT NULL,
    display_name         TEXT    NOT NULL,
    labeled_dose_mg      NUMERIC(10,4) NOT NULL CHECK (labeled_dose_mg > 0),
    billing_unit_mg      NUMERIC(10,4) CHECK (billing_unit_mg > 0),
    entrant_type         TEXT    NOT NULL
                         CHECK (entrant_type IN ('originator', 'biosimilar',
                                                 'line-extension')),
    reference_product    TEXT,
    modality             TEXT    NOT NULL,
    -- Two different exclusions. A product can be real class volume
    -- without being priced on the same axis (an implant refill), and a
    -- shared code can be neither (bevacizumab is billed mostly for
    -- oncology under the same HCPCS).
    include_in_class_totals     BOOLEAN NOT NULL DEFAULT TRUE,
    include_in_price_comparison BOOLEAN NOT NULL DEFAULT TRUE,
    exclusion_note       TEXT,
    UNIQUE (therapeutic_class, hcpcs_code)
);

-- A drug-year is unique within its identifying grain. A unique INDEX
-- rather than a table constraint, because the discriminating column
-- differs by program and NULLs would otherwise defeat uniqueness --
-- COALESCE is only available to an expression index.
CREATE UNIQUE INDEX uq_drug_year ON drug_spending (
    program, data_year,
    COALESCE(hcpcs_code, ''), COALESCE(brand_name, ''),
    COALESCE(generic_name, ''), COALESCE(manufacturer, '')
);

CREATE INDEX idx_spending_program_year ON drug_spending (program, data_year);
CREATE INDEX idx_spending_hcpcs        ON drug_spending (hcpcs_code);
CREATE INDEX idx_spending_brand        ON drug_spending (brand_name);
CREATE INDEX idx_spending_manufacturer ON drug_spending (manufacturer);
