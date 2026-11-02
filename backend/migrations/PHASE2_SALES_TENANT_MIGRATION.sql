-- InsightIQ Phase 2 sales tenant migration
-- REVIEW ONLY: do not execute without a verified backup and scratch rehearsal.
--
-- This file is intended for a database that has already passed the scratch
-- rehearsal. It does not contain a database-selection statement. The operator
-- must verify the target database before running any statement.
--
-- Preconditions:
-- 1. The target is the intended database and is not the original database
--    during scratch testing.
-- 2. PHASE1_AUTH_SCHEMA_DRAFT.sql has been reviewed and applied, so
--    businesses(business_id) exists.
-- 3. PHASE2_SALES_TENANT_SCRATCH_TEST.sql created and populated
--    pre_migration_sales_inventory in the same database.
-- 4. The verified baseline and every pre-migration gate below match.
--
-- MySQL DDL statements cause implicit commits. The nullable-column ALTER,
-- business creation, and final ALTER are therefore not one rollback-able
-- transaction. Rollback requires a separately reviewed maintenance procedure
-- and an independent snapshot; do not rely on ROLLBACK to undo this file.

-- ============================================================================
-- PHASE A: PRE-MIGRATION CHECKS - STOP MANUALLY ON ANY MISMATCH
-- ============================================================================
-- Baseline meanings: revenue is SUM(revenue) = 1479650.00; gross sales is
-- SUM(quantity * unit_price) = 1484650.00; discounts are
-- SUM(discount_amount) = 5000.00.

SELECT DATABASE() AS target_database;

SELECT COUNT(*) AS total_transactions,
       SUM(revenue) AS total_revenue,
       SUM(quantity * unit_price) AS gross_sales,
       SUM(discount_amount) AS total_discounts,
       SUM(quantity) AS total_quantity,
       AVG(revenue) AS average_revenue,
       MIN(transaction_id) AS minimum_transaction_id,
       MAX(transaction_id) AS maximum_transaction_id,
       MIN(order_date) AS minimum_order_date,
       MAX(order_date) AS maximum_order_date,
       CASE
           WHEN COUNT(*) = 50
            AND SUM(revenue) = 1479650.00
            AND SUM(quantity * unit_price) = 1484650.00
            AND SUM(discount_amount) = 5000.00
            AND SUM(quantity) = 652
            AND MIN(transaction_id) = 'TXN0001'
            AND MAX(transaction_id) = 'TXN1002'
            AND MIN(order_date) = '2026-01-03'
            AND MAX(order_date) = '2026-07-05'
           THEN 'PASS'
           ELSE 'STOP - baseline mismatch'
       END AS mandatory_baseline_gate
FROM sales_transactions;

SELECT COUNT(*) AS inventory_rows,
       CASE
           WHEN COUNT(*) = 50
            AND SUM(revenue) = 1479650.00
            AND SUM(quantity * unit_price) = 1484650.00
            AND SUM(discount_amount) = 5000.00
            AND SUM(quantity) = 652
           THEN 'PASS'
           ELSE 'STOP - inventory mismatch'
       END AS mandatory_inventory_gate
FROM pre_migration_sales_inventory;

SELECT transaction_id, COUNT(*) AS row_count
FROM sales_transactions
GROUP BY transaction_id
HAVING COUNT(*) <> 1;

SELECT transaction_id, COUNT(*) AS row_count
FROM pre_migration_sales_inventory
GROUP BY transaction_id
HAVING COUNT(*) <> 1;

-- STOP NOW unless target_database, both mandatory gates, and both duplicate
-- checks are correct. Do not continue by running the next section in a batch.

-- ============================================================================
-- PHASE B: LEGACY BUSINESS - CREATE/RESOLVE, THEN STOP FOR IDENTITY CHECK
-- ============================================================================

INSERT INTO businesses (name, slug, status)
VALUES ('InsightIQ Legacy Workspace', 'insightiq-legacy', 'active')
ON DUPLICATE KEY UPDATE business_id = LAST_INSERT_ID(business_id);

SELECT COUNT(*) INTO @legacy_slug_count
FROM businesses
WHERE slug = 'insightiq-legacy';

SELECT COUNT(*) INTO @legacy_business_match_count
FROM businesses
WHERE slug = 'insightiq-legacy'
  AND name = 'InsightIQ Legacy Workspace'
  AND status = 'active';

SELECT business_id
INTO @legacy_business_id
FROM businesses
WHERE slug = 'insightiq-legacy'
  AND name = 'InsightIQ Legacy Workspace'
  AND status = 'active'
LIMIT 1;

SELECT @legacy_business_id AS legacy_business_id,
       @legacy_slug_count AS legacy_slug_count,
       @legacy_business_match_count AS legacy_business_match_count,
       CASE
           WHEN @legacy_business_id IS NOT NULL
            AND @legacy_slug_count = 1
            AND @legacy_business_match_count = 1
           THEN 'PASS'
           ELSE 'STOP - invalid legacy business identity'
       END AS mandatory_business_gate;

-- STOP NOW unless the business gate is PASS. The ID must be non-null, the
-- slug must occur exactly once, and its name/status must match exactly.

-- ============================================================================
-- PHASE C: NULLABLE COLUMN AND BACKFILL
-- ============================================================================

ALTER TABLE sales_transactions
    ADD COLUMN business_id BIGINT UNSIGNED NULL
    AFTER transaction_id;

START TRANSACTION;

UPDATE sales_transactions
SET business_id = @legacy_business_id
WHERE business_id IS NULL;

COMMIT;

-- ============================================================================
-- PHASE D: POST-BACKFILL VERIFICATION - STOP MANUALLY ON ANY MISMATCH
-- ============================================================================

SELECT @legacy_business_id AS legacy_business_id;

SELECT COUNT(*) AS null_business_ids
FROM sales_transactions
WHERE business_id IS NULL;

SELECT COUNT(*) AS orphaned_transactions
FROM sales_transactions AS st
LEFT JOIN businesses AS b ON b.business_id = st.business_id
WHERE b.business_id IS NULL;

SELECT COUNT(*) AS wrong_business_owner_rows
FROM sales_transactions
WHERE business_id <> @legacy_business_id
   OR business_id IS NULL;

SELECT COUNT(*) AS legacy_transaction_count,
       SUM(revenue) AS legacy_revenue,
       SUM(quantity * unit_price) AS legacy_gross_sales,
       SUM(discount_amount) AS legacy_discounts,
       SUM(quantity) AS legacy_quantity,
       MIN(order_date) AS legacy_first_order_date,
       MAX(order_date) AS legacy_last_order_date,
       CASE
           WHEN COUNT(*) = 50
            AND SUM(revenue) = 1479650.00
            AND SUM(quantity * unit_price) = 1484650.00
            AND SUM(discount_amount) = 5000.00
            AND SUM(quantity) = 652
            AND MIN(order_date) = '2026-01-03'
            AND MAX(order_date) = '2026-07-05'
           THEN 'PASS'
           ELSE 'STOP - legacy aggregate mismatch'
       END AS mandatory_legacy_aggregate_gate
FROM sales_transactions
WHERE business_id = @legacy_business_id;

SELECT COUNT(*) AS duplicate_transaction_ids
FROM (
    SELECT transaction_id
    FROM sales_transactions
    GROUP BY transaction_id
    HAVING COUNT(*) <> 1
) AS duplicates;

-- Exact row-preservation check for every original sales column.
SELECT old_ids.transaction_id
FROM pre_migration_sales_inventory AS old_ids
LEFT JOIN sales_transactions AS st
  ON st.transaction_id = old_ids.transaction_id
WHERE st.transaction_id IS NULL;

SELECT st.transaction_id
FROM sales_transactions AS st
LEFT JOIN pre_migration_sales_inventory AS old_ids
  ON old_ids.transaction_id = st.transaction_id
WHERE old_ids.transaction_id IS NULL;

SELECT old_ids.transaction_id
FROM pre_migration_sales_inventory AS old_ids
JOIN sales_transactions AS st
  ON st.transaction_id = old_ids.transaction_id
WHERE NOT (
    st.order_date <=> old_ids.order_date
    AND st.product <=> old_ids.product
    AND st.category <=> old_ids.category
    AND st.region <=> old_ids.region
    AND st.sales_channel <=> old_ids.sales_channel
    AND st.quantity <=> old_ids.quantity
    AND st.unit_price <=> old_ids.unit_price
    AND st.discount_amount <=> old_ids.discount_amount
    AND st.status <=> old_ids.status
    AND st.revenue <=> old_ids.revenue
    AND st.order_month <=> old_ids.order_month
);

-- Exact aggregate checks for every grouped baseline dimension.
SELECT 'product' AS dimension, old.product AS grouping_value
FROM (
    SELECT product, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY product
) AS old
LEFT JOIN (
    SELECT product, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY product
) AS current USING (product)
WHERE current.product IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue
UNION ALL
SELECT 'product', current.product
FROM (
    SELECT product, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY product
) AS current
LEFT JOIN (
    SELECT product, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY product
) AS old USING (product)
WHERE old.product IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue;

SELECT 'region' AS dimension, old.region AS grouping_value
FROM (
    SELECT region, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY region
) AS old
LEFT JOIN (
    SELECT region, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY region
) AS current USING (region)
WHERE current.region IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue
UNION ALL
SELECT 'region', current.region
FROM (
    SELECT region, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY region
) AS current
LEFT JOIN (
    SELECT region, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY region
) AS old USING (region)
WHERE old.region IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue;

SELECT 'month' AS dimension, old.order_month AS grouping_value
FROM (
    SELECT order_month, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY order_month
) AS old
LEFT JOIN (
    SELECT order_month, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY order_month
) AS current USING (order_month)
WHERE current.order_month IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue
UNION ALL
SELECT 'month', current.order_month
FROM (
    SELECT order_month, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY order_month
) AS current
LEFT JOIN (
    SELECT order_month, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY order_month
) AS old USING (order_month)
WHERE old.order_month IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue;

SELECT 'sales_channel' AS dimension, old.sales_channel AS grouping_value
FROM (
    SELECT sales_channel, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY sales_channel
) AS old
LEFT JOIN (
    SELECT sales_channel, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY sales_channel
) AS current USING (sales_channel)
WHERE current.sales_channel IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue
UNION ALL
SELECT 'sales_channel', current.sales_channel
FROM (
    SELECT sales_channel, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM sales_transactions GROUP BY sales_channel
) AS current
LEFT JOIN (
    SELECT sales_channel, COUNT(*) AS row_count, SUM(quantity) AS total_quantity,
           SUM(revenue) AS total_revenue
    FROM pre_migration_sales_inventory GROUP BY sales_channel
) AS old USING (sales_channel)
WHERE old.sales_channel IS NULL
   OR current.row_count <> old.row_count
   OR current.total_quantity <> old.total_quantity
   OR current.total_revenue <> old.total_revenue;

-- STOP NOW unless every result in this section is empty/zero and the legacy
-- aggregate gate is PASS. Do not execute final DDL in the same batch.

-- ============================================================================
-- PHASE E: FINAL DDL — RUN ONLY AFTER ALL MANUAL GATES PASS
-- ============================================================================

ALTER TABLE sales_transactions
    MODIFY COLUMN business_id BIGINT UNSIGNED NOT NULL,
    ADD INDEX ix_sales_transactions_business_order_date
        (business_id, order_date),
    ADD CONSTRAINT fk_sales_transactions_business
        FOREIGN KEY (business_id) REFERENCES businesses (business_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;

-- ============================================================================
-- PHASE F: POST-MIGRATION VERIFICATION
-- ============================================================================

SELECT COUNT(*) AS null_business_ids
FROM sales_transactions
WHERE business_id IS NULL;

SELECT COUNT(*) AS orphaned_transactions
FROM sales_transactions AS st
LEFT JOIN businesses AS b ON b.business_id = st.business_id
WHERE b.business_id IS NULL;

SELECT COUNT(*) AS wrong_business_owner_rows
FROM sales_transactions
WHERE business_id <> @legacy_business_id
   OR business_id IS NULL;

SELECT COUNT(*) AS total_transactions,
       SUM(revenue) AS total_revenue,
       SUM(quantity * unit_price) AS gross_sales,
       SUM(discount_amount) AS total_discounts,
       SUM(quantity) AS total_quantity,
       AVG(revenue) AS average_revenue,
       MIN(transaction_id) AS minimum_transaction_id,
       MAX(transaction_id) AS maximum_transaction_id,
       MIN(order_date) AS minimum_order_date,
       MAX(order_date) AS maximum_order_date,
       CASE
           WHEN COUNT(*) = 50
            AND SUM(revenue) = 1479650.00
            AND SUM(quantity * unit_price) = 1484650.00
            AND SUM(discount_amount) = 5000.00
            AND SUM(quantity) = 652
            AND MIN(transaction_id) = 'TXN0001'
            AND MAX(transaction_id) = 'TXN1002'
            AND MIN(order_date) = '2026-01-03'
            AND MAX(order_date) = '2026-07-05'
           THEN 'PASS'
           ELSE 'STOP - post-migration aggregate mismatch'
       END AS mandatory_post_migration_gate
FROM sales_transactions;

SELECT old_ids.transaction_id
FROM pre_migration_sales_inventory AS old_ids
LEFT JOIN sales_transactions AS st
  ON st.transaction_id = old_ids.transaction_id
WHERE st.transaction_id IS NULL;

SELECT st.transaction_id
FROM sales_transactions AS st
LEFT JOIN pre_migration_sales_inventory AS old_ids
  ON old_ids.transaction_id = st.transaction_id
WHERE old_ids.transaction_id IS NULL;

-- Exact post-migration preservation check for every original sales column.
SELECT old_ids.transaction_id
FROM pre_migration_sales_inventory AS old_ids
JOIN sales_transactions AS st
 ON st.transaction_id = old_ids.transaction_id
WHERE NOT (
   st.order_date <=> old_ids.order_date
   AND st.product <=> old_ids.product
   AND st.category <=> old_ids.category
   AND st.region <=> old_ids.region
   AND st.sales_channel <=> old_ids.sales_channel
   AND st.quantity <=> old_ids.quantity
   AND st.unit_price <=> old_ids.unit_price
   AND st.discount_amount <=> old_ids.discount_amount
   AND st.status <=> old_ids.status
   AND st.revenue <=> old_ids.revenue
   AND st.order_month <=> old_ids.order_month
);

-- Exact post-migration grouped aggregate check. mismatch_groups must be zero
-- for product, region, month, and sales_channel.
SELECT 'product' AS dimension,
      (
          SELECT COUNT(*)
          FROM (
              SELECT old.product, old.row_count, old.total_quantity,
                     old.total_revenue
              FROM (
                  SELECT product, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY product
              ) AS old
              LEFT JOIN (
                  SELECT product, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY product
              ) AS current USING (product)
              WHERE current.product IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
              UNION ALL
              SELECT current.product, current.row_count,
                     current.total_quantity, current.total_revenue
              FROM (
                  SELECT product, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY product
              ) AS current
              LEFT JOIN (
                  SELECT product, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY product
              ) AS old USING (product)
              WHERE old.product IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
          ) AS product_mismatches
      ) AS mismatch_groups
UNION ALL
SELECT 'region',
      (
          SELECT COUNT(*)
          FROM (
              SELECT old.region
              FROM (
                  SELECT region, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY region
              ) AS old
              LEFT JOIN (
                  SELECT region, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY region
              ) AS current USING (region)
              WHERE current.region IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
              UNION ALL
              SELECT current.region
              FROM (
                  SELECT region, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY region
              ) AS current
              LEFT JOIN (
                  SELECT region, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY region
              ) AS old USING (region)
              WHERE old.region IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
          ) AS region_mismatches
      )
UNION ALL
SELECT 'month',
      (
          SELECT COUNT(*)
          FROM (
              SELECT old.order_month
              FROM (
                  SELECT order_month, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY order_month
              ) AS old
              LEFT JOIN (
                  SELECT order_month, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY order_month
              ) AS current USING (order_month)
              WHERE current.order_month IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
              UNION ALL
              SELECT current.order_month
              FROM (
                  SELECT order_month, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY order_month
              ) AS current
              LEFT JOIN (
                  SELECT order_month, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY order_month
              ) AS old USING (order_month)
              WHERE old.order_month IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
          ) AS month_mismatches
      )
UNION ALL
SELECT 'sales_channel',
      (
          SELECT COUNT(*)
          FROM (
              SELECT old.sales_channel
              FROM (
                  SELECT sales_channel, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY sales_channel
              ) AS old
              LEFT JOIN (
                  SELECT sales_channel, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY sales_channel
              ) AS current USING (sales_channel)
              WHERE current.sales_channel IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
              UNION ALL
              SELECT current.sales_channel
              FROM (
                  SELECT sales_channel, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM sales_transactions GROUP BY sales_channel
              ) AS current
              LEFT JOIN (
                  SELECT sales_channel, COUNT(*) AS row_count,
                         SUM(quantity) AS total_quantity,
                         SUM(revenue) AS total_revenue
                  FROM pre_migration_sales_inventory GROUP BY sales_channel
              ) AS old USING (sales_channel)
              WHERE old.sales_channel IS NULL
                 OR current.row_count <> old.row_count
                 OR current.total_quantity <> old.total_quantity
                 OR current.total_revenue <> old.total_revenue
          ) AS channel_mismatches
      );

SELECT c.COLUMN_NAME, c.COLUMN_TYPE, c.IS_NULLABLE
FROM information_schema.COLUMNS AS c
WHERE c.TABLE_SCHEMA = DATABASE()
  AND c.TABLE_NAME = 'sales_transactions'
  AND c.COLUMN_NAME = 'business_id';

SELECT kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME,
       kcu.REFERENCED_COLUMN_NAME, rc.DELETE_RULE, rc.UPDATE_RULE
FROM information_schema.KEY_COLUMN_USAGE AS kcu
JOIN information_schema.REFERENTIAL_CONSTRAINTS AS rc
  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
 AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
WHERE kcu.TABLE_SCHEMA = DATABASE()
  AND kcu.TABLE_NAME = 'sales_transactions'
  AND kcu.COLUMN_NAME = 'business_id';

SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'sales_transactions'
  AND INDEX_NAME = 'ix_sales_transactions_business_order_date'
ORDER BY SEQ_IN_INDEX;

SHOW CREATE TABLE sales_transactions;

-- Rollback:
-- * Before final DDL, stop and restore the disposable scratch copy or use the
--   independently captured snapshot. DDL before this point has already
--   committed implicitly.
-- * After final DDL, use only a separately reviewed maintenance-window
--   procedure: snapshot first, drop the named foreign key and index, modify
--   business_id back to nullable, remove the column only after verification,
--   and archive/remove the legacy workspace only after checking dependent
--   memberships and records.
-- * Never run an automatic destructive rollback and never use this procedure
--   against the original database without separate approval.
