"""
agent 5 — defects (customer complaints)

per vardan 2026-04-29: ds-level overall defect rate (NOT sub-tabbed by reason).
metric: defect_rate_pct = def_orders / total_orders × 100  per partner_wh_code per day

thresholds:
  t3 > 0.8%
  t2 0.6-0.8%
  t1 > 0.5%

source per saumy:
  UAE + EGY: noonbinimksa.darkstore.complains_raw_all
  KSA:       noonbinimksa.darkstore.complains_raw_sa

breakdown columns surfaced in payload (no separate sub-tabs):
  def_expired_items, def_near_expiry, def_dairy_milk_quality,
  def_fulfill_miss_wrong, def_delivery_damage, def_quality, etc.
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run


def _query(target_date, country):
    table = "complains_raw_all" if country in ("ae", "eg") else "complains_raw_sa"
    return f"""
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
        SUM(CASE WHEN complain_reason = 'expired_item'
                 THEN 1 ELSE 0 END) AS def_expired_items,
        SUM(CASE WHEN complain_reason IN ('Limited_shelf_life','item_near_expiry','near_expiry','warranty_near_expiry')
                 THEN 1 ELSE 0 END) AS def_near_expiry,
        SUM(CASE WHEN complain_reason IN ('quality_not_fresh','ProductQuality_FungusorMold','bad_quality_item','Presence_of_Foreign_Substance','Pest_Infestation','Presence_of_Worms')
                  AND LOWER(minutes_category_new) IN ('milk','dairy & eggs')
                 THEN 1 ELSE 0 END) AS def_dairy_milk_quality
      FROM `noonbinimksa.darkstore.{table}`
      WHERE complain_date = DATE('{target_date}')
        AND country_code = '{country}'
      GROUP BY partner_wh_code
    ),
    o AS (
      SELECT store AS ds_code, SUM(total_orders) AS orders
      FROM `noonbinimksa.darkstore.ipp_daily_{country}`
      WHERE date = DATE('{target_date}')
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
    WHERE o.orders > 0
    """


class DefectsAgent(Agent):
    AGENT_ID = "agent_05_defects"
    CADENCE = "daily"
    SUB_TABS = (None,)  # ds-level only — breakdown columns surfaced in payload

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        # vardan 2026-04-29: t3 > 0.8%, t2 0.6-0.8%, t1 > 0.5%
        return TierSpec(
            metric_field="defect_rate_pct", count_field="def_orders",
            floor=0.50,
            t3_metric=0.80, t2_metric=0.60, t1_metric=0.50,
            min_count_for_tier=3, use_or_logic=False, worst_n_floor=5,
        )


if __name__ == "__main__":
    print(DefectsAgent().run())
