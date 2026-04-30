"""
agent 2 — iph pickers (outbound)

per vardan 2026-04-29: 4 sub-tabs
  overall_d0       — ds-grain, today
  overall_d1       — ds-grain, yesterday
  picker_d0        — ds × picker, today
  picker_d1        — ds × picker, yesterday

source: noonbinimksa.darkstore.ipp_daily_ae (UAE) / ipp_daily_sa (KSA)
metric: iph = outbound_qty * 3600 / outbound_time_picking

option A cuts (vardan 2026-04-28): t3 < bucket.p10, t2 p10-p20, t1 p20-p50
"""
import sys, os
from datetime import date, timedelta

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec, opd_bucket, percentiles
from api.lib.bigquery_client import run as bq_run
from api.lib.db import save_thresholds


def _ds_today_query(target_date, country):
    table = f"ipp_daily_{country}"
    return f"""
    SELECT
      store AS ds_code,
      SUM(total_orders) AS opd,
      SUM(outbound_qty) AS outbound_qty,
      SUM(outbound_time_picking) AS outbound_time,
      SAFE_DIVIDE(SUM(outbound_qty) * 3600.0,
                  NULLIF(SUM(outbound_time_picking), 0)) AS iph,
      COUNT(DISTINCT Employee_ID) AS jobs
    FROM `noonbinimksa.darkstore.{table}`
    WHERE date = DATE('{target_date}')
    GROUP BY store
    HAVING SUM(outbound_time_picking) > 0
    """


def _picker_today_query(target_date, country):
    table = f"ipp_daily_{country}"
    return f"""
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
    FROM `noonbinimksa.darkstore.{table}`
    WHERE date = DATE('{target_date}')
      AND Employee_ID IS NOT NULL
    GROUP BY store, Employee_ID
    HAVING outbound_time > 0
    """


def _l7d_query(target_date, country):
    table = f"ipp_daily_{country}"
    return f"""
    SELECT
      store AS ds_code,
      date,
      SUM(total_orders) AS opd,
      SAFE_DIVIDE(SUM(outbound_qty) * 3600.0,
                  NULLIF(SUM(outbound_time_picking), 0)) AS iph
    FROM `noonbinimksa.darkstore.{table}`
    WHERE date BETWEEN DATE_SUB(DATE('{target_date}'), INTERVAL 7 DAY)
                   AND DATE_SUB(DATE('{target_date}'), INTERVAL 1 DAY)
    GROUP BY store, date
    HAVING SUM(outbound_time_picking) > 0
    """


def _rebase_thresholds(rows, agent_id):
    buckets = {"small": [], "medium": [], "large": []}
    for r in rows:
        b = opd_bucket(r.get("opd"))
        iph = r.get("iph")
        if iph is not None and iph > 0:
            buckets[b].append(iph)
    out = {}
    for b, vals in buckets.items():
        ps = percentiles(vals, (10, 20, 50))
        out[b] = {"cut1": ps["p10"], "cut2": ps["p20"], "cut3": ps["p50"]}
        save_thresholds(agent_id, b, ps["p10"], ps["p20"], ps["p50"])
    return out


class IphPickersAgent(Agent):
    AGENT_ID = "agent_02_iph_pickers"
    CADENCE = "hourly"
    SUB_TABS = ("overall_d0", "overall_d1", "picker_d0", "picker_d1")
    QUERY_TODAY_DS = staticmethod(_ds_today_query)
    QUERY_TODAY_PICKER = staticmethod(_picker_today_query)
    QUERY_L7D = staticmethod(_l7d_query)

    def _enrich(self, rows, thresholds):
        for r in rows:
            b = opd_bucket(r.get("opd"))
            t = thresholds.get(b, {})
            r["opd_bucket"] = b
            r["cut1"] = t.get("cut1", 0)
            r["cut2"] = t.get("cut2", 0)
            r["cut3"] = t.get("cut3", 0)
            r["iph"] = round(r.get("iph") or 0, 1)
        return rows

    def _date_for(self, sub_tab):
        return date.today() if sub_tab.endswith("_d0") else date.today() - timedelta(days=1)

    def scan(self, sub_tab=None):
        if sub_tab is None: return []
        td = str(self._date_for(sub_tab))
        # opd-bucket thresholds always rebased off L7D ending the day before today
        l7d_end = str(date.today())
        l7d = bq_run(self.QUERY_L7D(l7d_end, self.geo))
        thresholds = _rebase_thresholds(l7d, self.AGENT_ID)
        if sub_tab.startswith("overall_"):
            today = bq_run(self.QUERY_TODAY_DS(td, self.geo))
        else:  # picker_*
            today = bq_run(self.QUERY_TODAY_PICKER(td, self.geo))
        return self._enrich(today, thresholds)

    def tier_spec(self, sub_tab=None):
        # picker grain produces thousands of (ds × picker × day) rows; require the
        # picker has handled real volume before flagging. min_count = qty cutoff.
        if sub_tab and sub_tab.startswith("picker_"):
            return TierSpec(metric_field="iph", count_field="outbound_qty",
                            floor=0.0, min_count_for_tier=50, worst_n_floor=20)
        # ds grain: matrix v0.9 ≥10 jobs
        return TierSpec(metric_field="iph", count_field="jobs", floor=0.0,
                        min_count_for_tier=10, worst_n_floor=10)

    def row_key(self, row, sub_tab=None):
        if sub_tab and sub_tab.startswith("picker_"):
            return f"{self.AGENT_ID}|{sub_tab}|{row.get('ds_code','')}|{row.get('Employee_ID','')}"
        return f"{self.AGENT_ID}|{sub_tab or 'main'}|{row.get('ds_code','')}"

    def run(self):
        from api.lib.db import was_alerted_recently, log_alert, start_run, finish_run
        self.run_id = start_run(self.AGENT_ID)
        scanned = drafts = t1 = t2 = t3 = 0
        err = None
        try:
            for sub_tab in self.SUB_TABS:
                rows = self.scan(sub_tab=sub_tab) or []
                scanned += len(rows)
                spec = self.tier_spec(sub_tab=sub_tab)
                for r in rows:
                    jobs = r.get("jobs") or 0
                    if jobs < (spec.min_count_for_tier or 0):
                        continue
                    iph = r.get("iph") or 0
                    cut1, cut2, cut3 = r.get("cut1", 0), r.get("cut2", 0), r.get("cut3", 0)
                    tier = None
                    if iph < cut1 and cut1 > 0: tier = 3
                    elif iph < cut2 and cut2 > 0: tier = 2
                    elif iph < cut3 and cut3 > 0: tier = 1
                    if not tier: continue
                    r["tier"] = tier
                    if tier == 1: t1 += 1
                    elif tier == 2: t2 += 1
                    else: t3 += 1
                    rk = self.row_key(r, sub_tab)
                    if was_alerted_recently(self.AGENT_ID, rk, hours=48):
                        continue
                    log_alert(self.AGENT_ID, rk, tier, sub_tab=sub_tab,
                              ds_code=r.get("ds_code"),
                              metric_name="iph", metric_value=iph,
                              payload=r, draft_id=None, status="breach")
        except Exception as e:
            err = str(e); raise
        finally:
            finish_run(self.run_id, rows_scanned=scanned, t1=t1, t2=t2, t3=t3,
                       drafts=0, error=err)
        return {"agent": self.AGENT_ID, "scanned": scanned,
                "t1": t1, "t2": t2, "t3": t3, "drafts": 0}


if __name__ == "__main__":
    print(IphPickersAgent().run())
