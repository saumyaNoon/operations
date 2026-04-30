"""
nim-agents-ops api/lib/platform_health.py

computes the 10 platform-health KPIs shown in morpheus dashboard's bottom strip.
each metric is a single number (or a small dict) computed once and cached for
TTL_SECS to avoid hammering BQ on every dashboard reload.

10 metrics (per vardan 2026-04-28):
  1.  p95 fulfillment speed (mins, order created → fulfilled)
  2.  p95 fulfilled → delivered speed (mins)  [proxy: handover, last-mile]
  3.  % defects (def_orders / total_orders)
  4.  % skips (skip_items / picked_items)
  5.  fefo losses ($)
  6.  audit % pass stores (score1 ≥ 0.95)
  7.  % adherence ambient putaway (≤ 360 min)
  8.  % adherence frozen putaway  (≤ 30 min)
  9.  % adherence chilled putaway (≤ 30 min)
  10. stocktake adherence (completed / overall)
"""
import os, time
from datetime import date, timedelta

from .bigquery_client import run as bq_run

TTL_SECS = 300
_CACHE = {}


def _cached(key, fn):
    now = time.time()
    if key in _CACHE and now - _CACHE[key]["ts"] < TTL_SECS:
        return _CACHE[key]["val"]
    val = fn()
    _CACHE[key] = {"ts": now, "val": val}
    return val


def _yesterday():
    return str(date.today() - timedelta(days=1))


# ──────────────────────────────────────────────────────────────────────────────
# 1. p95 fulfillment speed (created → fulfilled, minutes)
# ──────────────────────────────────────────────────────────────────────────────
def p95_fulfillment_speed(country="ae"):
    """order-level p95 (fulfilling_at → fulfilled_at) from core.geomap.
    matches vardan's reported value 2026-04-28 (was 3.2 from speed_ae_hist
    pre-aggregated p95-of-store-day-averages — undercount)."""
    def _go():
        td = _yesterday()
        sql = f"""
        SELECT APPROX_QUANTILES(
          TIMESTAMP_DIFF(fulfilled_at, fulfilling_at, SECOND), 100
        )[OFFSET(95)] / 60.0 AS p95_mins
        FROM `noonbinimlog.core.geomap`
        WHERE date_ = DATE('{td}')
          AND LOWER(country_code) = '{country}'
          AND fulfilling_at IS NOT NULL
          AND fulfilled_at IS NOT NULL
          AND TIMESTAMP_DIFF(fulfilled_at, fulfilling_at, SECOND) > 0
        """
        r = bq_run(sql)
        return round(r[0]["p95_mins"] or 0, 1) if r else None
    return _cached(f"p95_fulfillment_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 2. p95 fulfilled → delivered speed (minutes)
#    proxy: from mp_order_history fulfilled→delivered timestamps
# ──────────────────────────────────────────────────────────────────────────────
def p95_fulfilled_to_delivered(country="ae"):
    """uses noonbinimlog.core.geomap (logistics dict 2026-04-28) which has clean
    fulfilled_at + delivered_at timestamps. one row per delivered order."""
    def _go():
        td = _yesterday()
        sql = f"""
        SELECT APPROX_QUANTILES(
          TIMESTAMP_DIFF(delivered_at, fulfilled_at, SECOND), 100
        )[OFFSET(95)] / 60.0 AS p95_mins
        FROM `noonbinimlog.core.geomap`
        WHERE date_ = DATE('{td}')
          AND LOWER(country_code) = '{country}'
          AND fulfilled_at IS NOT NULL
          AND delivered_at IS NOT NULL
          AND TIMESTAMP_DIFF(delivered_at, fulfilled_at, SECOND) > 0
        """
        try:
            r = bq_run(sql)
            return round(r[0]["p95_mins"] or 0, 1) if r else None
        except Exception:
            return None
    return _cached(f"p95_f2d_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 3. % defects = def_orders / total_orders × 100
# ──────────────────────────────────────────────────────────────────────────────
def pct_defects(country="ae"):
    def _go():
        td = _yesterday()
        defects_table = "complains_raw_all" if country in ("ae", "eg") else "complains_raw_sa"
        sql = f"""
        WITH d AS (
          SELECT COUNT(DISTINCT order_nr) AS def_orders
          FROM `noonbinimksa.darkstore.{defects_table}`
          WHERE complain_date = DATE('{td}') AND country_code = '{country}'
        ),
        o AS (
          SELECT SUM(total_orders) AS orders
          FROM `noonbinimksa.darkstore.ipp_daily_{country}`
          WHERE date = DATE('{td}')
        )
        SELECT ROUND(SAFE_DIVIDE(d.def_orders, NULLIF(o.orders, 0)) * 100, 3) AS pct,
               d.def_orders, o.orders
        FROM d, o
        """
        r = bq_run(sql)
        if not r: return None
        return {"pct": r[0]["pct"], "defects": r[0]["def_orders"], "orders": r[0]["orders"]}
    return _cached(f"pct_defects_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 4. % skips = skip_items / picked_items × 100
# ──────────────────────────────────────────────────────────────────────────────
def pct_skips(country="ae"):
    def _go():
        td = _yesterday()
        skip = f"daily_manual_skips_hourly_{('uae' if country == 'ae' else 'ksa')}_1"
        pick = f"ipp_daily_{country}"
        sql = f"""
        WITH s AS (
          SELECT SUM(items) AS skips
          FROM `noonbinimksa.darkstore.{skip}`
          WHERE DATE(date_) = DATE('{td}')
        ),
        p AS (
          SELECT SUM(outbound_qty) AS picked
          FROM `noonbinimksa.darkstore.{pick}`
          WHERE date = DATE('{td}')
        )
        SELECT ROUND(SAFE_DIVIDE(s.skips, NULLIF(p.picked, 0)) * 100, 3) AS pct,
               s.skips, p.picked
        FROM s, p
        """
        r = bq_run(sql)
        if not r: return None
        return {"pct": r[0]["pct"], "skips": r[0]["skips"], "picked": r[0]["picked"]}
    return _cached(f"pct_skips_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 5. fefo losses ($)
# ──────────────────────────────────────────────────────────────────────────────
def fefo_losses(country="ae"):
    def _go():
        td = _yesterday()
        sql = f"""
        WITH logs AS (
          SELECT DISTINCT a.*
          FROM `noonbinimdwh.modelling.fifo_report_logs` a
          JOIN (
            SELECT wh_code, sku, date_, MAX(updated_at) AS updated_at
            FROM `noonbinimdwh.modelling.fifo_report_logs`
            WHERE date_ = DATE('{td}')
            GROUP BY wh_code, sku, date_
          ) b USING (wh_code, sku, date_, updated_at)
          WHERE a.country_code = '{country}'
            AND a.date_ = DATE('{td}')
        )
        SELECT
          ROUND(SUM(IFNULL(ex_nl_value, 0)), 0) AS total_loss_value,
          SUM(CASE WHEN fefo_breach THEN 1 ELSE 0 END) AS skus_breached,
          COUNT(*) AS skus_total
        FROM logs
        """
        r = bq_run(sql)
        if not r: return None
        return {"value": r[0]["total_loss_value"], "skus_breached": r[0]["skus_breached"],
                "skus_total": r[0]["skus_total"]}
    return _cached(f"fefo_losses_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 6. audit % pass stores (score1 ≥ 0.95)
# ──────────────────────────────────────────────────────────────────────────────
def audit_pct_pass(country="ae"):
    def _go():
        cc_upper = country.upper()
        sql = f"""
        WITH latest AS (
          SELECT *, MAX(audit_date) OVER () AS max_d
          FROM `noonbinimksa.darkstore.historic_score1`
          WHERE country_code = '{cc_upper}'
        )
        SELECT
          COUNTIF(score1 >= 0.95) AS pass_stores,
          COUNT(*) AS total_stores,
          ROUND(SAFE_DIVIDE(COUNTIF(score1 >= 0.95), COUNT(*)) * 100, 1) AS pct
        FROM latest WHERE audit_date = max_d
        """
        r = bq_run(sql)
        if not r: return None
        return {"pct": r[0]["pct"], "pass": r[0]["pass_stores"], "total": r[0]["total_stores"]}
    return _cached(f"audit_pass_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 7-9. putaway adherence by storage condition (ambient / frozen / chilled)
#      SLA per Targets_Putaway_Temprature: ambient ≤ 360 min, frozen/chilled ≤ 30 min
#      proxy without storage_condition column: count items in pendency below SLA
#      labelled overall + bucketed approximation
# ──────────────────────────────────────────────────────────────────────────────
def putaway_adherence(country="ae"):
    """splits putaway pendency by storage type using saumy's src_wh_code → type mapping.
    SLA per Targets_Putaway_Temprature: ambient ≤ 360 min, frozen/chilled/UF/FnV ≤ 30 min."""
    def _go():
        td = _yesterday()
        sql = f"""
        WITH typed AS (
          SELECT
            ageing_in_mins,
            items_pending,
            CASE
              WHEN src_wh_code IN ('RUHID05','JEDID05','AUHFKZ01','AUHFKZ02','AUHFKZ03') THEN 'Frozen'
              WHEN src_wh_code IN ('JEDID01','JEDID02','RUHID01','RUHID02','CAIID01','CAIID02',
                                   'DXBID01','DXBID02','AUHID01','AUHID02','DXSID01','DXSID02') THEN 'Ambient'
              WHEN src_wh_code IN ('RUHID03','RUHID04','JEDID03','JEDID04',
                                   'AUHID03','AUHID04','DXBID03','DXBID04') THEN 'Chiller'
              WHEN src_wh_code IN ('RUHID07','JEDID07','AUHID07','DXBID07') THEN 'Ultrafresh'
              WHEN src_wh_code IN ('RUHID06','JEDID06','DXBFNV01') THEN 'FnV'
              ELSE 'Others'
            END AS type_
          FROM `noonbinimops.fulfillment.putaway_pendency_v2`
          WHERE date = DATE('{td}') AND LOWER(country_code) = '{country}'
        )
        SELECT
          type_,
          SUM(items_pending) AS total_items,
          SUM(CASE WHEN type_ = 'Ambient' AND ageing_in_mins <= 360 THEN items_pending
                   WHEN type_ <> 'Ambient' AND ageing_in_mins <= 30  THEN items_pending
                   ELSE 0 END) AS within_sla
        FROM typed
        GROUP BY type_
        """
        rows = bq_run(sql)
        out = {"ambient_pct": None, "frozen_pct": None, "chilled_pct": None,
               "ultrafresh_pct": None, "fnv_pct": None, "by_type": {}}
        for r in rows:
            t = r["type_"]
            tot = r["total_items"] or 0
            within = r["within_sla"] or 0
            pct = round(within / tot * 100, 1) if tot else None
            out["by_type"][t] = {"total": tot, "within_sla": within, "pct": pct}
            if t == "Ambient":     out["ambient_pct"] = pct
            elif t == "Frozen":    out["frozen_pct"] = pct
            elif t == "Chiller":   out["chilled_pct"] = pct
            elif t == "Ultrafresh":out["ultrafresh_pct"] = pct
            elif t == "FnV":       out["fnv_pct"] = pct
        return out
    return _cached(f"putaway_adh_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# 10. stocktake adherence (completed / overall) per saumy ref
# ──────────────────────────────────────────────────────────────────────────────
def stocktake_adherence(country="ae"):
    def _go():
        td = _yesterday()
        sql = f"""
        WITH base AS (
          SELECT a.*
          FROM `noonbinimksa.darkstore.stock_take_base` a
          WHERE LOWER(a.country_code) = '{country}'
            AND a.created_date = DATE('{td}')
            AND (status_desc IN ('completed','pending_approval') OR status_desc IS NULL)
        ),
        overall AS (
          SELECT COUNT(DISTINCT id_stock_take_job_queue) AS overall_jobs FROM base
        ),
        completed AS (
          SELECT COUNT(DISTINCT id_stock_take_job_queue) AS completed_jobs
          FROM base WHERE LOWER(status_desc) = 'completed'
        )
        SELECT
          ROUND(SAFE_DIVIDE(completed.completed_jobs, overall.overall_jobs) * 100, 1) AS pct,
          completed.completed_jobs AS completed,
          overall.overall_jobs AS overall
        FROM overall, completed
        """
        r = bq_run(sql)
        if not r: return None
        return {"pct": r[0]["pct"], "completed": r[0]["completed"], "overall": r[0]["overall"]}
    return _cached(f"stocktake_adh_{country}", _go)


# ──────────────────────────────────────────────────────────────────────────────
# bundle for the api endpoint
# ──────────────────────────────────────────────────────────────────────────────
def all_metrics(country="ae"):
    metrics = {}
    metrics["p95_fulfillment_mins"] = _safe(p95_fulfillment_speed, country)
    metrics["p95_fulfilled_to_delivered_mins"] = _safe(p95_fulfilled_to_delivered, country)
    metrics["pct_defects"] = _safe(pct_defects, country)
    metrics["pct_skips"] = _safe(pct_skips, country)
    metrics["fefo_losses"] = _safe(fefo_losses, country)
    metrics["audit_pct_pass"] = _safe(audit_pct_pass, country)
    metrics["putaway_adherence"] = _safe(putaway_adherence, country)
    metrics["stocktake_adherence"] = _safe(stocktake_adherence, country)
    return metrics


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return {"error": str(e)[:200]}
