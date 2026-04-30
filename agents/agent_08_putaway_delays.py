"""
agent 8 — putaway delays (ds × storage_type grain)

per vardan 2026-04-29: emit one row per (ds_code, storage_type) so the
dashboard table shows ambient / chiller / frozen / UF / FnV breaches for
each store separately (matches availability-query pattern).

storage type via saumy's src_wh_code mapping (2026-04-28).
SLAs (matrix + availability query):
  ambient    > 360 min
  chiller    >  60 min
  frozen     >  30 min
  ultrafresh >  30 min
  fnv        >  30 min

per-row metric: breach_qty = items pending past that storage's SLA.
tier on breach_qty per ds-storage row.
"""
import sys, os
from datetime import date

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run


def _query(target_date, country):
    return f"""
    WITH typed AS (
      SELECT
        ds_code, ds_name, ageing_in_mins, items_pending,
        CASE
          WHEN src_wh_code IN ('RUHID05','JEDID05','AUHFKZ01','AUHFKZ02','AUHFKZ03') THEN 'frozen'
          WHEN src_wh_code IN ('JEDID01','JEDID02','RUHID01','RUHID02','CAIID01','CAIID02',
                               'DXBID01','DXBID02','AUHID01','AUHID02','DXSID01','DXSID02') THEN 'ambient'
          WHEN src_wh_code IN ('RUHID03','RUHID04','JEDID03','JEDID04',
                               'AUHID03','AUHID04','DXBID03','DXBID04') THEN 'chiller'
          WHEN src_wh_code IN ('RUHID07','JEDID07','AUHID07','DXBID07') THEN 'ultrafresh'
          WHEN src_wh_code IN ('RUHID06','JEDID06','DXBFNV01') THEN 'fnv'
          ELSE 'others'
        END AS storage_type
      FROM `noonbinimops.fulfillment.putaway_pendency_v2`
      WHERE date = DATE('{target_date}') AND LOWER(country_code) = '{country}'
    )
    SELECT
      ds_code, ANY_VALUE(ds_name) AS ds_name,
      storage_type,
      SUM(items_pending) AS total_pending,
      SUM(CASE
            WHEN storage_type = 'ambient'    AND ageing_in_mins > 360 THEN items_pending
            WHEN storage_type = 'chiller'    AND ageing_in_mins > 60  THEN items_pending
            WHEN storage_type IN ('frozen','ultrafresh','fnv') AND ageing_in_mins > 30 THEN items_pending
            ELSE 0 END) AS breach_qty,
      ROUND(SAFE_DIVIDE(
        SUM(CASE
              WHEN storage_type = 'ambient' AND ageing_in_mins > 360 THEN items_pending
              WHEN storage_type = 'chiller' AND ageing_in_mins > 60  THEN items_pending
              WHEN storage_type IN ('frozen','ultrafresh','fnv') AND ageing_in_mins > 30 THEN items_pending
              ELSE 0 END),
        NULLIF(SUM(items_pending), 0)) * 100, 1) AS breach_pct
    FROM typed
    WHERE storage_type <> 'others'
    GROUP BY ds_code, storage_type
    HAVING total_pending > 0
    """


class PutawayDelaysAgent(Agent):
    AGENT_ID = "agent_08_putaway_delays"
    CADENCE = "hourly"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.target_date = date.today()  # realtime snapshot

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        return TierSpec(
            metric_field="breach_qty", count_field="breach_qty",
            floor=10,
            t3_metric=50, t2_metric=25, t1_metric=10,
            use_or_logic=False, worst_n_floor=10,
        )

    def row_key(self, row, sub_tab=None):
        # one alert per (ds, storage_type)
        return f"{self.AGENT_ID}|{row.get('ds_code','')}|{row.get('storage_type','')}"


if __name__ == "__main__":
    print(PutawayDelaysAgent().run())
