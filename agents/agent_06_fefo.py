"""
agent 6 — fefo adherence

per vardan 2026-04-29: metric = fefo_breach_skus / total_orders × 100
                              (i.e. % of total orders at the darkstore)

source per saumy: noonbinimdwh.modelling.fifo_report_logs (modelled summary)
                  noonbinimksa.darkstore.odr_gmv_uae for store-day order count

logs CTE: pick max(updated_at) per (wh_code, sku, date_) per saumy ref.

thresholds (vardan 2026-04-29, calibrated to L7D distribution):
  t3 > 1.0%
  t2 0.5 – 1.0%
  t1 0.3 – 0.5%

payload also surfaces ex_nl_value ($), breach_units (qty), skus_breached for context.
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
    WITH logs AS (
      SELECT DISTINCT a.*
      FROM `noonbinimdwh.modelling.fifo_report_logs` a
      JOIN (
        SELECT wh_code, sku, date_, MAX(updated_at) AS updated_at
        FROM `noonbinimdwh.modelling.fifo_report_logs`
        WHERE date_ BETWEEN DATE_SUB(DATE('{target_date}'), INTERVAL 1 DAY)
                        AND DATE('{target_date}')
        GROUP BY wh_code, sku, date_
      ) b USING (wh_code, sku, date_, updated_at)
      WHERE a.country_code = '{country}'
        AND a.date_ = DATE('{target_date}')
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
      FROM `noonbinimksa.darkstore.{gmv_table}` g
      LEFT JOIN `noonbinimdwh.chatbot.warehouse_base_table` wb ON wb.wh_code = g.wh_code
      WHERE g.created_date_uae = DATE('{target_date}')
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
    WHERE g.orders > 0 AND f.skus_breached > 0
    """


class FefoAgent(Agent):
    AGENT_ID = "agent_06_fefo"
    CADENCE = "daily"

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        # vardan 2026-04-29: fefo skus_breached / total orders, %-of-orders thresholds
        return TierSpec(
            metric_field="fefo_pct_orders", count_field="skus_breached",
            floor=0.30,
            t3_metric=1.00, t2_metric=0.50, t1_metric=0.30,
            min_count_for_tier=3, use_or_logic=False, worst_n_floor=5,
        )


if __name__ == "__main__":
    print(FefoAgent().run())
