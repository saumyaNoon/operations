"""
agent 3 — iph putaway (inbound)

per vardan 2026-04-29: 4 sub-tabs (same shape as agent_02)
  overall_d0 / overall_d1 / picker_d0 / picker_d1

source: noonbinimops.fulfillment.ipp (UAE) / ipp_ksa (KSA) / ipp_all (EGY/BAH)
metric: iph_inbound = completed_count * 3600 / total_sec
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents.agent_02_iph_pickers import IphPickersAgent
from api.lib.bigquery_client import run as bq_run


def _ipp_table(country):
    if country == "ae":
        return "`noonbinimops.fulfillment.ipp`"
    if country == "sa":
        return "`noonbinimops.fulfillment.ipp_ksa`"
    return "`noonbinimksa.darkstore.ipp_all`"


def _ds_today_query(target_date, country):
    src = _ipp_table(country)
    return f"""
    SELECT
      dist_partner_code AS ds_code,
      SUM(completed_count) AS inbound_qty,
      SUM(total_sec) AS inbound_time,
      SAFE_DIVIDE(SUM(completed_count) * 3600.0,
                  NULLIF(SUM(total_sec), 0)) AS iph,
      COUNT(DISTINCT id_user) AS jobs
    FROM {src}
    WHERE DATE(date) = DATE('{target_date}')
    GROUP BY dist_partner_code
    HAVING SUM(total_sec) > 0
    """


def _picker_today_query(target_date, country):
    src = _ipp_table(country)
    return f"""
    SELECT
      dist_partner_code AS ds_code,
      id_user AS Employee_ID,
      SUM(completed_count) AS inbound_qty,
      SUM(total_sec) AS inbound_time,
      SAFE_DIVIDE(SUM(completed_count) * 3600.0,
                  NULLIF(SUM(total_sec), 0)) AS iph,
      1 AS jobs
    FROM {src}
    WHERE DATE(date) = DATE('{target_date}')
      AND id_user IS NOT NULL
    GROUP BY dist_partner_code, id_user
    HAVING inbound_time > 0
    """


def _l7d_query(target_date, country):
    src = _ipp_table(country)
    return f"""
    SELECT
      dist_partner_code AS ds_code,
      DATE(date) AS date,
      SAFE_DIVIDE(SUM(completed_count) * 3600.0,
                  NULLIF(SUM(total_sec), 0)) AS iph
    FROM {src}
    WHERE DATE(date) BETWEEN DATE_SUB(DATE('{target_date}'), INTERVAL 7 DAY)
                         AND DATE_SUB(DATE('{target_date}'), INTERVAL 1 DAY)
    GROUP BY dist_partner_code, DATE(date)
    HAVING SUM(total_sec) > 0
    """


class IphPutawayAgent(IphPickersAgent):
    AGENT_ID = "agent_03_iph_putaway"
    QUERY_TODAY_DS = staticmethod(_ds_today_query)
    QUERY_TODAY_PICKER = staticmethod(_picker_today_query)
    QUERY_L7D = staticmethod(_l7d_query)


if __name__ == "__main__":
    print(IphPutawayAgent().run())
