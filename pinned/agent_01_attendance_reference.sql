-- ============================================================
-- agent 01 attendance and absenteeism — canonical reference
-- source: vardan, 2026-04-28
-- ============================================================
-- this is the production-ready query used as the manpower CTE for
-- nim-agents-ops agent 1. covers all 3 sub-tabs (cc / temp / vendor)
--
-- key logic to internalize:
--   rostered = active - week_off - al_leave - other_leave
--   absent% = absent_other / rostered
--
-- people on planned leave (week off, annual leave, sick leave, comp
-- off, maternity, marriage etc) are NOT counted in rostered. only
-- people expected to show up and didn't are counted as absent.
--
-- DO NOT use working_day=1 as the denominator — biometric working_day
-- includes everyone scheduled including planned leaves
-- ============================================================

DECLARE target_date DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Dubai'), INTERVAL 1 DAY);
DECLARE target_country STRING DEFAULT 'ae';

-- ─────────────────────────────────────────────────────────────────
-- daily ds ops consolidated view — full reference query for context
-- one row per darkstore for the target date and country
-- covers: manpower · orders · items picked · inbound · stocktake · defects
-- ─────────────────────────────────────────────────────────────────

WITH
-- 1. manpower from biometric roster
manpower AS (
  SELECT
    b.wh_code,
    w.area_name_en,
    COUNT(DISTINCT b.employee_id) AS active,
    COUNT(DISTINCT CASE WHEN b.punch_status = 'Punched Properly' THEN b.employee_id END) AS present,
    COUNT(DISTINCT CASE WHEN b.punch_status = 'Week Off' THEN b.employee_id END) AS week_off,
    COUNT(DISTINCT CASE WHEN b.punch_status = 'Annual Leave' THEN b.employee_id END) AS al_leave,
    COUNT(DISTINCT CASE
            WHEN LOWER(b.punch_status) LIKE '%leave'
             AND b.punch_status <> 'Annual Leave'
          THEN b.employee_id END) AS other_leave,
    COUNT(DISTINCT CASE
            WHEN b.punch_status NOT IN ('Punched Properly','Week Off','Annual Leave')
             AND LOWER(b.punch_status) NOT LIKE '%leave'
          THEN b.employee_id END) AS absent_other,
    COUNT(DISTINCT CASE WHEN b.valid_in_time IS NOT NULL THEN b.employee_id END) AS logged_in
  FROM `noonbinimksa.Stores.Biometric_base_v2_3` b
  LEFT JOIN `noonbinimksa.Stores.warehouse` w
    ON b.wh_code = w.partner_wh_code
  WHERE b.created_date = target_date
    AND LOWER(w.country_code) = target_country
  GROUP BY b.wh_code, w.area_name_en
),

-- 2. orders and items picked from ipp_daily (country-specific source)
productivity AS (
  SELECT
    store AS wh_code,
    SUM(total_orders) AS orders,
    SUM(outbound_qty) AS items_picked,
    SUM(outbound_time_picking) AS pick_time_sec
  FROM (
    SELECT store, total_orders, outbound_qty, outbound_time_picking
    FROM `noonbinimksa.darkstore.ipp_daily_ae`
    WHERE date = target_date AND target_country = 'ae'
    UNION ALL
    SELECT store, total_orders, outbound_qty, outbound_time_picking
    FROM `noonbinimksa.darkstore.ipp_daily_sa`
    WHERE date = target_date AND target_country = 'sa'
  )
  GROUP BY store
),

-- 3. inbound qty from fulfillment.ipp (country-specific source)
inbound AS (
  SELECT dist_partner_code AS wh_code,
         SUM(completed_count) AS inbound_qty,
         COUNT(DISTINCT id_user) AS inbound_users
  FROM (
    SELECT dist_partner_code, completed_count, id_user, date
    FROM `noonbinimops.fulfillment.ipp`
    WHERE DATE(date) = target_date AND target_country = 'ae'
    UNION ALL
    SELECT dist_partner_code, completed_count, id_user, date
    FROM `noonbinimops.fulfillment.ipp_ksa`
    WHERE DATE(date) = target_date AND target_country = 'sa'
    UNION ALL
    SELECT dist_partner_code, completed_count, id_user, date
    FROM `noonbinimksa.darkstore.ipp_all`
    WHERE DATE(date) = target_date
      AND target_country IN ('eg','bh')
      AND country_code = CASE WHEN target_country = 'eg' THEN 3
                              WHEN target_country = 'bh' THEN 4
                              ELSE NULL END
  )
  GROUP BY dist_partner_code
),

-- 4. stocktake from the proper base: lines counted, not inflated
stocktake AS (
  SELECT
    partner_warehouse_code AS wh_code,
    COUNT(DISTINCT id_user) AS st_users,
    SUM(CASE
          WHEN status_desc IN ('pending_approval','completed')
           AND job_line_status IN ('present','excess')
          THEN 1 ELSE 0 END) AS st_items_counted,
    COUNT(*) AS st_total_lines,
    SUM(COALESCE(variance_, 0)) AS st_net_variance,
    SUM(CASE WHEN variance_ > 0 THEN variance_ ELSE 0 END) AS st_pos_variance,
    SUM(CASE WHEN variance_ < 0 THEN ABS(variance_) ELSE 0 END) AS st_neg_variance
  FROM `noonbinimksa.darkstore.stock_take_base`
  WHERE created_date = target_date
    AND LOWER(country_code) = target_country
  GROUP BY partner_warehouse_code
),

-- 5. defects by category
defects AS (
  SELECT
    partner_wh_code AS wh_code,
    SUM(count_order) AS def_orders,
    SUM(count_item) AS def_items,
    SUM(CASE WHEN complain_category = 'Defect_fulfillment (missing / wrong item / order)' THEN count_item ELSE 0 END) AS f_miss_wrong,
    SUM(CASE WHEN complain_category = 'Defect_delivery (damage)'                          THEN count_item ELSE 0 END) AS d_damage,
    SUM(CASE WHEN complain_category = 'Defect_quality'                                     THEN count_item ELSE 0 END) AS d_quality,
    SUM(CASE WHEN complain_category = 'Defect_delivery (late / wrong / no delivery)'      THEN count_item ELSE 0 END) AS d_late_wrong,
    SUM(CASE WHEN complain_category = 'Defect_category (content_mismatch)'                THEN count_item ELSE 0 END) AS c_mismatch,
    SUM(CASE WHEN complain_category = 'Defect_fulfillment (others)'                        THEN count_item ELSE 0 END) AS f_other,
    SUM(CASE WHEN complain_category = 'Defect_category (item malfunction / warranty)'     THEN count_item ELSE 0 END) AS c_malf
  FROM `noonbinimksa.darkstore.complains_raw_ae_bh_agg`
  WHERE date = target_date
    AND country = target_country
  GROUP BY partner_wh_code
)

-- final: one row per darkstore — agent 1 reads from manpower CTE only
-- agents 5 (defects), 9 (missing inv) reuse defects + stocktake CTEs
-- agents 2/3 (iph) reuse productivity + inbound
SELECT
  m.wh_code,
  m.area_name_en,

  -- manpower
  m.active,
  (m.active - m.week_off - m.al_leave - m.other_leave) AS rostered,
  m.present,
  ROUND(SAFE_DIVIDE(m.present, NULLIF(m.active - m.week_off - m.al_leave - m.other_leave, 0)) * 100, 1) AS present_pct,
  m.week_off,
  m.al_leave,
  m.other_leave,
  m.absent_other,
  m.logged_in,

  -- orders + picking
  COALESCE(p.orders, 0) AS orders,
  COALESCE(p.items_picked, 0) AS items_picked,
  ROUND(SAFE_DIVIDE(p.items_picked, NULLIF(p.orders,0)), 2) AS items_per_order,
  ROUND(SAFE_DIVIDE(p.items_picked, NULLIF(m.present,0)), 0) AS items_per_mp,
  ROUND(SAFE_DIVIDE(p.items_picked * 3600, NULLIF(p.pick_time_sec,0)), 0) AS pick_iph,

  -- inbound
  COALESCE(i.inbound_qty, 0) AS inbound_qty,
  COALESCE(i.inbound_users, 0) AS inbound_users,

  -- stocktake
  COALESCE(s.st_users, 0) AS st_users,
  COALESCE(s.st_items_counted, 0) AS st_items_counted,
  COALESCE(s.st_total_lines, 0) AS st_total_lines,
  COALESCE(s.st_net_variance, 0) AS st_net_variance,
  COALESCE(s.st_pos_variance, 0) AS st_pos_variance,
  COALESCE(s.st_neg_variance, 0) AS st_neg_variance,
  ROUND(SAFE_DIVIDE(ABS(s.st_net_variance), NULLIF(s.st_items_counted,0)) * 100, 2) AS variance_pct,

  -- defects
  COALESCE(d.def_orders, 0) AS def_orders,
  COALESCE(d.def_items, 0) AS def_items,
  ROUND(SAFE_DIVIDE(d.def_orders, NULLIF(p.orders,0)) * 100, 2) AS defect_rate_pct,
  COALESCE(d.f_miss_wrong, 0) AS def_fulfill_miss_wrong,
  COALESCE(d.d_damage, 0) AS def_delivery_damage,
  COALESCE(d.d_quality, 0) AS def_quality,
  COALESCE(d.d_late_wrong, 0) AS def_delivery_late_wrong,
  COALESCE(d.c_mismatch, 0) AS def_content_mismatch,
  COALESCE(d.f_other, 0) AS def_fulfill_other,
  COALESCE(d.c_malf, 0) AS def_malfunction_warranty,

  -- meta
  target_date AS report_date,
  UPPER(target_country) AS country_code

FROM manpower m
LEFT JOIN productivity p ON m.wh_code = p.wh_code
LEFT JOIN inbound      i ON m.wh_code = i.wh_code
LEFT JOIN stocktake    s ON m.wh_code = s.wh_code
LEFT JOIN defects      d ON m.wh_code = d.wh_code
ORDER BY orders DESC NULLS LAST;

-- ============================================================
-- adapting for agent 1 sub-tabs
-- ============================================================
-- the manpower CTE above gives ds-grain TOTAL counts (cc + temp combined)
-- to split into cc-absent and temp-absent sub-tabs, replicate manpower
-- with an HR_Desig filter:
--
-- cc filter:
--   AND (UPPER(b.HR_Desig) LIKE '%CORE%'
--        OR b.HR_Desig IN ('Barista','Team Leader, Operations','Warehouse Associate'))
--
-- temp filter:
--   AND UPPER(b.HR_Desig) LIKE '%TEMP%'
--
-- for the vendor sub-tab, after applying temp filter, join to user/vendor:
--   LEFT JOIN `noondwh.instantusers_cup.user` u
--     ON LOWER(TRIM(b.name)) = LOWER(TRIM(CONCAT(u.first_name, ' ', u.last_name)))
--    AND u.is_active = 1
--   LEFT JOIN `noondwh.instantusers_cup.vendor` v
--     ON u.id_vendor = v.id_vendor
--   -- prefix fallback for unmatched names:
--   LEFT JOIN (
--     SELECT SUBSTR(shortcode, 1, 3) AS pfx, id_vendor, name AS vendor_name, email
--     FROM `noondwh.instantusers_cup.vendor`
--     WHERE is_active = 1 AND vendor_type = 'external'
--   ) vp ON REGEXP_EXTRACT(b.employee_id, r'^([A-Z]+)') = vp.pfx
--
-- and aggregate by COALESCE(v.id_vendor, vp.id_vendor) instead of wh_code
-- exclude id_vendor = 143 (non uae internal CC catch-all) from vendor sub-tab
-- ============================================================
