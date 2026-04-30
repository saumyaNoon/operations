"""
nim-agents-ops api/lib/draft_builder.py

one template per agent (and sub-tab). returns (subject, body_html).

style rules (mirrors nim-agents-sc, vardan's preferences):
  - subject prefix `[ds ops-<agent_short>][uae][t{n}][...]`
  - lowercase prose
  - numbered bullets
  - no em dashes, no bold, no period at sentence ends
  - sign off "vardan" on a new line
  - acronyms only in caps: UAE OOS DS WH IPP AM DTS

each template takes a row dict + sub_tab + date_ and returns the html body.
the dispatcher `build_draft(agent, row, sub_tab, date_)` picks the right one.
"""

def _wrap(content):
    return (f'<html><body style="font-family:arial,sans-serif;font-size:11pt;'
            f'color:#333;line-height:1.55">{content}</body></html>')


def _t(row):
    return f"t{row.get('tier', 0)}"


def _ds_label(row):
    code = row.get("ds_code", "")
    name = row.get("ds_name") or row.get("name") or ""
    return f"{code}" + (f" ({name})" if name and name != code else "")


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 1 — attendance, 3 sub-tabs
# ──────────────────────────────────────────────────────────────────────────────
def _build_attendance_ds(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    pct = row.get("absent_pct", 0)
    cnt = row.get("absent_count", 0)
    rostered = row.get("rostered", 0)
    bucket = "cc" if sub_tab == "cc" else "temp"
    title = "core colleagues" if sub_tab == "cc" else "temp staff"

    subject = f"[ds ops-attendance][uae][{_t(row)}][{bucket}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging {title} attendance breach at {ds} as of {date_}</p>
<ol>
<li>absent {cnt} of {rostered} rostered, {pct}%</li>
<li>rostered excludes week off + annual + sick + comp off + other planned leave</li>
<li>punch_status breakdown: no punch / single punch counted as absent</li>
</ol>
<p>please confirm shift cover plan for today and root cause for absentees</p>
<p>vardan</p>""")
    return subject, body


def _build_attendance_vendor(row, sub_tab, date_):
    short = row.get("vendor_shortcode", "")
    name = row.get("vendor_name", "")
    pct = row.get("absent_pct", 0)
    cnt = row.get("absent_count", 0)
    rostered = row.get("rostered", 0)
    stores = row.get("stores_affected") or row.get("ds_count", 0)

    subject = f"[ds ops-attendance][uae][{_t(row)}][vendor][{short}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging vendor attendance breach for {name} ({short}) as of {date_}</p>
<ol>
<li>absent {cnt} of {rostered} rostered across {stores} darkstores, {pct}%</li>
<li>rostered excludes week off + annual + sick + comp off + other planned leave</li>
<li>shortcode prefix matched via biometric employee_id pattern</li>
</ol>
<p>please confirm cover plan and root cause for the absentees</p>
<p>vardan</p>""")
    return subject, body


def build_attendance(row, sub_tab, date_):
    if sub_tab == "vendor":
        return _build_attendance_vendor(row, sub_tab, date_)
    return _build_attendance_ds(row, sub_tab, date_)


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 2 — iph pickers (outbound)
# ──────────────────────────────────────────────────────────────────────────────
def build_iph_pickers(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    iph = row.get("iph", 0)
    jobs = row.get("jobs", 0)
    bucket = row.get("opd_bucket", "?")
    p20 = row.get("p20", 0)
    p50 = row.get("p50", 0)
    opd = row.get("opd", 0)

    subject = f"[ds ops-iph-pick][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging picker iph below {bucket}-bucket cut at {ds} as of {date_}</p>
<ol>
<li>ds iph {iph}, {jobs} jobs, store opd {opd}</li>
<li>{bucket}-bucket cuts: p20 {p20} / p50 {p50}</li>
<li>thresholds rebased nightly off L7D</li>
</ol>
<p>please review picker training and shift composition for today</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 3 — iph putaway (inbound)
# ──────────────────────────────────────────────────────────────────────────────
def build_iph_putaway(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    iph = row.get("iph", 0)
    jobs = row.get("jobs", 0)
    bucket = row.get("opd_bucket", "?")
    p20 = row.get("p20", 0)
    p50 = row.get("p50", 0)
    opd = row.get("opd", 0)

    subject = f"[ds ops-iph-putaway][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging putaway iph below {bucket}-bucket cut at {ds} as of {date_}</p>
<ol>
<li>ds inbound iph {iph}, {jobs} jobs, store opd {opd}</li>
<li>{bucket}-bucket cuts: p20 {p20} / p50 {p50}</li>
<li>thresholds rebased nightly off L7D</li>
</ol>
<p>please review inbound staffing and dock-to-stock handover</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 4 — skips (picker)
# ──────────────────────────────────────────────────────────────────────────────
def build_skips_picker(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    pct = row.get("skip_pct", 0)
    skips = row.get("skips", 0)
    picked = row.get("picked", 0)
    miss = row.get("missing", 0)
    dam = row.get("damaged", 0)
    exp = row.get("expired", 0)

    subject = f"[ds ops-skips-pick][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging picker skip rate at {ds} as of {date_}</p>
<ol>
<li>{skips} skips on {picked} items, {pct}%</li>
<li>missing {miss}, damaged {dam}, expired {exp}</li>
</ol>
<p>please drill into top-skipped skus and confirm shelf vs system mismatch</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 5 — defects
# ──────────────────────────────────────────────────────────────────────────────
def build_defects(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    rate = row.get("defect_rate_pct", 0)
    defects = row.get("def_orders", 0)
    orders = row.get("orders", 0)
    sub_label = {
        "expired":            "expired-item",
        "near_expiry":        "near-expiry",
        "dairy_milk_quality": "dairy/milk quality",
    }.get(sub_tab, "defects")
    sub_slug = (sub_tab or "all").replace("_", "-")

    subject = f"[ds ops-defects][uae][{_t(row)}][{sub_slug}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging {sub_label} complaint rate at {ds} as of {date_}</p>
<ol>
<li>{defects} {sub_label} complaints across {orders} orders, {rate}%</li>
<li>complain_reason filter applied per saumy ref (vardan 2026-04-28)</li>
</ol>
<p>please pull yesterday's tickets for these reasons and confirm whether shelf-life / category-quality is the root cause</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 6 — fefo adherence
# ──────────────────────────────────────────────────────────────────────────────
def build_fefo(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    units = row.get("breach_units", 0)
    val = row.get("ex_nl_value", 0)
    adj_units = row.get("breach_units_adjusted", 0)
    adj_val = row.get("ex_nl_value_adjusted", 0)

    subject = f"[ds ops-fefo][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging fefo adherence breach at {ds} as of {date_}</p>
<ol>
<li>system view: {units} units / ${val} expected near-loss value</li>
<li>kl-adjusted view: {adj_units} units / ${adj_val}</li>
<li>thresholds calibrated against L30D distribution, rebased monthly</li>
</ol>
<p>please pull the fefo violations list and confirm shelf rotation for top-loss skus</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 7 — adjustments
# ──────────────────────────────────────────────────────────────────────────────
def build_adjustments(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    pct = row.get("adj_pct", 0)
    val = row.get("adj_value", 0)
    up = row.get("adj_up_value", 0)
    down = row.get("adj_down_value", 0)
    inv = row.get("live_inv_value", 0)

    subject = f"[ds ops-adjustments][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging stock adjustment value at {ds} as of {date_}</p>
<ol>
<li>net adj ${val} on ${inv} live inventory, {pct}%</li>
<li>adj-up ${up}, adj-down ${down}</li>
</ol>
<p>please share reason codes for top adjustments and confirm whether physical-system reconciliation completed</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 8 — putaway delays
# ──────────────────────────────────────────────────────────────────────────────
def build_putaway_delays(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    over_6 = row.get("qty_above_6hr", 0)
    over_3 = row.get("qty_3_to_6hr", 0)
    fresh = row.get("qty_0_to_3hr", 0)
    total = row.get("total_pending", 0)
    pct = row.get("share_above_6hr_pct", 0)

    subject = f"[ds ops-putaway][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging store putaway pendency at {ds} as of {date_}</p>
<ol>
<li>0-3hr {fresh} units, 3-6hr {over_3} units, &gt;6hr {over_6} units</li>
<li>total pending {total}, {pct}% beyond 6hr</li>
</ol>
<p>please confirm putaway shift staffing and box-clearance plan</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 9 — missing inventory
# ──────────────────────────────────────────────────────────────────────────────
def build_missing_inventory(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    var_pct = row.get("variance_pct", 0)
    abs_diff = row.get("abs_diff", 0)
    expected = row.get("expected_qty", 0)
    pos = row.get("st_pos_variance", 0)
    neg = row.get("st_neg_variance", 0)

    subject = f"[ds ops-missing-inv][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging stocktake variance at {ds} as of {date_}</p>
<ol>
<li>net variance {abs_diff} units on {expected} expected, {var_pct}%</li>
<li>positive variance {pos}, negative variance {neg}</li>
</ol>
<p>please share top variance skus and confirm full-location adherence for the run</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 10 — skips (incident stocktake)
# ──────────────────────────────────────────────────────────────────────────────
def build_skips_stocktake(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    pct = row.get("force_closed_pct", 0)
    closed = row.get("force_closed_jobs", 0)
    total = row.get("total_jobs", 0)

    subject = f"[ds ops-skips-stocktake][uae][{_t(row)}][{code}]"
    body = _wrap(f"""
<p>hi team</p>
<p>flagging incident-stocktake force-closure rate at {ds} as of {date_}</p>
<ol>
<li>{closed} of {total} jobs force-closed, {pct}%</li>
<li>distinct from picker-skip — these are stocktake jobs abandoned mid-flight</li>
</ol>
<p>please review job-level reason codes and confirm whether ops is short-staffed during stocktake windows</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# AGENT 11 — audit scores
# ──────────────────────────────────────────────────────────────────────────────
def build_audit_scores(row, sub_tab, date_):
    ds = _ds_label(row)
    code = row.get("ds_code", "")
    s1 = row.get("score1", 0)
    s2 = row.get("score2")
    s3 = row.get("score3")
    s4 = row.get("score4")
    trend = row.get("trend", "")

    subject = f"[ds ops-audit][uae][{_t(row)}][{code}]"
    history_bits = []
    for i, s in enumerate([s1, s2, s3, s4], start=1):
        if s is not None:
            history_bits.append(f'<li>w-{i-1}: {s}</li>')

    body = _wrap(f"""
<p>hi team</p>
<p>flagging audit score breach at {ds} as of {date_}</p>
<ol>
<li>latest weekly audit {s1}, trend {trend or 'stable'}</li>
<li>history</li>
<ol>{"".join(history_bits)}</ol>
</ol>
<p>please review audit checklist and confirm corrective action timeline</p>
<p>vardan</p>""")
    return subject, body


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRY
# ──────────────────────────────────────────────────────────────────────────────
TEMPLATES = {
    "agent_01_attendance":         build_attendance,
    "agent_02_iph_pickers":        build_iph_pickers,
    "agent_03_iph_putaway":        build_iph_putaway,
    "agent_04_skips_picker":       build_skips_picker,
    "agent_05_defects":            build_defects,
    "agent_06_fefo":               build_fefo,
    "agent_07_adjustments":        build_adjustments,
    "agent_08_putaway_delays":     build_putaway_delays,
    "agent_09_missing_inventory":  build_missing_inventory,
    "agent_10_skips_stocktake":    build_skips_stocktake,
    "agent_11_audit_scores":       build_audit_scores,
}


def build_draft(agent, row, sub_tab=None, date_=None):
    fn = TEMPLATES.get(agent)
    if not fn:
        raise ValueError(f"no template for agent {agent}")
    return fn(row, sub_tab, date_)
