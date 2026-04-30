# morpheus · agent thresholds (locked)

**last updated:** 2026-04-30 by vardan
**scope:** UAE · 156 darkstores · 11 agents

---

## tier model (gate-then-tier)

every agent applies the same 4-step gate:

1. **floor**       drop rows below the t1 threshold
2. **min count**   drop rows that don't meet the absolute-count safeguard (avoids false alarms on tiny volumes)
3. **tier**        assign t3 / t2 / t1 based on metric thresholds
4. **dedup**       skip if the same `(agent, sub_tab, ds_code)` was alerted in the last 48h

**tier colour code:** t3 = red (critical) · t2 = amber (watch) · t1 = blue (floor)
**deep-dive sort order:** t3 → t2 → t1, then by metric value descending

---

## the 11 agents

### agent 01 · attendance and absenteeism

- **grain:** ds × date (cc / temp), vendor (cross-stores)
- **sub-tabs:** cc today · cc d-1 · temp today · temp d-1 · vendor today · vendor d-1
- **metric:** `absent_pct = absent_count / planned × 100`
  - `active`  = total unique employees on roster
  - `planned` = active − week_off − annual_leave − other_leave
  - `present` = punched_properly OR single_punch
  - `absent`  = expected to show up but didn't
- **filter:**
  - cc:    `HR_Desig NOT LIKE '%TEMP%' AND HR_Desig IS NOT NULL`
  - temp:  `HR_Desig LIKE '%TEMP%'`
  - vendor: temp employees joined to vendor master via 3-step chain (excludes id_vendor=143 'non uae')
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `absent% > 8%` OR `absent_count > 10` |
| t2 | `absent% > 5%` OR `absent_count > 5` |
| t1 | `absent% > 3%` |

- **min count:** none
- **source:** `noonbinimksa.Stores.Biometric_base_v2_3` joined to `noonbinimksa.Stores.warehouse`

---

### agent 02 · iph pickers (outbound)

- **grain:** ds × date, ds × picker × date
- **sub-tabs:** overall d0 · picker d0 · overall d-1 · picker d-1
- **metric:** `iph = outbound_qty × 3600 / outbound_time_picking`
- **opd buckets:** small (<500 opd) · medium (500–1500) · large (1500+)
- **thresholds (rolling p10/p20/p50 within bucket, rebased nightly off L7D):**

| tier | rule |
|---|---|
| t3 | `iph < bucket.p10` |
| t2 | `iph < bucket.p20` (above p10) |
| t1 | `iph < bucket.p50` (above p20) |

- **min count (overall sub-tabs):** ≥10 jobs
- **min count (picker sub-tabs):** ≥50 outbound qty handled
- **source:** `noonbinimksa.darkstore.ipp_daily_ae`

---

### agent 03 · iph putaway (inbound)

- **grain:** ds × date, ds × picker × date
- **sub-tabs:** overall d0 · picker d0 · overall d-1 · picker d-1
- **metric:** `iph = completed_count × 3600 / total_sec`
- **thresholds:** same rolling p10/p20/p50 model as agent 02
- **min count:** same as agent 02
- **source:** `noonbinimops.fulfillment.ipp` (UAE) / `ipp_ksa` (KSA)

---

### agent 04 · skips (picker)

- **grain:** ds × date, ds × picker × date
- **sub-tabs:** store · picker
- **metric (store sub-tab):** `skip_pct = skips / picked_items × 100`
- **metric (picker sub-tab):** raw skip count per picker
- **thresholds (store sub-tab):**

| tier | rule |
|---|---|
| t3 | `skip% > 0.20%` |
| t2 | `skip%  0.15% – 0.20%` |
| t1 | `skip%  0.10% – 0.15%` |

- **min count:** ≥2 skips (lowered from 5 per vardan 2026-04-29)
- **thresholds (picker sub-tab, raw count):** t3 ≥20 · t2 ≥10 · t1 ≥3
- **source:** `noonbinimksa.darkstore.daily_manual_skips_hourly_uae_1` joined to `ipp_daily_ae` for picked qty

---

### agent 05 · defects (customer complaints)

- **grain:** ds × date (overall, not split by complain_reason)
- **metric:** `defect_rate_pct = def_orders / total_orders × 100`
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `defect% > 0.80%` |
| t2 | `defect%  0.60% – 0.80%` |
| t1 | `defect%  0.50% – 0.60%` |

- **min count:** ≥3 defective orders
- **source:** `noonbinimksa.darkstore.complains_raw_all` (UAE+EGY) / `complains_raw_sa` (KSA)
- **breakdown columns** in deep dive (no separate sub-tabs):
  - expired_items · near_expiry · dairy_milk_quality · fulfilment_miss_wrong · delivery_damage · quality

---

### agent 06 · fefo adherence

- **grain:** ds × date
- **metric:** `fefo_pct_orders = fefo_breach_skus / total_orders × 100`
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `fefo% > 1.0%` |
| t2 | `fefo%  0.5% – 1.0%` |
| t1 | `fefo%  0.3% – 0.5%` |

- **min count:** ≥3 breach skus
- **source:** `noonbinimdwh.modelling.fifo_report_logs` (latest updated_at per wh × sku × date_)
- **payload also surfaces** `ex_nl_value` ($) and `breach_units` (qty) for context

---

### agent 07 · adjustments in stores

- **grain:** ds × date (live snapshot, today)
- **metric:** `adj_pct = adj_value / live_inv_value × 100`
  - `adj_value` = positive_variance_value + |negative_variance_value|
  - `live_inv_value` = SUM(qty × cost_price) per ds
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `adj% > 0.50%`  AND `adj_value > $2,000` |
| t2 | `adj%  0.25%–0.50%`  AND `adj_value > $1,000` |
| t1 | `adj% > 0.10%`  AND `adj_value > $500` |

- **min count:** none
- **source:** `noonbinimops.fulfillment.adjustments_master_uae` joined to `noonbinimksa.Stores.wh_loc_qty` (live inv qty) joined to `noonbinimprc.pricing.cost_price_retail` (unit cost via zsku)

---

### agent 08 · putaway delays

- **grain:** ds × storage_type (live snapshot, today)
- **storage types:** ambient · chiller · frozen · ultrafresh · fnv (mapped from src_wh_code per saumy 2026-04-28)
- **metric:** `breach_qty` = items pending past the storage type's SLA
- **per-storage SLA:**

| storage | SLA |
|---|---|
| ambient    | > 360 min |
| chiller    | >  60 min |
| frozen     | >  30 min |
| ultrafresh | >  30 min |
| fnv        | >  30 min |

- **thresholds (on `breach_qty`):**

| tier | rule |
|---|---|
| t3 | `breach_qty > 50` |
| t2 | `breach_qty 25 – 50` |
| t1 | `breach_qty 10 – 25` |

- **source:** `noonbinimops.fulfillment.putaway_pendency_v2`

---

### agent 09 · missing inventory

- **grain:** ds × date (D-1)
- **metric:** `missing_value_pct = abs(missing_value) / store_gmv × 100`
  - `missing_qty` = SUM(ABS(net_variance)) across all skus
  - `missing_value` = qty × cost_price (psku_code → zsku → cost via zsku_catexsp.psku bridge)
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `missing > 0.30% of store GMV` |
| t2 | `missing  0.20% – 0.30%` |
| t1 | `missing  0.10% – 0.20%` |

- **min count:** none
- **source:** `noonbinimksa.darkstore.stock_take_base` + `noondwh.zsku_catexsp.psku` + `noonbinimprc.pricing.cost_price_retail` + `noonbinimksa.darkstore.odr_gmv_uae`

---

### agent 10 · incident stocktake % adherence

- **grain:** ds × date (live snapshot, today)
- **metric:** `jobs_adherence_pct = jobs_completed / jobs_created × 100`
  - tier metric stored as `adherence_gap_pct = 100 − adherence_pct` (higher gap = worse)
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `adherence < 50%` (gap > 50) |
| t2 | `adherence  50% – 70%` (gap 30–50) |
| t1 | `adherence  70% – 85%` (gap 15–30) |

- **min count:** ≥5 jobs
- **source:** `noondwh.mxdcss_dcss.job` (id_job_subtype=10 stock_take, id_status=12 force_closed) joined to `noondwh.mxdcss_dcss.warehouse` for country (id_country=1 ae, 2 sa)

---

### agent 11 · audit scores and status

- **grain:** ds (latest weekly snapshot)
- **metric:** `score1` (latest audit), plus `fails_in_4w` = count of (score1..score4 < 0.85)
- **thresholds:**

| tier | rule |
|---|---|
| t3 | `score1 < 0.85`  OR  `fails_in_4w ≥ 3` |
| t2 | `score1 < 0.90`  OR  `fails_in_4w == 2` |
| t1 | `score1 < 0.95`  OR  `fails_in_4w == 1` |

- **min count:** none
- **source:** `noonbinimksa.darkstore.historic_score1`
- **escalation override:** t3 → ds AM + lead_name + logistic_head_name (from row) + cc ali

---

## escalation routing summary

| agent / sub_tab | t1 | t2 | t3 |
|---|---|---|---|
| ds-grain (1 cc/temp · 2 · 3 · 4 · 5 · 6 · 7 · 8 · 9 · 10) | ds AM | ds AM + sharath | ds AM + sharath + ali (+ saro for inv-health agents) |
| 1 vendor sub-tab | vendor.email | vendor.email + sharath | vendor.email + sharath + harish |
| 11 audit | ds AM + lead + logistic_head | + sharath | + ali |

inventory-health agents (saro on cc t3): **agent_04, agent_05, agent_06, agent_09**

---

## bottom-strip platform health KPIs (10 metrics)

| # | metric | source |
|---|---|---|
| 1 | p95 fulfilment (mins, fulfilling → fulfilled) | `noonbinimops.fulfillment.speed_ae_hist` |
| 2 | p95 fulfilled → delivered (mins) | `noonbinimlog.core.geomap` |
| 3 | % defects | complains_raw_all / ipp_daily |
| 4 | % skips | daily_manual_skips × ipp_daily |
| 5 | fefo losses ($) | fifo_report_logs |
| 6 | audit pass % (score ≥ 0.95) | historic_score1 |
| 7 | ambient putaway adherence (≤ 360 min) | putaway_pendency_v2 |
| 8 | frozen putaway adherence (≤ 30 min) | putaway_pendency_v2 |
| 9 | chilled putaway adherence (≤ 60 min) | putaway_pendency_v2 |
| 10 | stocktake adherence | stock_take_base |

---

## change log

| date | agent | change |
|---|---|---|
| 2026-04-29 | 04 | thresholds tightened to 0.10/0.15/0.20% · min_count lowered to 2 |
| 2026-04-29 | 05 | dropped sub-tabs · ds-level overall · t3 > 0.8% · t2 0.6–0.8% · t1 > 0.5% |
| 2026-04-29 | 06 | metric switched to % of orders · t3 > 1.0% · t2 0.5–1.0% · t1 0.3–0.5% |
| 2026-04-29 | 07 | adj_pct via cost_price_retail (proper live_inv_value) |
| 2026-04-29 | 08 | renamed → ds × storage_type rows · per-condition SLAs |
| 2026-04-29 | 09 | thresholds switched to %GMV · t3 > 0.3% · t2 0.2–0.3% · t1 0.1–0.2% |
| 2026-04-29 | 10 | renamed → "incident stocktake % adherence" · jobs/qty completed metric |
| 2026-04-29 | 11 | added fails_in_4w computed from score1..4 |
| 2026-04-29 | 02/03 | rolling cuts narrowed to p10/p20/p50 · picker sub-tab min ≥50 qty |
| 2026-04-30 | all | dashboard sorts t3 → t2 → t1 · "create draft" preview popup |
