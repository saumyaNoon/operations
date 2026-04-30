"""
agent 1 — attendance and absenteeism

per vardan 2026-04-29: show 4 cols: # planned (rostered), # active, # absent,
% absenteeism. 2 sub-tabs per grain: today (D0) + D-1 (yesterday).

cc-grain        ds × date (HR_Desig NOT LIKE '%TEMP%' AND IS NOT NULL)
temp-grain      ds × date (HR_Desig LIKE '%TEMP%')
vendor-grain    vendor (cross-stores) via 3-step join chain (id_vendor != 143)

shared thresholds (matrix v0.9):
  t3 absent% > 8% OR absent_count > 10
  t2 absent% > 5% OR absent_count > 5
  t1 absent% > 3%

vardan 2026-04-29: also "trigger by severity of darkstore" — interpreted as
worst-first sort + fully populated AM/Supervisor/TL columns in the dashboard.

manpower CTE per saumy reference: Single Punch counts as present.
"""
import sys, os
from datetime import date, timedelta

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run

CC_DESIG = "(UPPER(b.HR_Desig) NOT LIKE '%TEMP%' AND b.HR_Desig IS NOT NULL)"
TEMP_DESIG = "UPPER(b.HR_Desig) LIKE '%TEMP%'"


def _ds_query(target_date, target_country, desig_filter):
    return f"""
    WITH manpower AS (
      SELECT
        b.wh_code,
        w.area_name_en,
        COUNT(DISTINCT b.employee_id) AS active,
        COUNT(DISTINCT CASE WHEN b.punch_status IN ('Punched Properly','Single Punch')
                            THEN b.employee_id END) AS present,
        COUNT(DISTINCT CASE WHEN b.punch_status = 'Week Off' THEN b.employee_id END) AS week_off,
        COUNT(DISTINCT CASE WHEN b.punch_status = 'Annual Leave' THEN b.employee_id END) AS al_leave,
        COUNT(DISTINCT CASE
                WHEN LOWER(b.punch_status) LIKE '%leave'
                 AND b.punch_status <> 'Annual Leave'
              THEN b.employee_id END) AS other_leave
      FROM `noonbinimksa.Stores.Biometric_base_v2_3` b
      LEFT JOIN `noonbinimksa.Stores.warehouse` w ON b.wh_code = w.partner_wh_code
      WHERE b.created_date = DATE('{target_date}')
        AND LOWER(w.country_code) = '{target_country}'
        AND {desig_filter}
      GROUP BY b.wh_code, w.area_name_en
    )
    SELECT
      wh_code AS ds_code,
      area_name_en AS ds_name,
      active,
      (active - week_off - al_leave - other_leave) AS rostered,  -- planned
      present,
      -- absent = planned - present (only people expected to show up but didn't punch)
      GREATEST((active - week_off - al_leave - other_leave) - present, 0) AS absent_count,
      ROUND(SAFE_DIVIDE(
        GREATEST((active - week_off - al_leave - other_leave) - present, 0),
        NULLIF(active - week_off - al_leave - other_leave, 0)) * 100, 1) AS absent_pct
    FROM manpower
    WHERE (active - week_off - al_leave - other_leave) > 0
    """


def _vendor_query(target_date, target_country):
    return f"""
    WITH bio AS (
      SELECT b.wh_code, b.employee_id, b.name, b.punch_status
      FROM `noonbinimksa.Stores.Biometric_base_v2_3` b
      LEFT JOIN `noonbinimksa.Stores.warehouse` w ON b.wh_code = w.partner_wh_code
      WHERE b.created_date = DATE('{target_date}')
        AND LOWER(w.country_code) = '{target_country}'
        AND UPPER(b.HR_Desig) LIKE '%TEMP%'
    ),
    name_join AS (
      SELECT bio.*, u.id_vendor
      FROM bio
      LEFT JOIN `noondwh.instantusers_cup.user` u
        ON LOWER(TRIM(bio.name)) = LOWER(TRIM(CONCAT(u.first_name, ' ', u.last_name)))
       AND u.is_active = 1
    ),
    prefix_lookup AS (
      SELECT SUBSTR(shortcode, 1, 3) AS pfx,
             ANY_VALUE(id_vendor) AS id_vendor,
             ANY_VALUE(name) AS vendor_name,
             ANY_VALUE(email) AS vendor_email,
             ANY_VALUE(shortcode) AS shortcode
      FROM `noondwh.instantusers_cup.vendor`
      WHERE is_active = 1 AND vendor_type = 'external'
      GROUP BY pfx
    ),
    matched AS (
      SELECT nj.*,
             COALESCE(nj.id_vendor, pl.id_vendor) AS final_id_vendor,
             pl.shortcode AS prefix_shortcode,
             pl.vendor_name AS prefix_vendor_name,
             pl.vendor_email AS prefix_vendor_email
      FROM name_join nj
      LEFT JOIN prefix_lookup pl
        ON REGEXP_EXTRACT(nj.employee_id, r'^([A-Z]+)') = pl.pfx
    ),
    enriched AS (
      SELECT m.*, v.shortcode AS v_shortcode, v.name AS v_name, v.email AS v_email
      FROM matched m
      LEFT JOIN `noondwh.instantusers_cup.vendor` v ON v.id_vendor = m.final_id_vendor
    )
    SELECT
      final_id_vendor AS id_vendor,
      COALESCE(v_shortcode, prefix_shortcode) AS vendor_shortcode,
      COALESCE(v_name, prefix_vendor_name) AS vendor_name,
      COALESCE(v_email, prefix_vendor_email) AS vendor_email,
      COUNT(DISTINCT wh_code) AS stores_affected,
      COUNT(DISTINCT employee_id) AS active,
      COUNT(DISTINCT CASE WHEN punch_status IN ('Punched Properly','Single Punch') THEN employee_id END) AS present,
      COUNT(DISTINCT CASE WHEN punch_status = 'Week Off' THEN employee_id END) AS week_off,
      COUNT(DISTINCT CASE WHEN punch_status = 'Annual Leave' THEN employee_id END) AS al_leave,
      COUNT(DISTINCT CASE WHEN LOWER(punch_status) LIKE '%leave' AND punch_status <> 'Annual Leave' THEN employee_id END) AS other_leave
    FROM enriched
    WHERE final_id_vendor IS NOT NULL AND final_id_vendor <> 143
    GROUP BY final_id_vendor, v_shortcode, prefix_shortcode, v_name,
             prefix_vendor_name, v_email, prefix_vendor_email
    HAVING (active - week_off - al_leave - other_leave) > 0
    """


# sub-tabs: cc_today / cc_d1 / temp_today / temp_d1 / vendor_today / vendor_d1
class AttendanceAgent(Agent):
    AGENT_ID = "agent_01_attendance"
    CADENCE = "daily"
    SUB_TABS = ("cc_today", "cc_d1", "temp_today", "temp_d1", "vendor_today", "vendor_d1")

    def _date_for(self, sub_tab):
        if sub_tab.endswith("_today"):
            return date.today()
        return date.today() - timedelta(days=1)

    def scan(self, sub_tab=None):
        if sub_tab is None: return []
        td = str(self._date_for(sub_tab))
        if sub_tab.startswith("cc_"):
            sql = _ds_query(td, self.geo, CC_DESIG)
        elif sub_tab.startswith("temp_"):
            sql = _ds_query(td, self.geo, TEMP_DESIG)
        elif sub_tab.startswith("vendor_"):
            sql = _vendor_query(td, self.geo)
        else:
            return []
        rows = bq_run(sql)
        if sub_tab.startswith("vendor_"):
            for r in rows:
                rostered = (r.get("active", 0) - (r.get("week_off") or 0)
                            - (r.get("al_leave") or 0) - (r.get("other_leave") or 0))
                r["rostered"] = rostered
                # absent = planned - present (vardan 2026-04-30)
                r["absent_count"] = max(rostered - (r.get("present") or 0), 0)
                r["absent_pct"] = (round((r["absent_count"] / rostered * 100), 1) if rostered else 0)
        return rows

    def tier_spec(self, sub_tab=None) -> TierSpec:
        return TierSpec(
            metric_field="absent_pct", count_field="absent_count",
            floor=3.0,
            t3_metric=8.0, t3_count=10,
            t2_metric=5.0, t2_count=5,
            t1_metric=3.0, t1_count=None,
            use_or_logic=True, worst_n_floor=5,
        )


if __name__ == "__main__":
    res = AttendanceAgent().run()
    print(res)
