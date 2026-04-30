"""
agent 7 — adjustments in stores

source per suarya code repo:
  UAE: noonbinimops.fulfillment.adjustments_master_uae
  KSA: noonbinimops.fulfillment.adjustments_master_ksa
  EGY: noonbinimops.fulfillment.adjustments_master_eg

live inventory: noonbinimksa.Stores.wh_loc_qty
  schema: warehouse (partner_wh_code), zsku, qty, country_code, location, etc.
  no unit_cost in this table — joined to fifo_report_logs for unit_cost lookup
  per (ds_code, sku) over L7D.

primary metric (matrix v0.9): adj_pct = adj_value / live_inv_value
  thresholds: t3 > 0.50%, t2 0.25–0.50%, t1 > 0.10%

abs $ also surfaced as `adj_value` for context.
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run


def _query(target_date, country):
    suffix = {"ae": "uae", "sa": "ksa", "eg": "eg"}.get(country, "uae")
    return f"""
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
      FROM `noonbinimops.fulfillment.adjustments_master_{suffix}`
      WHERE put_date = DATE('{target_date}')
      GROUP BY wh_code
    ),
    -- unit_cost lookup per saumy pricing repo: noonbinimprc.pricing.cost_price_retail
    -- pick latest year_month per sku to handle SKUs with no current-month entry
    cost AS (
      SELECT sku, ANY_VALUE(cost_price) AS cost_price
      FROM (
        SELECT sku, cost_price,
               ROW_NUMBER() OVER (PARTITION BY sku ORDER BY year_month DESC) AS rn
        FROM `noonbinimprc.pricing.cost_price_retail`
        WHERE country_code = '{country}' AND cost_price > 0
      )
      WHERE rn = 1
      GROUP BY sku
    ),
    -- live inventory $ value per ds = SUM(qty * cost_price)
    inv AS (
      SELECT
        q.warehouse AS ds_code,
        SUM(q.qty) AS live_inv_units,
        ROUND(SUM(q.qty * IFNULL(CAST(c.cost_price AS FLOAT64), 0)), 2) AS live_inv_value,
        ROUND(SAFE_DIVIDE(SUM(CASE WHEN c.cost_price IS NOT NULL THEN q.qty ELSE 0 END),
                          NULLIF(SUM(q.qty), 0)) * 100, 1) AS cost_coverage_pct
      FROM `noonbinimksa.Stores.wh_loc_qty` q
      LEFT JOIN cost c ON c.sku = q.zsku
      WHERE LOWER(q.country_code) = '{country}'
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
    WHERE adj.adj_value > 0
    """


class AdjustmentsAgent(Agent):
    AGENT_ID = "agent_07_adjustments"
    CADENCE = "hourly"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # adjustments_master is fresh same-day; override D-1 default to today
        from datetime import date
        self.target_date = date.today()

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        # matrix v0.9 % thresholds, with absolute $ floor
        return TierSpec(
            metric_field="adj_pct", count_field="adj_value",
            floor=0.10,
            t3_metric=0.50, t3_count=2000,
            t2_metric=0.25, t2_count=1000,
            t1_metric=0.10, t1_count=500,
            use_or_logic=False, worst_n_floor=5,
        )


if __name__ == "__main__":
    print(AdjustmentsAgent().run())
