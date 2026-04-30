# morpheus · agent query reference (analyst review)

**version:** v2026-04-30
**owner:** vardan nagar (vnagar@noon.com)
**purpose:** reference doc for each of the 11 ds-ops agents — definitions, source tables,
SQL, thresholds, and known edge cases. analyst should validate the SQL + metric math
against business intent.

> when reviewing: focus on (a) metric math, (b) source-table choice + filters, (c) thresholds vs. matrix v0.9 expectation, (d) edge cases listed under "review checkpoints" per agent.

---

## 0. shared concepts

### 0.1 tier model (gate-then-tier)

every agent applies the same pipeline:

1. **scope filter** → restrict to active UAE darkstores (156)
2. **floor** → drop rows below the t1 metric threshold
3. **min count** → drop rows below the count safeguard (avoids false alarms on tiny volumes)
4. **significance gate** → keep `worst_n_floor` (default top 5–10) regardless of contribution
5. **tier assignment** → t3 → t2 → t1 evaluated in order, first match wins
6. **dedup** → skip if the same `(agent, sub_tab, ds_code)` was alerted in the last 48h
7. **draft on demand** → no auto-drafts; user clicks "create draft" in the dashboard

### 0.2 country code mapping

| friendly | bq country_code | id_country (mxdcss_dcss) |
|---|---|---|
| uae | `ae` | 1 |
| ksa | `sa` | 2 |
| egy | `eg` | 3 |

### 0.3 storage type mapping (saumy, 2026-04-28)

```sql
CASE
  WHEN src_wh_code IN ('RUHID05','JEDID05','AUHFKZ01','AUHFKZ02','AUHFKZ03') THEN 'frozen'
  WHEN src_wh_code IN ('JEDID01','JEDID02','RUHID01','RUHID02','CAIID01','CAIID02',
                       'DXBID01','DXBID02','AUHID01','AUHID02','DXSID01','DXSID02') THEN 'ambient'
  WHEN src_wh_code IN ('RUHID03','RUHID04','JEDID03','JEDID04',
                       'AUHID03','AUHID04','DXBID03','DXBID04') THEN 'chiller'
  WHEN src_wh_code IN ('RUHID07','JEDID07','AUHID07','DXBID07') THEN 'ultrafresh'
  WHEN src_wh_code IN ('RUHID06','JEDID06','DXBFNV01') THEN 'fnv'
  ELSE 'others'
END
```

per-storage SLA (minutes):

| storage | SLA |
|---|---|
| ambient | 360 |
| chiller | 60 |
| frozen | 30 |
| ultrafresh | 30 |
| fnv | 30 |

### 0.4 escalation routing

| grain | t1 | t2 | t3 |
|---|---|---|---|
| ds-grain (1 cc/temp · 2 · 3 · 4 · 5 · 6 · 7 · 8 · 9 · 10) | ds AM | + sharath | + ali (+ saro for inv-health: 4, 5, 6, 9) |
| 1 vendor sub-tab | vendor.email | + sharath | + harish |
| 11 audit | ds AM + lead + logistic_head | + sharath | + ali |

---

## agent 01 · attendance and absenteeism

### purpose
flag darkstores (cc / temp grain) and vendors where absenteeism breaches threshold.

### grain
ds × date (cc / temp) · vendor (cross-stores)

### sub-tabs
`cc_today · temp_today · vendor_today · cc_d1 · temp_d1 · vendor_d1`

### definitions
- `active`  = total unique employees on the biometric roster for that day
- `planned` = active − week_off − annual_leave − other_leave  ← people expected to show up
- `present` = punched_properly OR single_punch
- `absent`  = **planned − present** (vardan 2026-04-30 fix; was previously double-counting Single Punch)
- `absent_pct` = absent / planned × 100

### thresholds

| tier | rule |
|---|---|
| t3 | `absent% > 8%` OR `absent_count > 10` |
| t2 | `absent% > 5%` OR `absent_count > 5` |
| t1 | `absent% > 3%` |

### filter (cc vs temp)

- **cc grain**:  `UPPER(HR_Desig) NOT LIKE '%TEMP%' AND HR_Desig IS NOT NULL`
- **temp grain**:  `UPPER(HR_Desig) LIKE '%TEMP%'`
- **vendor grain**: temp employees joined to vendor master (3-step chain), excludes `id_vendor=143` ('non uae' catch-all)

### sql · ds-grain (cc + temp)

```sql
DECLARE target_date DATE DEFAULT DATE('2026-04-30');
DECLARE target_country STRING DEFAULT 'ae';
-- {desig_filter} = the cc or temp filter shown above

WITH manpower AS (
  SELECT
    b.wh_code,
    w.area_name_en,
    COUNT(DISTINCT b.employee_id) AS active,
    COUNT(DISTINCT CASE WHEN b.punch_status IN ('Punched Properly','Single Punch')
                        THEN b.employee_id END) AS present,
    COUNT(DISTINCT CASE WHEN b.punch_status = 'Week Off' THEN b.employee_id END) AS week_off,
    COUNT(DISTINCT CASE WHEN b.punch_status = 'Annual Leave' THEN b.employee_id END) AS al_leave,
    COUNT(DISTINCT CASE
            WHEN LOWER(b.punch_status) LIKE '%leave'
             AND b.punch_status <> 'Annual Leave'
          THEN b.employee_id END) AS other_leave
  FROM `noonbinimksa.Stores.Biometric_base_v2_3` b
  LEFT JOIN `noonbinimksa.Stores.warehouse` w ON b.wh_code = w.partner_wh_code
  WHERE b.created_date = target_date
    AND LOWER(w.country_code) = target_country
    AND {desig_filter}
  GROUP BY b.wh_code, w.area_name_en
)
SELECT
  wh_code AS ds_code,
  area_name_en AS ds_name,
  active,
  (active - week_off - al_leave - other_leave) AS rostered,                    -- planned
  present,
  GREATEST((active - week_off - al_leave - other_leave) - present, 0) AS absent_count,
  ROUND(SAFE_DIVIDE(
    GREATEST((active - week_off - al_leave - other_leave) - present, 0),
    NULLIF(active - week_off - al_leave - other_leave, 0)) * 100, 1) AS absent_pct
FROM manpower
WHERE (active - week_off - al_leave - other_leave) > 0;
```

### sql · vendor-grain (cross-stores)

```sql
WITH bio AS (
  SELECT b.wh_code, b.employee_id, b.name, b.punch_status
  FROM `noonbinimksa.Stores.Biometric_base_v2_3` b
  LEFT JOIN `noonbinimksa.Stores.warehouse` w ON b.wh_code = w.partner_wh_code
  WHERE b.created_date = target_date
    AND LOWER(w.country_code) = target_country
    AND UPPER(b.HR_Desig) LIKE '%TEMP%'
),
name_join AS (
  -- step 1 (~81% match): biometric.name → user.full_name
  SELECT bio.*, u.id_vendor
  FROM bio
  LEFT JOIN `noondwh.instantusers_cup.user` u
    ON LOWER(TRIM(bio.name)) = LOWER(TRIM(CONCAT(u.first_name, ' ', u.last_name)))
   AND u.is_active = 1
),
prefix_lookup AS (
  -- step 2 (~14% match): biometric employee_id prefix → vendor shortcode prefix
  SELECT SUBSTR(shortcode, 1, 3) AS pfx,
         ANY_VALUE(id_vendor) AS id_vendor,
         ANY_VALUE(name) AS vendor_name,
         ANY_VALUE(email) AS vendor_email,
         ANY_VALUE(shortcode) AS shortcode
  FROM `noondwh.instantusers_cup.vendor`
  WHERE is_active = 1 AND vendor_type = 'external'
  GROUP BY pfx
),
matched AS (
  SELECT nj.*,
         COALESCE(nj.id_vendor, pl.id_vendor) AS final_id_vendor,
         pl.shortcode AS prefix_shortcode,
         pl.vendor_name AS prefix_vendor_name,
         pl.vendor_email AS prefix_vendor_email
  FROM name_join nj
  LEFT JOIN prefix_lookup pl
    ON REGEXP_EXTRACT(nj.employee_id, r'^([A-Z]+)') = pl.pfx
),
enriched AS (
  SELECT m.*, v.shortcode AS v_shortcode, v.name AS v_name, v.email AS v_email
  FROM matched m
  LEFT JOIN `noondwh.instantusers_cup.vendor` v ON v.id_vendor = m.final_id_vendor
)
SELECT
  final_id_vendor AS id_vendor,
  COALESCE(v_shortcode, prefix_shortcode) AS vendor_shortcode,
  COALESCE(v_name, prefix_vendor_name) AS vendor_name,
  COALESCE(v_email, prefix_vendor_email) AS vendor_email,
  COUNT(DISTINCT wh_code) AS stores_affected,
  COUNT(DISTINCT employee_id) AS active,
  COUNT(DISTINCT CASE WHEN punch_status IN ('Punched Properly','Single Punch')
                      THEN employee_id END) AS present,
  COUNT(DISTINCT CASE WHEN punch_status = 'Week Off' THEN employee_id END) AS week_off,
  COUNT(DISTINCT CASE WHEN punch_status = 'Annual Leave' THEN employee_id END) AS al_leave,
  COUNT(DISTINCT CASE
          WHEN LOWER(punch_status) LIKE '%leave'
           AND punch_status <> 'Annual Leave'
        THEN employee_id END) AS other_leave
FROM enriched
WHERE final_id_vendor IS NOT NULL AND final_id_vendor <> 143  -- exclude 'non uae'
GROUP BY final_id_vendor, v_shortcode, prefix_shortcode, v_name,
         prefix_vendor_name, v_email, prefix_vendor_email
HAVING (active - week_off - al_leave - other_leave) > 0;
```

note: in python post-processing, `absent_count = max(active - leaves - present, 0)` and `absent_pct = absent_count / planned × 100`. matches the ds-grain formula.

### review checkpoints
- [ ] HR_Desig "NOT LIKE '%TEMP%'" — does this capture 100% of cc roles? (vardan probed 2026-04-29; CC variants seen: Core Colleague, Senior Core Colleague, Team Leader Operations, Warehouse Associate, Core Colleague FSQ, Assistant Supervisor Operations, CC, TL, PICKER)
- [ ] vendor 3-step join chain — saumy's matrix v0.9 says ~95% coverage. confirm any unattributed temp employees aren't routed to t3 vendor escalation
- [ ] id_vendor=143 ('non uae') exclusion — confirm 1,197 noon-direct CC catch-all should NOT be treated as a vendor
- [ ] Single Punch counting as `present` — confirm vs. business definition (was a 2026-04-28 fix)
- [ ] absent = planned − present is correct only when (Single Punch + Punched Properly) covers all "showed up" cases. confirm

---

## agent 02 · iph pickers (outbound)

### purpose
flag darkstores (and individual pickers) where outbound iph is below the bucket-relative cut.

### grain
ds × date (overall) · ds × picker × date (picker)

### sub-tabs
`overall_d0 · picker_d0 · overall_d1 · picker_d1`

### definition
`iph = SUM(outbound_qty) × 3600 / NULLIF(SUM(outbound_time_picking), 0)`

### opd buckets
- small: opd < 500
- medium: 500–1500
- large: 1500+

### thresholds (rolling within bucket, rebased nightly off L7D)

| tier | rule (option A, vardan 2026-04-28) |
|---|---|
| t3 | `iph < bucket.p10` |
| t2 | `iph < bucket.p20` (above p10) |
| t1 | `iph < bucket.p50` (above p20) |

### min count
- overall sub-tabs: ≥10 jobs (distinct Employee_ID)
- picker sub-tabs: ≥50 outbound qty handled

### sql · today (ds grain)

```sql
SELECT
  store AS ds_code,
  SUM(total_orders) AS opd,
  SUM(outbound_qty) AS outbound_qty,
  SUM(outbound_time_picking) AS outbound_time,
  SAFE_DIVIDE(SUM(outbound_qty) * 3600.0,
              NULLIF(SUM(outbound_time_picking), 0)) AS iph,
  COUNT(DISTINCT Employee_ID) AS jobs
FROM `noonbinimksa.darkstore.ipp_daily_ae`
WHERE date = DATE('2026-04-30')
GROUP BY store
HAVING SUM(outbound_time_picking) > 0;
```

### sql · today (picker grain)

```sql
SELECT
  store AS ds_code,
  Employee_ID,
  ANY_VALUE(user_name) AS user_name,
  ANY_VALUE(designation) AS designation,
  SUM(total_orders) AS opd,
  SUM(outbound_qty) AS outbound_qty,
  SUM(outbound_time_picking) AS outbound_time,
  SAFE_DIVIDE(SUM(outbound_qty) * 3600.0,
              NULLIF(SUM(outbound_time_picking), 0)) AS iph,
  1 AS jobs
FROM `noonbinimksa.darkstore.ipp_daily_ae`
WHERE date = DATE('2026-04-30')
  AND Employee_ID IS NOT NULL
GROUP BY store, Employee_ID
HAVING outbound_time > 0;
```

### sql · L7D distribution (for nightly threshold rebase)

```sql
SELECT
  store AS ds_code,
  date,
  SUM(total_orders) AS opd,
  SAFE_DIVIDE(SUM(outbound_qty) * 3600.0,
              NULLIF(SUM(outbound_time_picking), 0)) AS iph
FROM `noonbinimksa.darkstore.ipp_daily_ae`
WHERE date BETWEEN DATE_SUB(DATE('2026-04-30'), INTERVAL 7 DAY)
               AND DATE_SUB(DATE('2026-04-30'), INTERVAL 1 DAY)
GROUP BY store, date
HAVING SUM(outbound_time_picking) > 0;
```

### review checkpoints
- [ ] `outbound_time_picking` (the right time field for outbound iph) — saumy ref confirmed; `outbound_time_` includes packing
- [ ] opd-bucket cuts (500 / 1500) — matrix v0.9; analyst can validate against a histogram of L30D ds-day opd
- [ ] picker grain min ≥50 qty — chosen to suppress noise from <1hr shifts

---

## agent 03 · iph putaway (inbound)

### purpose
mirror of agent 02 but for inbound (putaway) iph.

### grain
ds × date · ds × user × date

### definition
`iph = SUM(completed_count) × 3600 / NULLIF(SUM(total_sec), 0)`

### thresholds
same opd-bucket p10/p20/p50 as agent 02 (option A, vardan 2026-04-28)

### sql · today (ds grain)

```sql
SELECT
  dist_partner_code AS ds_code,
  SUM(completed_count) AS inbound_qty,
  SUM(total_sec) AS inbound_time,
  SAFE_DIVIDE(SUM(completed_count) * 3600.0,
              NULLIF(SUM(total_sec), 0)) AS iph,
  COUNT(DISTINCT id_user) AS jobs
FROM `noonbinimops.fulfillment.ipp`     -- KSA: ipp_ksa · EGY/BAH: noonbinimksa.darkstore.ipp_all
WHERE DATE(date) = DATE('2026-04-30')
GROUP BY dist_partner_code
HAVING SUM(total_sec) > 0;
```

### sql · today (picker grain)

```sql
SELECT
  dist_partner_code AS ds_code,
  id_user AS Employee_ID,
  SUM(completed_count) AS inbound_qty,
  SUM(total_sec) AS inbound_time,
  SAFE_DIVIDE(SUM(completed_count) * 3600.0,
              NULLIF(SUM(total_sec), 0)) AS iph,
  1 AS jobs
FROM `noonbinimops.fulfillment.ipp`
WHERE DATE(date) = DATE('2026-04-30')
  AND id_user IS NOT NULL
GROUP BY dist_partner_code, id_user
HAVING inbound_time > 0;
```

### review checkpoints
- [ ] `noonbinimops.fulfillment.ipp` is the inbound table (saumy ref confirmed; not `ipp_daily_ae` which is outbound)
- [ ] opd for bucketing is sourced from `ipp_daily_ae` and joined back (since `ipp` doesn't have order count). check that the join is sound for stores with no outbound activity that day

---

## agent 04 · skips (picker)

### purpose
flag stores where picker skip rate breaches threshold + name top-skipper pickers.

### grain
ds × date (store sub-tab) · ds × picker × date (picker sub-tab)

### sub-tabs
`store · picker`

### definition (store sub-tab)
`skip_pct = SUM(items skipped) / SUM(outbound_qty picked) × 100`

### thresholds (store, vardan 2026-04-29)

| tier | rule |
|---|---|
| t3 | `skip% > 0.20%` |
| t2 | `skip%  0.15% – 0.20%` |
| t1 | `skip%  0.10% – 0.15%` |

min count: ≥2 skips (lowered from 5 per vardan 2026-04-29)

### thresholds (picker sub-tab, raw count)
t3 ≥20 · t2 ≥10 · t1 ≥3

### sql · store sub-tab

```sql
WITH skips AS (
  SELECT
    partner_wh_code AS ds_code,
    SUM(items) AS skips,
    SUM(CASE WHEN LOWER(reason_) LIKE '%not_found%' OR LOWER(reason_) LIKE '%missing%'
             THEN items ELSE 0 END) AS missing,
    SUM(CASE WHEN LOWER(reason_) LIKE '%damag%' THEN items ELSE 0 END) AS damaged,
    SUM(CASE WHEN LOWER(reason_) LIKE '%expir%' THEN items ELSE 0 END) AS expired
  FROM `noonbinimksa.darkstore.daily_manual_skips_hourly_uae_1`     -- KSA: _ksa_1
  WHERE DATE(date_) = DATE('2026-04-30')
  GROUP BY partner_wh_code
),
picks AS (
  SELECT store AS ds_code, SUM(outbound_qty) AS picked
  FROM `noonbinimksa.darkstore.ipp_daily_ae`
  WHERE date = DATE('2026-04-30')
  GROUP BY store
)
SELECT
  s.ds_code,
  COALESCE(p.picked, 0) AS picked,
  s.skips,
  COALESCE(s.missing, 0) AS missing,
  COALESCE(s.damaged, 0) AS damaged,
  COALESCE(s.expired, 0) AS expired,
  ROUND(SAFE_DIVIDE(s.skips, NULLIF(p.picked, 0)) * 100, 2) AS skip_pct
FROM skips s LEFT JOIN picks p USING (ds_code)
WHERE p.picked > 0 AND s.skips > 0;
```

### sql · picker sub-tab

```sql
SELECT
  partner_wh_code AS ds_code,
  skipped_by AS picker_name,
  SUM(items) AS skips,
  SUM(CASE WHEN LOWER(reason_) LIKE '%not_found%' OR LOWER(reason_) LIKE '%missing%'
           THEN items ELSE 0 END) AS missing,
  SUM(CASE WHEN LOWER(reason_) LIKE '%damag%' THEN items ELSE 0 END) AS damaged,
  SUM(CASE WHEN LOWER(reason_) LIKE '%expir%' THEN items ELSE 0 END) AS expired
FROM `noonbinimksa.darkstore.daily_manual_skips_hourly_uae_1`
WHERE DATE(date_) = DATE('2026-04-30')
GROUP BY partner_wh_code, skipped_by
HAVING skips > 0
ORDER BY skips DESC;
```

### review checkpoints
- [ ] `daily_manual_skips_hourly_uae_1` is the right source (saumy referenced this file; the `_1` suffix is the latest version)
- [ ] reason_ buckets — currently mapping not_found/missing → missing, damag → damaged, expir → expired. analyst can confirm against the actual reason_ enum
- [ ] picked denominator is `outbound_qty` from `ipp_daily_ae` — confirm this matches what skips% should be % of (vs total items in orders, vs items_in_picklist)

---

## agent 05 · defects (customer complaints)

### purpose
flag stores where customer-defect rate breaches threshold; surface sub-category breakdown.

### grain
ds × date (overall ds-level rate; sub-categories surfaced as columns, not separate sub-tabs)

### definition
`defect_rate_pct = COUNT(DISTINCT order_nr) / total_orders × 100`

### thresholds (vardan 2026-04-29)

| tier | rule |
|---|---|
| t3 | `defect% > 0.80%` |
| t2 | `defect%  0.60% – 0.80%` |
| t1 | `defect%  0.50% – 0.60%` |

min count: ≥3 defective orders

### sql

```sql
WITH d AS (
  SELECT
    partner_wh_code AS ds_code,
    COUNT(DISTINCT order_nr) AS def_orders,
    COUNT(*) AS def_items,
    SUM(CASE WHEN complain_category = 'Defect_fulfillment (missing / wrong item / order)'
             THEN 1 ELSE 0 END) AS def_fulfill_miss_wrong,
    SUM(CASE WHEN complain_category = 'Defect_delivery (damage)'
             THEN 1 ELSE 0 END) AS def_delivery_damage,
    SUM(CASE WHEN complain_category = 'Defect_quality'
             THEN 1 ELSE 0 END) AS def_quality,
    SUM(CASE WHEN complain_category = 'Defect_delivery (late / wrong / no delivery)'
             THEN 1 ELSE 0 END) AS def_delivery_late_wrong,
    SUM(CASE WHEN complain_category = 'Defect_category (content_mismatch)'
             THEN 1 ELSE 0 END) AS def_content_mismatch,
    SUM(CASE WHEN complain_category = 'Defect_fulfillment (others)'
             THEN 1 ELSE 0 END) AS def_fulfill_other,
    -- saumy's sub-reason buckets
    SUM(CASE WHEN complain_reason = 'expired_item'
             THEN 1 ELSE 0 END) AS def_expired_items,
    SUM(CASE WHEN complain_reason IN ('Limited_shelf_life','item_near_expiry',
                                       'near_expiry','warranty_near_expiry')
             THEN 1 ELSE 0 END) AS def_near_expiry,
    SUM(CASE WHEN complain_reason IN ('quality_not_fresh','ProductQuality_FungusorMold',
                                       'bad_quality_item','Presence_of_Foreign_Substance',
                                       'Pest_Infestation','Presence_of_Worms')
              AND LOWER(minutes_category_new) IN ('milk','dairy & eggs')
             THEN 1 ELSE 0 END) AS def_dairy_milk_quality
  FROM `noonbinimksa.darkstore.complains_raw_all`     -- KSA: complains_raw_sa
  WHERE complain_date = DATE('2026-04-30')
    AND country_code = 'ae'
  GROUP BY partner_wh_code
),
o AS (
  SELECT store AS ds_code, SUM(total_orders) AS orders
  FROM `noonbinimksa.darkstore.ipp_daily_ae`
  WHERE date = DATE('2026-04-30')
  GROUP BY store
)
SELECT
  d.ds_code,
  COALESCE(o.orders, 0) AS orders,
  d.def_orders,
  d.def_items,
  d.def_fulfill_miss_wrong, d.def_delivery_damage, d.def_quality,
  d.def_delivery_late_wrong, d.def_content_mismatch, d.def_fulfill_other,
  d.def_expired_items, d.def_near_expiry, d.def_dairy_milk_quality,
  ROUND(SAFE_DIVIDE(d.def_orders, NULLIF(o.orders, 0)) * 100, 3) AS defect_rate_pct
FROM d LEFT JOIN o USING (ds_code)
WHERE o.orders > 0;
```

### review checkpoints
- [ ] `complains_raw_all` is correct for UAE+EGY (confirmed; saumy doc had ae/sa labels swapped)
- [ ] denominator = `total_orders` from `ipp_daily_ae` for the same date — should this be delivered orders only? could differ if we want to exclude cancellations
- [ ] complain_reason mappings (expired/near_expiry/dairy_milk_quality) — analyst can sanity-check against actual reason_ values

---

## agent 06 · fefo adherence

### purpose
flag stores where the share of fefo-breached SKUs against orders is high.

### grain
ds × date

### definition (vardan 2026-04-29)
`fefo_pct_orders = COUNT(skus where fefo_breach = TRUE) / total_orders × 100`

### thresholds

| tier | rule |
|---|---|
| t3 | `fefo% > 1.0%` |
| t2 | `fefo%  0.5% – 1.0%` |
| t1 | `fefo%  0.3% – 0.5%` |

min count: ≥3 breach skus

### sql

```sql
WITH logs AS (
  -- saumy ref: pick max(updated_at) per (wh_code, sku, date_) to dedup intraday updates
  SELECT DISTINCT a.*
  FROM `noonbinimdwh.modelling.fifo_report_logs` a
  JOIN (
    SELECT wh_code, sku, date_, MAX(updated_at) AS updated_at
    FROM `noonbinimdwh.modelling.fifo_report_logs`
    WHERE date_ BETWEEN DATE_SUB(DATE('2026-04-30'), INTERVAL 1 DAY)
                    AND DATE('2026-04-30')
    GROUP BY wh_code, sku, date_
  ) b USING (wh_code, sku, date_, updated_at)
  WHERE a.country_code = 'ae'
    AND a.date_ = DATE('2026-04-30')
    AND LOWER(IFNULL(a.sub_category, '')) NOT IN ('fresh juices')
    AND LOWER(IFNULL(a.category, '')) NOT IN ('beverages')
),
fefo_ds AS (
  SELECT
    ds_code,
    ANY_VALUE(ds_name) AS ds_name,
    COUNT(*) AS skus_total,
    SUM(CASE WHEN fefo_breach THEN 1 ELSE 0 END) AS skus_breached,
    SUM(IFNULL(exp_nl_units, 0)) AS breach_units,
    ROUND(SUM(IFNULL(ex_nl_value, 0)), 2) AS ex_nl_value
  FROM logs GROUP BY ds_code
),
gmv AS (
  SELECT wb.partner_wh_code AS ds_code, SUM(g.order_nr_cnt) AS orders
  FROM `noonbinimksa.darkstore.odr_gmv_uae` g     -- KSA: odr_gmv_ksa
  LEFT JOIN `noonbinimdwh.chatbot.warehouse_base_table` wb ON wb.wh_code = g.wh_code
  WHERE g.created_date_uae = DATE('2026-04-30')
  GROUP BY wb.partner_wh_code
)
SELECT
  f.ds_code,
  f.ds_name,
  f.skus_total,
  f.skus_breached,
  f.breach_units,
  f.ex_nl_value,
  g.orders,
  ROUND(SAFE_DIVIDE(f.skus_breached, NULLIF(g.orders, 0)) * 100, 3) AS fefo_pct_orders
FROM fefo_ds f LEFT JOIN gmv g USING (ds_code)
WHERE g.orders > 0 AND f.skus_breached > 0;
```

### review checkpoints
- [ ] sources: `noonbinimdwh.modelling.fifo_report_logs` (modelled summary, has fefo_breach + ex_nl_value); confirm vs. matrix's earlier `central_expiry_raw` reference
- [ ] excluded categories: 'Beverages' + 'Fresh Juices' subcategory — saumy ref. confirm with category lead
- [ ] denominator is `order_nr_cnt` (count of orders), not items. confirm matches business definition of "% of orders"
- [ ] `wh_code` ↔ `partner_wh_code` mapping via `chatbot.warehouse_base_table` — analyst can sanity-check coverage

---

## agent 07 · adjustments in stores

### purpose
flag stores where stock adjustments breach threshold (% of live inventory value + absolute $).

### grain
ds × date (live snapshot, same-day)

### definition
- `adj_value` = SUM(positive_variance_value) + SUM(|negative_variance_value|)
- `live_inv_value` = SUM(qty × cost_price) per ds
- `adj_pct = adj_value / live_inv_value × 100`

### thresholds (matrix v0.9 % + absolute $ guards)

| tier | rule |
|---|---|
| t3 | `adj% > 0.50%` AND `adj_value > $2,000` |
| t2 | `adj%  0.25%–0.50%` AND `adj_value > $1,000` |
| t1 | `adj% > 0.10%` AND `adj_value > $500` |

### sql

```sql
WITH adj AS (
  SELECT
    wh_code AS ds_code,
    ANY_VALUE(warehouse_name) AS ds_name,
    SUM(IFNULL(positive_variance_units, 0)) AS adj_up_units,
    SUM(IFNULL(negative_variance_units, 0)) AS adj_down_units,
    ROUND(SUM(IFNULL(CAST(positive_variance_value AS FLOAT64), 0)), 2) AS adj_up_value,
    ROUND(SUM(IFNULL(CAST(negative_variance_value AS FLOAT64), 0)), 2) AS adj_down_value,
    ROUND(SUM(IFNULL(CAST(positive_variance_value AS FLOAT64), 0))
         + SUM(IFNULL(CAST(negative_variance_value AS FLOAT64), 0)), 2) AS adj_value
  FROM `noonbinimops.fulfillment.adjustments_master_uae`     -- KSA: _ksa · EGY: _eg
  WHERE put_date = CURRENT_DATE()
  GROUP BY wh_code
),
-- unit_cost lookup per saumy pricing repo: pick latest year_month per sku
cost AS (
  SELECT sku, ANY_VALUE(cost_price) AS cost_price
  FROM (
    SELECT sku, cost_price,
           ROW_NUMBER() OVER (PARTITION BY sku ORDER BY year_month DESC) AS rn
    FROM `noonbinimprc.pricing.cost_price_retail`
    WHERE country_code = 'ae' AND cost_price > 0
  )
  WHERE rn = 1 GROUP BY sku
),
-- live inventory $ per ds = SUM(qty × cost_price)
inv AS (
  SELECT
    q.warehouse AS ds_code,
    SUM(q.qty) AS live_inv_units,
    ROUND(SUM(q.qty * IFNULL(CAST(c.cost_price AS FLOAT64), 0)), 2) AS live_inv_value,
    ROUND(SAFE_DIVIDE(SUM(CASE WHEN c.cost_price IS NOT NULL THEN q.qty ELSE 0 END),
                      NULLIF(SUM(q.qty), 0)) * 100, 1) AS cost_coverage_pct
  FROM `noonbinimksa.Stores.wh_loc_qty` q
  LEFT JOIN cost c ON c.sku = q.zsku
  WHERE LOWER(q.country_code) = 'ae'
  GROUP BY q.warehouse
)
SELECT
  adj.ds_code,
  adj.ds_name,
  adj.adj_up_units,
  adj.adj_down_units,
  adj.adj_up_value,
  adj.adj_down_value,
  adj.adj_value,
  inv.live_inv_units,
  inv.live_inv_value,
  inv.cost_coverage_pct,
  ROUND(SAFE_DIVIDE(adj.adj_value, NULLIF(inv.live_inv_value, 0)) * 100, 3) AS adj_pct
FROM adj LEFT JOIN inv USING (ds_code)
WHERE adj.adj_value > 0;
```

### review checkpoints
- [ ] `adjustments_master_uae` is fresh same-day; agent uses TODAY (D0), not D-1
- [ ] cost source `noonbinimprc.pricing.cost_price_retail` (saumy pricing repo) — better than fifo_report_logs which only has costs for fefo'd skus
- [ ] `wh_loc_qty.zsku` vs `cost_price_retail.sku` — both are zsku format; join works directly (no psku_code bridge needed for this agent)
- [ ] cost_coverage_pct surfaced for transparency; t3 stores with low coverage may have inflated pct

---

## agent 08 · putaway delays

### purpose
flag (ds × storage_type) where pending qty exceeds the storage's SLA.

### grain
ds × storage_type (live snapshot, same-day)

### definition
`breach_qty` = items_pending where ageing_in_mins exceeds the storage type's SLA (per 0.3 above)

### thresholds

| tier | rule |
|---|---|
| t3 | `breach_qty > 50` |
| t2 | `breach_qty 25 – 50` |
| t1 | `breach_qty 10 – 25` |

### sql

```sql
WITH typed AS (
  SELECT
    ds_code, ds_name, ageing_in_mins, items_pending,
    CASE
      WHEN src_wh_code IN ('RUHID05','JEDID05','AUHFKZ01','AUHFKZ02','AUHFKZ03') THEN 'frozen'
      WHEN src_wh_code IN ('JEDID01','JEDID02','RUHID01','RUHID02','CAIID01','CAIID02',
                           'DXBID01','DXBID02','AUHID01','AUHID02','DXSID01','DXSID02') THEN 'ambient'
      WHEN src_wh_code IN ('RUHID03','RUHID04','JEDID03','JEDID04',
                           'AUHID03','AUHID04','DXBID03','DXBID04') THEN 'chiller'
      WHEN src_wh_code IN ('RUHID07','JEDID07','AUHID07','DXBID07') THEN 'ultrafresh'
      WHEN src_wh_code IN ('RUHID06','JEDID06','DXBFNV01') THEN 'fnv'
      ELSE 'others'
    END AS storage_type
  FROM `noonbinimops.fulfillment.putaway_pendency_v2`
  WHERE date = CURRENT_DATE() AND LOWER(country_code) = 'ae'
)
SELECT
  ds_code, ANY_VALUE(ds_name) AS ds_name,
  storage_type,
  SUM(items_pending) AS total_pending,
  SUM(CASE
        WHEN storage_type = 'ambient'    AND ageing_in_mins > 360 THEN items_pending
        WHEN storage_type = 'chiller'    AND ageing_in_mins > 60  THEN items_pending
        WHEN storage_type IN ('frozen','ultrafresh','fnv') AND ageing_in_mins > 30 THEN items_pending
        ELSE 0 END) AS breach_qty,
  ROUND(SAFE_DIVIDE(
    SUM(CASE
          WHEN storage_type = 'ambient' AND ageing_in_mins > 360 THEN items_pending
          WHEN storage_type = 'chiller' AND ageing_in_mins > 60  THEN items_pending
          WHEN storage_type IN ('frozen','ultrafresh','fnv') AND ageing_in_mins > 30 THEN items_pending
          ELSE 0 END),
    NULLIF(SUM(items_pending), 0)) * 100, 1) AS breach_pct
FROM typed
WHERE storage_type <> 'others'
GROUP BY ds_code, storage_type
HAVING total_pending > 0;
```

### review checkpoints
- [ ] storage type mapping per saumy 2026-04-28 — analyst can validate src_wh_code list is complete (any new WH not listed → 'others' → dropped)
- [ ] per-storage SLA thresholds (360/60/30/30/30) match availability query
- [ ] live snapshot semantics: putaway_pendency_v2 only retains TODAY's snapshot; older partitions have stale data. agent uses CURRENT_DATE()

---

## agent 09 · missing inventory

### purpose
flag stores where stocktake variance value as % of store GMV is high.

### grain
ds × date

### definition (vardan 2026-04-29)
- `missing_qty` = SUM(ABS(net_variance)) across all skus that were stock-taken
- `missing_value` = qty × cost_price (psku_code → zsku → cost via zsku_catexsp.psku bridge)
- `missing_value_pct = missing_value / store_gmv × 100`

### thresholds

| tier | rule |
|---|---|
| t3 | `missing > 0.30% of store GMV` |
| t2 | `missing  0.20% – 0.30%` |
| t1 | `missing  0.10% – 0.20%` |

### sql

```sql
WITH s AS (
  SELECT
    partner_warehouse_code AS ds_code,
    psku_code,
    SUM(CASE
          WHEN status_desc IN ('pending_approval','completed') AND job_line_status IN ('present','excess')
          THEN 1 ELSE 0 END) AS expected_qty,
    SUM(COALESCE(variance_, 0)) AS net_variance_qty
  FROM `noonbinimksa.darkstore.stock_take_base`
  WHERE created_date = DATE('2026-04-30')
    AND LOWER(country_code) = 'ae'
  GROUP BY partner_warehouse_code, psku_code
),
-- bridge: stock_take_base.psku_code is a hash; cost_price_retail.sku is zsku-format.
-- noondwh.zsku_catexsp.psku maps psku_code → zsku_child
psku_map AS (
  SELECT psku_code, ANY_VALUE(zsku_child) AS zsku
  FROM `noondwh.zsku_catexsp.psku`
  GROUP BY psku_code
),
cost AS (
  SELECT sku, ANY_VALUE(cost_price) AS cost_price
  FROM (
    SELECT sku, cost_price,
           ROW_NUMBER() OVER (PARTITION BY sku ORDER BY year_month DESC) AS rn
    FROM `noonbinimprc.pricing.cost_price_retail`
    WHERE country_code = 'ae' AND cost_price > 0
  ) WHERE rn = 1 GROUP BY sku
),
valued AS (
  SELECT
    s.ds_code,
    SUM(s.expected_qty) AS expected_qty,
    SUM(s.net_variance_qty) AS net_variance_qty,
    SUM(ABS(s.net_variance_qty)) AS missing_qty,
    ROUND(SUM(ABS(s.net_variance_qty) * IFNULL(CAST(c.cost_price AS FLOAT64), 0)), 2) AS missing_value
  FROM s
  LEFT JOIN psku_map pm ON pm.psku_code = s.psku_code
  LEFT JOIN cost c ON c.sku = pm.zsku
  GROUP BY s.ds_code
),
gmv AS (
  SELECT
    wb.partner_wh_code AS ds_code,
    SUM(g.gmv) AS store_gmv,
    SUM(g.order_nr_cnt) AS orders
  FROM `noonbinimksa.darkstore.odr_gmv_uae` g
  LEFT JOIN `noonbinimdwh.chatbot.warehouse_base_table` wb ON wb.wh_code = g.wh_code
  WHERE g.created_date_uae = DATE('2026-04-30')
  GROUP BY wb.partner_wh_code
)
SELECT
  v.ds_code,
  v.expected_qty,
  v.missing_qty,
  v.missing_value,
  g.store_gmv,
  g.orders,
  ROUND(SAFE_DIVIDE(v.missing_value, NULLIF(g.store_gmv, 0)) * 100, 3) AS missing_value_pct
FROM valued v LEFT JOIN gmv g USING (ds_code)
WHERE v.missing_qty > 0 AND g.store_gmv > 0;
```

### review checkpoints
- [ ] psku_code → zsku bridge (`noondwh.zsku_catexsp.psku`) — analyst should confirm coverage; missing skus get cost=0 → understates missing_value
- [ ] `net_variance_qty` is signed (positive_variance vs negative_variance can cancel). using ABS for $ basis. analyst can validate this matches business intent
- [ ] `store_gmv` is yesterday's daily GMV from `odr_gmv_uae` — confirm vs. live order value

---

## agent 10 · incident stocktake % adherence

### purpose
flag stores with low completion rate on stocktake jobs.

### grain
ds × date (live snapshot, same-day)

### definition
`jobs_adherence_pct = jobs_completed / jobs_created × 100`
also surfaces `qty_adherence_pct` (line-level) for context.

### thresholds (vardan 2026-04-29; tier metric is the GAP, higher = worse)

| tier | rule |
|---|---|
| t3 | `adherence < 50%` (gap > 50) |
| t2 | `adherence 50% – 70%` (gap 30–50) |
| t1 | `adherence 70% – 85%` (gap 15–30) |

min count: ≥5 jobs

### sql

```sql
WITH base AS (
  SELECT
    w.partner_warehouse_code AS ds_code,
    j.id_job, j.id_status,
    CASE WHEN j.id_status = 3 THEN 1 ELSE 0 END AS is_completed
  FROM `noondwh.mxdcss_dcss.job` j
  JOIN `noondwh.mxdcss_dcss.warehouse` w ON w.id_warehouse = j.id_warehouse
  WHERE DATE(j.created_at) = CURRENT_DATE()
    AND w.id_country = 1                      -- ae=1 · sa=2 (saumy 2026-04-28)
    AND j.id_job_subtype = 10                 -- stock_take
),
line_counts AS (
  SELECT
    b.ds_code,
    COUNT(*) AS lines_total,
    SUM(CASE WHEN jl.id_status = 3 THEN 1 ELSE 0 END) AS lines_completed
  FROM base b
  LEFT JOIN `noondwh.mxdcss_dcss.job_line` jl ON jl.id_job = b.id_job
  GROUP BY b.ds_code
)
SELECT
  b.ds_code,
  COUNT(*) AS jobs_created,
  SUM(b.is_completed) AS jobs_completed,
  ROUND(SAFE_DIVIDE(SUM(b.is_completed), COUNT(*)) * 100, 1) AS jobs_adherence_pct,
  ANY_VALUE(lc.lines_total) AS qty_created,
  ANY_VALUE(lc.lines_completed) AS qty_completed,
  ROUND(SAFE_DIVIDE(ANY_VALUE(lc.lines_completed),
                    NULLIF(ANY_VALUE(lc.lines_total), 0)) * 100, 1) AS qty_adherence_pct
FROM base b LEFT JOIN line_counts lc USING (ds_code)
GROUP BY b.ds_code
HAVING jobs_created >= 5;
```

### lookups (for reference)

```sql
-- id_status
3  = 'completed'
12 = 'force_closed'
5  = 'pending'
7  = 'closed'
-- id_job_subtype
10 = 'stock_take'
1  = 'pick_item'
4  = 'putaway_item'
```

### review checkpoints
- [ ] id_status=3 (completed) is the right "success" signal — confirm vs. id_status=7 (closed) which is also a terminal state
- [ ] id_job_subtype=10 (stock_take) covers BOTH routine + incident stocktake. matrix said "incident" specifically — if incident-only is needed, add a process_type filter (process_type=10 routine, =8 stock_take)
- [ ] live snapshot: agent uses CURRENT_DATE()

---

## agent 11 · audit scores and status

### purpose
flag stores with low recent audit scores or repeat failures.

### grain
ds (latest weekly audit snapshot)

### definitions
- `score1` = latest audit score (out of 1.0)
- `score2/3/4` = prior 3 weekly audits
- `fails_in_4w` = COUNT of (score1..4 < 0.85)

### thresholds (vardan 2026-04-29)

| tier | rule |
|---|---|
| t3 | `score1 < 0.85` OR `fails_in_4w ≥ 3` |
| t2 | `score1 < 0.90` OR `fails_in_4w == 2` |
| t1 | `score1 < 0.95` OR `fails_in_4w == 1` |

### sql

```sql
SELECT
  partner_wh_code AS ds_code,
  DS_name AS ds_name,
  area_name_en,
  Fulfilment_AM AS am_name,
  lead_name,
  logistic_head_name,
  score1, score2, score3, score4,
  fail_status,
  audit_date,
  (CASE WHEN score1 IS NOT NULL AND score1 < 0.85 THEN 1 ELSE 0 END
   + CASE WHEN score2 IS NOT NULL AND score2 < 0.85 THEN 1 ELSE 0 END
   + CASE WHEN score3 IS NOT NULL AND score3 < 0.85 THEN 1 ELSE 0 END
   + CASE WHEN score4 IS NOT NULL AND score4 < 0.85 THEN 1 ELSE 0 END
  ) AS fails_in_4w
FROM `noonbinimksa.darkstore.historic_score1`
WHERE country_code = 'AE'                           -- uppercase!
  AND audit_date = (
    SELECT MAX(audit_date)
    FROM `noonbinimksa.darkstore.historic_score1`
    WHERE country_code = 'AE' AND audit_date <= CURRENT_DATE()
  );
```

### review checkpoints
- [ ] `country_code = 'AE'` is uppercase in this table (most others are lowercase). preserved
- [ ] fail threshold of 0.85 — confirm vs. the audit framework's official "fail" cut
- [ ] only latest snapshot is surfaced — check if `historic_score1` always has 156 UAE ds. recent run showed only 18 rows with score1 != null

---

## bottom-strip platform health KPIs

12 metrics surfaced at the top of the dashboard, refreshed every 60s with 5-min cache.

| # | metric | formula | source |
|---|---|---|---|
| 1 | % absenteeism | `(planned − present) / planned × 100` (uae cc-grain D-1) | Biometric_base_v2_3 |
| 2 | logged-in pickers | `COUNT(DISTINCT eid)` from `bio UNION ipp` | Biometric_base_v2_3 + ipp_daily_ae |
| 3 | p95 fulfilment | `APPROX_QUANTILES(fulfillment_time_per_ordrs)[OFFSET(95)] / 60.0` | speed_ae_hist |
| 4 | p95 fulfil → delivery | `APPROX_QUANTILES(TIMESTAMP_DIFF(delivered_at, fulfilled_at, SEC))[OFFSET(95)] / 60.0` | noonbinimlog.core.geomap |
| 5 | % defects | `def_orders / total_orders × 100` | complains_raw_all + ipp_daily_ae |
| 6 | % skips | `skips / picked_items × 100` | daily_manual_skips_hourly_uae_1 + ipp_daily_ae |
| 7 | fefo losses ($) | `SUM(ex_nl_value)` | fifo_report_logs |
| 8 | audit pass % | `COUNTIF(score1 ≥ 0.95) / total` | historic_score1 |
| 9 | ambient putaway adh | items ≤ 360 min / total ambient items | putaway_pendency_v2 |
| 10 | frozen putaway adh | items ≤ 30 min / total frozen items | putaway_pendency_v2 |
| 11 | chilled putaway adh | items ≤ 60 min / total chilled items | putaway_pendency_v2 |
| 12 | stocktake adh | `completed / overall` (status_desc='completed') | stock_take_base |

---

## change log

| date | scope | change |
|---|---|---|
| 2026-04-28 | all | initial 11-agent build wired to real BQ |
| 2026-04-28 | 02/03 | rolling cuts narrowed to p10/p20/p50 |
| 2026-04-28 | 06 | switched source to `fifo_report_logs` (not `central_expiry_raw`) |
| 2026-04-29 | 04 | thresholds 0.10/0.15/0.20% · min_count → 2 |
| 2026-04-29 | 05 | dropped sub-tabs · ds-level overall · 0.50/0.60/0.80% |
| 2026-04-29 | 06 | metric switched to % of orders · 0.30/0.50/1.00% |
| 2026-04-29 | 07 | adj_pct via cost_price_retail (proper live_inv_value) |
| 2026-04-29 | 08 | renamed → ds × storage_type rows · per-condition SLAs |
| 2026-04-29 | 09 | switched to %GMV thresholds · psku_code→zsku bridge |
| 2026-04-29 | 10 | renamed → "incident stocktake % adherence" |
| 2026-04-29 | 11 | added fails_in_4w |
| 2026-04-30 | 01 | **fix**: absent = planned − present (no Single Punch double-count) |
| 2026-04-30 | dashboard | KPI strip moved to TOP · added % absenteeism + logged-in pickers |
| 2026-04-30 | dashboard | create-draft modal with editable to/cc/subject/body |

---

## how to run

```
cd C:\Users\vnagar\Documents\nim-agents-ops
python -m agents.agent_01_attendance       # single agent
python -m agents.agent_05_defects
# all 11 agents · cadence per scheduler/windows_tasks.ps1
```

api: `localhost:5001` · dashboard: `dashboard/morpheus-dsops_commandcenter.html` (open via file://)

---

*end of reference. ping vardan if any check above is unclear or you find a math/source error.*
