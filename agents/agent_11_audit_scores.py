"""
agent 11 — audit scores and status

per vardan 2026-04-29: show store, score1, # times failed in last 4 weeks
  fail = score below 0.85 in any of score1 / score2 / score3 / score4

thresholds:
  t3 score1 < 0.85  OR  fails_in_4w >= 3
  t2 score1 < 0.90  OR  fails_in_4w == 2
  t1 score1 < 0.95  OR  fails_in_4w == 1
"""
import sys, os
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents._base import Agent
from api.lib.tiering import TierSpec
from api.lib.bigquery_client import run as bq_run

FAIL_BELOW = 0.85


def _query(target_date, country):
    cc_upper = country.upper()
    return f"""
    SELECT
      partner_wh_code AS ds_code,
      DS_name AS ds_name,
      area_name_en,
      Fulfilment_AM AS am_name,
      lead_name,
      logistic_head_name,
      score1, score2, score3, score4,
      fail_status,
      audit_date,
      (CASE WHEN score1 IS NOT NULL AND score1 < {FAIL_BELOW} THEN 1 ELSE 0 END
       + CASE WHEN score2 IS NOT NULL AND score2 < {FAIL_BELOW} THEN 1 ELSE 0 END
       + CASE WHEN score3 IS NOT NULL AND score3 < {FAIL_BELOW} THEN 1 ELSE 0 END
       + CASE WHEN score4 IS NOT NULL AND score4 < {FAIL_BELOW} THEN 1 ELSE 0 END
      ) AS fails_in_4w
    FROM `noonbinimksa.darkstore.historic_score1`
    WHERE country_code = '{cc_upper}'
      AND audit_date = (
        SELECT MAX(audit_date)
        FROM `noonbinimksa.darkstore.historic_score1`
        WHERE country_code = '{cc_upper}' AND audit_date <= DATE('{target_date}')
      )
    """


class AuditScoresAgent(Agent):
    AGENT_ID = "agent_11_audit_scores"
    CADENCE = "daily"

    def scan(self, sub_tab=None):
        return bq_run(_query(str(self.target_date), self.geo))

    def tier_spec(self, sub_tab=None):
        return TierSpec(metric_field="score1", floor=0.0)

    def run(self):
        from api.lib.db import was_alerted_recently, log_alert, start_run, finish_run

        self.run_id = start_run(self.AGENT_ID)
        scanned = drafts = t1 = t2 = t3 = 0
        err = None
        try:
            rows = self.scan()
            scanned = len(rows)
            for r in rows:
                s1 = r.get("score1")
                fails = r.get("fails_in_4w") or 0
                if s1 is None: continue

                tier = None
                if s1 < 0.85 or fails >= 3:
                    tier = 3
                elif s1 < 0.90 or fails == 2:
                    tier = 2
                elif s1 < 0.95 or fails == 1:
                    tier = 1
                if not tier: continue
                r["tier"] = tier
                if tier == 1: t1 += 1
                elif tier == 2: t2 += 1
                else: t3 += 1

                rk = self.row_key(r)
                if was_alerted_recently(self.AGENT_ID, rk, hours=48):
                    continue
                log_alert(self.AGENT_ID, rk, tier, ds_code=r.get("ds_code"),
                          metric_name="score1", metric_value=s1, payload=r,
                          draft_id=None, status="breach")
        except Exception as e:
            err = str(e); raise
        finally:
            finish_run(self.run_id, rows_scanned=scanned, t1=t1, t2=t2, t3=t3,
                       drafts=0, error=err)
        return {"agent": self.AGENT_ID, "scanned": scanned,
                "t1": t1, "t2": t2, "t3": t3, "drafts": 0}


if __name__ == "__main__":
    print(AuditScoresAgent().run())
