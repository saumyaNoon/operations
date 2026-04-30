"""
nim-agents-ops agents/_base.py

base class every agent extends. owns the run lifecycle:

  scan() — agent-specific bigquery + pandas, returns list of candidate rows
  tier() — apply tiering.assign_tiers
  draft() — for each tier-bearing row, build draft + create gmail draft +
            log to alert_log + actions

each subclass needs to set:
  AGENT_ID    e.g. "agent_01_attendance"
  CADENCE     "daily" or "hourly"
  GEO_DEFAULT "uae"

and implement:
  scan(self, target_date) -> list of candidate row dicts (one per ds or vendor)

the rest is plumbing handled here.
"""
import os, sys
from datetime import datetime, date, timedelta

# add api/lib to path (agents are run as scripts from project root)
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from api.lib.db import (
    init_db, log_alert, was_alerted_recently, log_action,
    start_run, finish_run
)
from api.lib.tiering import assign_tiers, TierSpec
from api.lib.routing import resolve_routing
from api.lib.draft_builder import build_draft
from api.lib.gmail_client import create_draft_in_matrix


class Agent:
    AGENT_ID = "base"
    CADENCE = "daily"
    GEO_DEFAULT = "ae"            # 2-letter country code (matches BQ country_code col)
    SUB_TABS = (None,)            # overridden by attendance to ("cc","temp","vendor")

    # geo aliases — accept the 3-letter friendly form too (matches matrix v0.9 ds_area_manager)
    _GEO_ALIASES = {"uae": "ae", "ksa": "sa", "egy": "eg", "egp": "eg", "bhr": "bh", "qat": "qa"}

    def __init__(self, geo=None, target_date=None, dry_run=False):
        raw = (geo or self.GEO_DEFAULT).lower()
        self.geo = self._GEO_ALIASES.get(raw, raw)
        self.target_date = target_date or (date.today() - timedelta(days=1))
        self.dry_run = dry_run
        self.run_id = None
        init_db()

    # ── overrideable hooks ────────────────────────────────────────────────
    def scan(self, sub_tab=None):
        """return list of candidate row dicts for a given sub_tab.
        each row should carry the fields the tier spec + draft template need.
        """
        raise NotImplementedError

    def tier_spec(self, sub_tab=None) -> TierSpec:
        """return a TierSpec for the sub_tab. default = no-op (caller fills)."""
        raise NotImplementedError

    def row_key(self, row, sub_tab=None):
        """unique key for dedup. default ds_code; vendor sub-tabs override."""
        if sub_tab == "vendor":
            return f"{self.AGENT_ID}|vendor|{row.get('vendor_shortcode','')}"
        return f"{self.AGENT_ID}|{sub_tab or 'main'}|{row.get('ds_code','')}"

    # ── orchestration ────────────────────────────────────────────────────
    def run(self):
        """one full pass across all sub_tabs."""
        self.run_id = start_run(self.AGENT_ID)
        total_scanned = total_t1 = total_t2 = total_t3 = total_drafts = 0
        err = None

        try:
            for sub_tab in self.SUB_TABS:
                rows = self.scan(sub_tab=sub_tab) or []
                total_scanned += len(rows)
                spec = self.tier_spec(sub_tab=sub_tab)
                tiered = assign_tiers(rows, spec)

                for r in tiered:
                    tier = r.get("tier")
                    if tier == 1: total_t1 += 1
                    elif tier == 2: total_t2 += 1
                    elif tier == 3: total_t3 += 1

                    rk = self.row_key(r, sub_tab)
                    if was_alerted_recently(self.AGENT_ID, rk, hours=48):
                        continue

                    # vardan 2026-04-28: agents only compute + log breaches.
                    # drafts are created on-demand from the dashboard, not here.
                    log_alert(
                        self.AGENT_ID, rk, tier,
                        sub_tab=sub_tab,
                        ds_code=r.get("ds_code"),
                        vendor_shortcode=r.get("vendor_shortcode"),
                        metric_name=spec.metric_field,
                        metric_value=r.get(spec.metric_field),
                        contribution_pct=r.get(spec.contrib_field),
                        payload=r,
                        draft_id=None,
                        status="breach",
                    )
        except Exception as e:
            err = str(e)
            raise
        finally:
            finish_run(
                self.run_id,
                rows_scanned=total_scanned,
                t1=total_t1, t2=total_t2, t3=total_t3,
                drafts=total_drafts, error=err,
            )
        return {
            "agent": self.AGENT_ID,
            "scanned": total_scanned,
            "t1": total_t1, "t2": total_t2, "t3": total_t3,
            "drafts": total_drafts,
        }

    def _create_draft(self, row, sub_tab):
        if self.dry_run:
            return f"dryrun::{self.AGENT_ID}::{self.row_key(row, sub_tab)}"
        try:
            subject, body = build_draft(self.AGENT_ID, row, sub_tab=sub_tab,
                                        date_=str(self.target_date))
            to_list, cc_list = resolve_routing(self.AGENT_ID, row, sub_tab=sub_tab,
                                               geo=self.geo)
            return create_draft_in_matrix(to_list, cc_list, subject, body)
        except Exception as e:
            print(f"[{self.AGENT_ID}] draft creation failed for "
                  f"{self.row_key(row, sub_tab)}: {e}")
            return None
