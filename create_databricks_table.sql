-- =============================================================================
-- SQL SCRIPT: create_databricks_table.sql
-- PURPOSE:
--   Creates the high-performance Delta Lake table in Databricks from the 
--   generated CSV (tda_lcd_claim_decision_asset.csv) and optimizes it with
--   Z-Ordering for sub-second claims adjudication lookups.
-- =============================================================================

-- Step 1: Create Database / Schema
CREATE SCHEMA IF NOT EXISTS gold_coverage;

-- Step 2: Create Delta Table Schema
CREATE TABLE IF NOT EXISTS gold_coverage.tda_lcd_claim_decision_asset (
    -- Article & LCD Policy Metadata
    article_id                      STRING        COMMENT 'Article ID with A prefix (e.g. A52369)',
    article_version                 INT           COMMENT 'Article version number',
    article_type_id                 INT           COMMENT 'Article type identifier (1=Article, 3=SAD, 6=Billing & Coding)',
    article_type_desc               STRING        COMMENT 'Article type description',
    article_title                   STRING        COMMENT 'Cleaned article title',
    related_lcd_id                  STRING        COMMENT 'Linked LCD ID with L prefix (e.g. L33394)',
    related_lcd_version             INT           COMMENT 'Linked LCD version number',
    contractor_ids                  STRING        COMMENT 'Comma-separated contractor IDs',
    contractor_names                STRING        COMMENT 'Pipe-separated MAC business names',
    jurisdiction_states             STRING        COMMENT 'Comma-separated states covered by this policy (e.g. CA,NV,HI)',
    
    -- Procedure / HCPCS Information
    hcpc_code_id                    STRING        COMMENT 'CPT / HCPCS procedure code as formatted by CMS',
    hcpc_code_clean                 STRING        COMMENT 'Cleaned uppercase CPT/HCPCS code',
    hcpc_code_version               INT           COMMENT 'HCPCS code version',
    hcpc_group_id                   INT           COMMENT 'HCPCS group number within the article',
    hcpc_short_desc                 STRING        COMMENT 'Short procedure description',
    hcpc_long_desc                  STRING        COMMENT 'Full procedure description',
    is_hcpc_explicitly_noncovered   INT           COMMENT '1 if HCPCS group narrative states non-coverage, else 0',
    
    -- Diagnosis / ICD-10 Information
    icd10_code_id                   STRING        COMMENT 'ICD-10-CM code with decimals (or ALL)',
    icd10_code_clean                STRING        COMMENT 'Cleaned ICD-10 code without decimals (or ALL)',
    icd10_code_version              INT           COMMENT 'ICD-10-CM code version',
    icd10_group_id                  INT           COMMENT 'ICD-10 group number within the article',
    icd10_desc                      STRING        COMMENT 'Diagnosis description',
    
    -- Coverage Determination & Classification Flags
    coverage_status                 STRING        COMMENT 'COVERED, NON_COVERED, or CONDITIONAL_COVERED',
    is_covered_flag                 INT           COMMENT '1 if covered, else 0',
    is_non_covered_flag             INT           COMMENT '1 if non-covered, else 0',
    is_conditional_flag             INT           COMMENT '1 if conditional (e.g. asterisk condition), else 0',
    adjudication_action             STRING        COMMENT 'PAY, DENY, or PEND_MANUAL_REVIEW',
    denial_reason_code              STRING        COMMENT 'Standard rejection/denial reason code',
    denial_reason_message           STRING        COMMENT 'Human-readable explanation for EOB / denial notice',
    group_pairing_mode              STRING        COMMENT 'Pairing logic used (SINGLE_1TO1, EXACT_GROUP_MATCH, SAD_TABLE_EXCLUSION, etc.)',
    
    -- Clinical Conditions & Constraints
    asterisk_flag                   STRING        COMMENT 'Y if asterisk conditions apply, else N',
    asterisk_explanation            STRING        COMMENT 'Cleaned explanation of the asterisk requirement',
    dual_diagnosis_required         INT           COMMENT '1 if group requires combo diagnosis billing, else 0',
    paired_group_instructions       STRING        COMMENT 'Group introductory directions',
    required_modifiers              STRING        COMMENT 'Pipe-delimited list of required modifiers (e.g. 50|KX)',
    allowed_bill_types              STRING        COMMENT 'Pipe-delimited list of valid UB-04 bill types (e.g. 011|013)',
    allowed_revenue_codes           STRING        COMMENT 'Pipe-delimited list of valid Revenue codes (e.g. 0490)',
    
    -- Temporal Boundaries
    effective_start_date            DATE          COMMENT 'Date rule becomes active for Claim Date of Service',
    effective_end_date              DATE          COMMENT 'Date rule expires or was retired',
    source_table                    STRING        COMMENT 'Source table name'
)
USING DELTA
COMMENT 'Master CMS MCD LCD & Article Decision Asset for 100% Claim Adjudication';

-- Step 3: Ingest the CSV into the Delta Table
-- (Change the path to your DBFS, ADLS, or S3 volume path where you upload the CSV)
COPY INTO gold_coverage.tda_lcd_claim_decision_asset
FROM (
    SELECT 
        article_id,
        CAST(article_version AS INT),
        CAST(article_type_id AS INT),
        article_type_desc,
        article_title,
        related_lcd_id,
        CAST(related_lcd_version AS INT),
        contractor_ids,
        contractor_names,
        jurisdiction_states,
        hcpc_code_id,
        hcpc_code_clean,
        CAST(hcpc_code_version AS INT),
        CAST(hcpc_group_id AS INT),
        hcpc_short_desc,
        hcpc_long_desc,
        CAST(is_hcpc_explicitly_noncovered AS INT),
        icd10_code_id,
        icd10_code_clean,
        CAST(icd10_code_version AS INT),
        CAST(icd10_group_id AS INT),
        icd10_desc,
        coverage_status,
        CAST(is_covered_flag AS INT),
        CAST(is_non_covered_flag AS INT),
        CAST(is_conditional_flag AS INT),
        adjudication_action,
        denial_reason_code,
        denial_reason_message,
        group_pairing_mode,
        asterisk_flag,
        asterisk_explanation,
        CAST(dual_diagnosis_required AS INT),
        paired_group_instructions,
        required_modifiers,
        allowed_bill_types,
        allowed_revenue_codes,
        TO_DATE(effective_start_date, 'yyyy-MM-dd'),
        TO_DATE(effective_end_date, 'yyyy-MM-dd'),
        source_table
    FROM '/FileStore/tables/tda_lcd_claim_decision_asset.csv'
)
FILEFORMAT = CSV
FORMAT_OPTIONS (
    'header' = 'true',
    'inferSchema' = 'false',
    'quote' = '"',
    'escape' = '"',
    'multiLine' = 'true'
)
COPY_OPTIONS (
    'mergeSchema' = 'true'
);

-- Step 4: Optimize Delta Layout for Sub-Second Claim Adjudication
OPTIMIZE gold_coverage.tda_lcd_claim_decision_asset
ZORDER BY (hcpc_code_clean, icd10_code_clean, article_id);
