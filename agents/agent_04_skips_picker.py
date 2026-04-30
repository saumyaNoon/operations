"""
agent 4 — skips (picker)

per vardan 2026-04-29: 2 sub-tabs
  store        — ds × date, total skips per store (with AM/Supervisor/TL via dashboard join)
  picker       — ds × picker × date, picker name + #skips

source: noonbinimksa.darkstore.daily_manual_skips_hourly_uae_1
        joined to ipp_daily for picked-items denominator (store sub-tab only)
metric: skip_pct = skips / picked × 100  (store-grain)
        skips count                       (picker-grain — top offenders by raw count)
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run


def _store_query(target_date, country):
    skip_table = f"daily_manual_skips_hourly_{('uae' if country == 'ae' else 'ksa')}_1"
    pick_table = f"ipp_daily_{country}"
    return f"""
    WITH skips AS (
      SELECT
        partner_wh_code AS ds_code,
        SUM(items) AS skips,
        SUM(CASE WHEN LOWER(reason_) LIKE '%not_found%' OR LOWER(reason_) LIKE '%missing%'
                 THEN items ELSE 0 END) AS missing,
        SUM(CASE WHEN LOWER(reason_) LIKE '%damag%' THEN items ELSE 0 END) AS damaged,
        SUM(CASE WHEN LOWER(reason_) LIKE '%expir%' THEN items ELSE 0 END) AS expired
      FROM `noonbinimksa.darkstore.{skip_table}`
      WHERE DATE(date_) = DATE('{target_date}')
      GROUP BY partner_wh_code
    ),
    picks AS (
      SELECT store AS ds_code, SUM(outbound_qty) AS picked
      FROM `noonbinimksa.darkstore.{pick_table}`
      WHERE date = DATE('{target_date}')
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
    WHERE p.picked > 0 AND s.skips > 0
    """


def _picker_query(target_date, country):
    skip_table = f"daily_manual_skips_hourly_{('uae' if country == 'ae' else 'ksa')}_1"
    return f"""
    SELECT
      partner_wh_code AS ds_code,
      skipped_by AS picker_name,
      SUM(items) AS skips,
      SUM(CASE WHEN LOWER(reason_) LIKE '%not_found%' OR LOWER(reason_) LIKE '%missing%'
               THEN items ELSE 0 END) AS missing,
      SUM(CASE WHEN LOWER(reason_) LIKE '%damag%' THEN items ELSE 0 END) AS damaged,
      SUM(CASE WHEN LOWER(reason_) LIKE '%expir%' THEN items ELSE 0 END) AS expired
    FROM `noonbinimksa.darkstore.{skip_table}`
    WHERE DATE(date_) = DATE('{target_date}')
    GROUP BY partner_wh_code, skipped_by
    HAVING skips > 0
    ORDER BY skips DESC
    """


class SkipsPickerAgent(Agent):
    AGENT_ID = "agent_04_skips_picker"
    CADENCE = "hourly"
    SUB_TABS = ("store", "picker")

    def scan(self, sub_tab=None):
        td = str(self.target_date)
        if sub_tab == "store":
            return bq_run(_store_query(td, self.geo))
        if sub_tab == "picker":
            return bq_run(_picker_query(td, self.geo))
        return []

    def tier_spec(self, sub_tab=None):
        if sub_tab == "picker":
            # rank by raw skip count per picker
            return TierSpec(
                metric_field="skips", count_field="skips",
                floor=3,
                t3_metric=20, t2_metric=10, t1_metric=3,
                use_or_logic=False, worst_n_floor=10,
            )
        # store sub-tab — vardan 2026-04-29: t3 > 0.2%, t2 0.15-0.2%, t1 0.1-0.15%
        # min_count = 2 (vardan 2026-04-29) so low-volume but high-rate stores still surface
        return TierSpec(
            metric_field="skip_pct", count_field="skips",
            floor=0.10,
            t3_metric=0.20, t2_metric=0.15, t1_metric=0.10,
            min_count_for_tier=2, use_or_logic=False, worst_n_floor=5,
        )

    def row_key(self, row, sub_tab=None):
        if sub_tab == "picker":
            return f"{self.AGENT_ID}|picker|{row.get('ds_code','')}|{row.get('picker_name','')}"
        return f"{self.AGENT_ID}|store|{row.get('ds_code','')}"


if __name__ == "__main__":
    print(SkipsPickerAgent().run())
