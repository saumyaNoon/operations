"""
agent 9 — missing inventory in stores (GMV-based thresholds)

source: noonbinimksa.darkstore.stock_take_base + odr_gmv_uae for store GMV
        + cost_price_retail for unit cost (to compute $value of missing items)

per vardan 2026-04-29:
  metric: missing_value_pct = abs(net_variance_value) / store_gmv * 100
  show: missing_qty (units), missing_value ($), store_gmv ($), missing_value_pct
  thresholds:
    t3 > 0.3% of store GMV
    t2 > 0.2% of store GMV
    t1 > 0.1% of store GMV
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run


def _query(target_date, country):
    gmv_table = f"odr_gmv_{('uae' if country == 'ae' else 'ksa')}"
    return f"""
    WITH s AS (
      SELECT
        partner_warehouse_code AS ds_code,
        psku_code,
        SUM(CASE
              WHEN status_desc IN ('pending_approval','completed') AND job_line_status IN ('present','excess')
              THEN 1 ELSE 0 END) AS expected_qty,
        SUM(COALESCE(variance_, 0)) AS net_variance_qty
      FROM `noonbinimksa.darkstore.stock_take_base`
      WHERE created_date = DATE('{target_date}')
        AND LOWER(country_code) = '{country}'
      GROUP BY partner_warehouse_code, psku_code
    ),
    -- bridge psku_code (hash) to zsku_child via noondwh.zsku_catexsp.psku,
    -- then cost lookup uses zsku_child as 'sku' in cost_price_retail.
    psku_map AS (
      SELECT psku_code, ANY_VALUE(zsku_child) AS zsku FROM `noondwh.zsku_catexsp.psku`
      GROUP BY psku_code
    ),
    cost AS (
      SELECT sku, ANY_VALUE(cost_price) AS cost_price
      FROM (
        SELECT sku, cost_price,
               ROW_NUMBER() OVER (PARTITION BY sku ORDER BY year_month DESC) AS rn
        FROM `noonbinimprc.pricing.cost_price_retail`
        WHERE country_code = '{country}' AND cost_price > 0
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
    -- pull yesterday's gmv per ds; map W-prefix wh_code → partner_wh_code via warehouse_base_table
    gmv AS (
      SELECT
        wb.partner_wh_code AS ds_code,
        SUM(g.gmv) AS store_gmv,
        SUM(g.order_nr_cnt) AS orders
      FROM `noonbinimksa.darkstore.{gmv_table}` g
      LEFT JOIN `noonbinimdwh.chatbot.warehouse_base_table` wb ON wb.wh_code = g.wh_code
      WHERE g.created_date_uae = DATE('{target_date}')
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
    WHERE v.missing_qty > 0 AND g.store_gmv > 0
    """


class MissingInventoryAgent(Agent):
    AGENT_ID = "agent_09_missing_inventory"
    CADENCE = "hourly"

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        # vardan 2026-04-29: t3 > 0.3% of store GMV, t2 > 0.2%, t1 > 0.1%
        return TierSpec(
            metric_field="missing_value_pct", count_field="missing_value",
            floor=0.10,
            t3_metric=0.30, t2_metric=0.20, t1_metric=0.10,
            min_count_for_tier=None, use_or_logic=False, worst_n_floor=5,
        )


if __name__ == "__main__":
    print(MissingInventoryAgent().run())
