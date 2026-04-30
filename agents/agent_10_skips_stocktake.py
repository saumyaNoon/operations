"""
agent 10 — incident stocktake % adherence

renamed from "skips_stocktake" per vardan 2026-04-29.
metric: jobs_completed / jobs_created  (e.g. 50 created, 40 done = 80%)
        qty_completed / qty_created     (alternate basis)

source: noondwh.mxdcss_dcss.job + warehouse for country join
mappings:
  job_subtype.id = 10  → 'stock_take'
  status.id      = 3   → 'completed'
  status.id      = 12  → 'force_closed'
  status.id      = 5   → 'pending'
  warehouse.id_country: 1=ae, 2=sa

filter to incident stocktake (excludes routine via process_type if needed; for
now subtype=10 covers both incident + routine, lookup table only has 1 stock_take
subtype). a job has lines; we count both job-level and line-level adherence.

thresholds (lower is worse):
  t3 adherence < 50%
  t2 adherence 50-70%
  t1 adherence 70-85%
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run

COUNTRY_ID = {"ae": 1, "sa": 2}


def _query(target_date, country):
    id_country = COUNTRY_ID.get(country, 1)
    return f"""
    WITH base AS (
      SELECT
        w.partner_warehouse_code AS ds_code,
        j.id_job, j.id_status,
        CASE WHEN j.id_status = 3 THEN 1 ELSE 0 END AS is_completed
      FROM `noondwh.mxdcss_dcss.job` j
      JOIN `noondwh.mxdcss_dcss.warehouse` w ON w.id_warehouse = j.id_warehouse
      WHERE DATE(j.created_at) = DATE('{target_date}')
        AND w.id_country = {id_country}
        AND j.id_job_subtype = 10
    ),
    -- job-line counts via job_line for qty-level adherence
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
    HAVING jobs_created >= 5
    """


class IncidentStocktakeAdherenceAgent(Agent):
    AGENT_ID = "agent_10_skips_stocktake"      # keep DB id stable
    AGENT_NAME = "incident stocktake % adherence"
    CADENCE = "hourly"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # mxdcss_dcss.job is realtime; use today
        from datetime import date
        self.target_date = date.today()

    def scan(self, sub_tab=None):
        rows = bq_run(_query(str(self.target_date), self.geo))
        # invert metric: lower adherence = higher tier
        # store negative-adherence so tier logic (higher = worse) works directly
        for r in rows:
            adh = r.get("jobs_adherence_pct") or 0
            r["adherence_gap_pct"] = round(100.0 - adh, 1)
        return rows

    def tier_spec(self, sub_tab=None):
        # adherence_gap_pct = 100 - adherence%; higher gap = worse
        # t3: gap > 50 (adherence < 50%)
        # t2: gap 30-50 (adherence 50-70%)
        # t1: gap 15-30 (adherence 70-85%)
        return TierSpec(
            metric_field="adherence_gap_pct", count_field="jobs_created",
            floor=15.0,
            t3_metric=50.0, t2_metric=30.0, t1_metric=15.0,
            min_count_for_tier=5, use_or_logic=False, worst_n_floor=5,
        )


# legacy alias
SkipsStocktakeAgent = IncidentStocktakeAdherenceAgent

if __name__ == "__main__":
    print(IncidentStocktakeAdherenceAgent().run())
