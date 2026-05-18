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
    # complains_raw_sa lacks `minutes_category_new` (the canonical column lives in complains_raw_all).
    # for ksa we drop the dairy/milk breakdown — it's a payload-only column, not used in tiering.
    dairy_milk_expr = (
        "SUM(CASE WHEN complain_reason IN ('quality_not_fresh','ProductQuality_FungusorMold','bad_quality_item',"
        "'Presence_of_Foreign_Substance','Pest_Infestation','Presence_of_Worms') "
        "AND LOWER(minutes_category_new) IN ('milk','dairy & eggs') THEN 1 ELSE 0 END)"
        if country in ("ae", "eg") else "0"
    )

    # ipp_daily_sa lacks `total_orders` (only ipp_daily_ae has it).
    # for ksa, denominator comes from odr_gmv_ksa_today.orders (per-ds today count).
    if country == "ae":
        orders_cte = (
            "SELECT b.partner_Wh_code AS ds_code, SUM(a.order_cnt) AS orders "
            "FROM `noonbinimksa.darkstore.odr_uae_bh` a "
            "LEFT JOIN `noondwh.instant_instant_order.warehouse` b ON a.wh_code = b.wh_code "
            f"WHERE LOWER(a.country_code) = 'ae' AND a.created_date = DATE('{target_date}') "
            "GROUP BY b.partner_Wh_code"
        )
    else:
        # ksa: today's orders are in odr_gmv_ksa_today; D-1 and older are in odr_gmv_ksa_last30
        from datetime import date as _d
        is_today = str(target_date) == _d.today().isoformat()
        gmv_table = "odr_gmv_ksa_today" if is_today else "odr_gmv_ksa_last30"
        orders_cte = (
            "SELECT partner_Wh_code AS ds_code, SUM(orders) AS orders "
            f"FROM `noonbinimksa.darkstore.{gmv_table}` "
            f"WHERE order_date = DATE('{target_date}') GROUP BY partner_Wh_code"
        )

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
        {dairy_milk_expr} AS def_dairy_milk_quality
      FROM `noonbinimksa.darkstore.{table}`
      WHERE complain_date = DATE('{target_date}')
        AND country_code = '{country}'
      GROUP BY partner_wh_code
    ),
    o AS (
      {orders_cte}
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
            min_count_for_tier=3, use_or_logic=False, worst_n_floor=200,
        )


if __name__ == "__main__":
    print(DefectsAgent().run())
