"""
nim-agents-ops api/lib/routing.py

resolves (to_list, cc_list) per agent + tier. ds-grain agents route to the AM
from ds_routing; the vendor sub-tab on agent 1 routes to vendor.email; agent 11
audit overrides with lead_name + logistic_head_name from the row itself.

routing per PLAN.md:

  ds-grain (agent 1 cc/temp · 2 · 3 · 4 · 5 · 6 · 7 · 8 · 9 · 10):
    t1 → ds AM
    t2 → ds AM + sharath
    t3 → ds AM + sharath + ali (+ saro if inv health agent)

  agent 1 vendor sub-tab:
    t1 → vendor.email
    t2 → vendor.email + sharath
    t3 → vendor.email + sharath + harish

  agent 11 audit:
    t3 → ds AM + lead_name + logistic_head_name + sharath + ali

  agent 1 cc/temp t3 also adds harish (escalation chain for attendance)
"""
from .db import get_ds_routing, get_vendor_routing

# canonical email constants — single source of truth for fixed cc list
SHARATH = "sharath@noon.com"
ALI = "akh@noon.com"
HARISH = "hgaudi@noon.com"
SARO = "saro@noon.com"
VARDAN = "vnagar@noon.com"

# agent buckets (drives cc additions per tier)
INV_HEALTH_AGENTS = {
    "agent_04_skips_picker", "agent_05_defects",
    "agent_06_fefo", "agent_09_missing_inventory"
}
OPS_HYGIENE_AGENTS = {
    "agent_07_adjustments", "agent_08_putaway_delays", "agent_10_skips_stocktake"
}
MANPOWER_AGENTS = {
    "agent_01_attendance", "agent_02_iph_pickers", "agent_03_iph_putaway"
}


def _dedup(lst):
    seen, out = set(), []
    for x in lst:
        if x and x.strip() and x not in seen:
            seen.add(x); out.append(x)
    return out


def resolve_routing(agent, row, sub_tab=None, geo="uae"):
    """return (to_list, cc_list) for a draft.

    `row` should contain at least:
      - tier (1/2/3)
      - ds_code (or vendor_shortcode for vendor sub-tab)
      - audit-only: lead_email, logistic_head_email
    """
    tier = row.get("tier") or 1

    # agent 1 vendor sub-tab override
    if agent == "agent_01_attendance" and sub_tab == "vendor":
        v_email = row.get("vendor_email")
        # fall back to lookup by shortcode if not embedded
        if not v_email and row.get("vendor_shortcode"):
            v = get_vendor_routing(shortcode=row["vendor_shortcode"])
            if v:
                v_email = v.get("vendor_email")
        to = [v_email] if v_email else [VARDAN]
        cc = []
        if tier >= 2:
            cc.append(SHARATH)
        if tier >= 3:
            cc.append(HARISH)
        cc.append(VARDAN)
        return _dedup(to), _dedup(cc)

    # agent 11 audit override
    if agent == "agent_11_audit_scores":
        ds = row.get("ds_code") or ""
        ds_route = get_ds_routing(ds) or {}
        am_email = ds_route.get("am_email")
        to = [am_email] if am_email else [VARDAN]
        # lead + logistic_head come straight from the historic_score1 row
        if row.get("lead_email"):
            to.append(row["lead_email"])
        if row.get("logistic_head_email"):
            to.append(row["logistic_head_email"])
        cc = [SHARATH]
        if tier >= 3:
            cc.append(ALI)
        cc.append(VARDAN)
        return _dedup(to), _dedup(cc)

    # default ds-grain routing
    ds = row.get("ds_code") or ""
    ds_route = get_ds_routing(ds) or {}
    am_email = ds_route.get("am_email")
    to = [am_email] if am_email else [VARDAN]

    cc = []
    if tier >= 2:
        cc.append(SHARATH)
    if tier >= 3:
        # attendance cc/temp adds harish + ali; inv health adds saro; everything else just ali
        if agent == "agent_01_attendance" and sub_tab in ("cc", "temp"):
            cc.append(HARISH)
        cc.append(ALI)
        if agent in INV_HEALTH_AGENTS:
            cc.append(SARO)
    cc.append(VARDAN)
    return _dedup(to), _dedup(cc)
