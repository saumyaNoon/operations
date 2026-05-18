"""
nim-agents-ops api/app.py

flask app, port 5001 (sc lives on 5055/5000). serves the react dashboard at
localhost:3001.

routes:
  /api/health
  /api/alerts                    list recent alerts (filter by agent + tier)
  /api/alerts/<id>/dismiss       mark dismissed
  /api/alerts/<id>/send          (placeholder — moves draft → sent)
  /api/agents                    list 11 agents + last run meta
  /api/agents/<id>/run           trigger an agent run
  /api/routing/ds                list 156 ds rows
  /api/routing/vendors           list vendors
  /api/todos                     GET / POST
  /api/todos/<id>/complete       POST
  /api/notes                     GET / POST
  /api/drafts                    list matrix drafts (gmail)
  /api/thresholds/<agent>        latest p20/p50/p80 per opd-bucket
  /actions                       SEPARATE TAB — action plan page (built from action_layer)
  /api/actions/take              POST 3-mode action (email/whatsapp/copy)
"""
import os, sys, json, importlib, subprocess
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from api.lib.db import (
    init_db, conn, recent_alerts, update_alert_status, log_action,
    add_todo, open_todos, complete_todo, add_note, recent_notes,
    latest_run, latest_thresholds
)
from api.lib.gmail_client import list_matrix_drafts
from api.lib.platform_health import all_metrics as platform_health_metrics

# oracle_gm blueprint — sibling cockpit on the same flask process
from api.blueprints.oracle_gm import bp as oracle_gm_bp, init as oracle_gm_init
# triage blueprint — in-page draft compose+approve+save flow for /email-triage
from api.blueprints.triage import bp as triage_bp

app = Flask(__name__)
CORS(app, origins=["http://localhost:*", "http://127.0.0.1:*",
                   "file://*", "null"])
app.register_blueprint(oracle_gm_bp)
app.register_blueprint(triage_bp)
oracle_gm_init()  # ensures metrics_snapshot + category_atc_drops tables exist

@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# 11-agent registry: id → (display_name, cadence, module)
AGENTS = [
    ("agent_01_attendance",        "attendance and absenteeism", "daily"),
    ("agent_02_iph_pickers",       "iph pickers (outbound)",     "hourly"),
    ("agent_03_iph_putaway",       "iph putaway (inbound)",      "hourly"),
    ("agent_04_skips_picker",      "skips (picker)",             "hourly"),
    ("agent_05_defects",           "defects (customer complaints)", "daily"),
    ("agent_06_fefo",              "fefo adherence",             "daily"),
    ("agent_07_adjustments",       "adjustments",                "hourly"),
    ("agent_08_putaway_delays",    "putaway delays",             "hourly"),
    ("agent_09_missing_inventory", "missing inventory",          "hourly"),
    ("agent_10_skips_stocktake",   "skips (incident stocktake)", "hourly"),
    ("agent_11_audit_scores",      "audit scores",               "daily"),
    ("agent_12_bt_pending_pick",   "bt pending pick",            "hourly"),
]


# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "nim-agents-ops",
        "version": "0.1.0",
        "now": datetime.now().isoformat(),
        "domain": "ops · ds",
        "geo": "uae",
    })


# ── alerts ────────────────────────────────────────────────────────────────────
@app.get("/api/alerts")
def api_alerts():
    agent = request.args.get("agent")
    tier = request.args.get("tier", type=int)
    hours = request.args.get("hours", default=48, type=int)
    limit = request.args.get("limit", default=5000, type=int)
    rows = recent_alerts(agent=agent, hours=hours, limit=limit)
    if tier:
        rows = [r for r in rows if r.get("tier") == tier]
    for r in rows:
        if r.get("payload_json"):
            try:
                r["payload"] = json.loads(r["payload_json"])
            except Exception:
                r["payload"] = None
        r.pop("payload_json", None)
    return jsonify(rows)


@app.post("/api/alerts/<int:alert_id>/dismiss")
def api_dismiss(alert_id):
    update_alert_status(alert_id, "dismissed")
    log_action("dismissed", "ui", str(alert_id))
    return jsonify({"ok": True})


def _build_alert_email(alert_id):
    """resolve to/cc/subject/body from an alert id. shared by preview + create."""
    from api.lib.draft_builder import build_draft
    from api.lib.routing import resolve_routing
    with conn() as c:
        r = c.execute("SELECT * FROM alert_log WHERE id=?", (alert_id,)).fetchone()
        if not r: return None, "alert not found", 404
        alert = dict(r)
    payload = json.loads(alert.get("payload_json") or "{}")
    payload["tier"] = alert.get("tier")
    if alert.get("ds_code"): payload["ds_code"] = alert["ds_code"]
    if alert.get("vendor_shortcode"): payload["vendor_shortcode"] = alert["vendor_shortcode"]
    try:
        subject, body = build_draft(alert["agent"], payload,
                                     sub_tab=alert.get("sub_tab"),
                                     date_=alert["drafted_at"][:10])
        to_, cc_ = resolve_routing(alert["agent"], payload,
                                    sub_tab=alert.get("sub_tab"), geo="ae")
    except Exception as e:
        return None, str(e), 500
    return {"alert": alert, "to": to_, "cc": cc_, "subject": subject, "body": body}, None, 200


@app.get("/api/alerts/<int:alert_id>/preview")
def api_preview_draft(alert_id):
    """preview to/cc/subject/body for an alert without creating a gmail draft."""
    out, err, code = _build_alert_email(alert_id)
    if err: return jsonify({"ok": False, "error": err}), code
    return jsonify({
        "ok": True,
        "to": out["to"], "cc": out["cc"],
        "subject": out["subject"], "body_html": out["body"],
        "draft_id": out["alert"].get("draft_id"),
        "agent": out["alert"]["agent"],
    })


@app.post("/api/alerts/<int:alert_id>/draft")
def api_create_draft(alert_id):
    """build + post a gmail draft for an existing alert. accepts optional
    body overrides {to, cc, subject, body_html} so the dashboard can let
    the user edit before creation."""
    from api.lib.gmail_client import create_draft_in_matrix
    out, err, code = _build_alert_email(alert_id)
    if err: return jsonify({"ok": False, "error": err}), code
    alert = out["alert"]
    if alert.get("draft_id"):
        return jsonify({"ok": False, "error": "draft already exists",
                        "draft_id": alert["draft_id"]}), 409

    body_override = request.get_json(silent=True) or {}
    to_      = body_override.get("to")      or out["to"]
    cc_      = body_override.get("cc")      or out["cc"]
    subject  = body_override.get("subject") or out["subject"]
    body_html = body_override.get("body_html") or out["body"]

    try:
        draft_id = create_draft_in_matrix(to_, cc_, subject, body_html)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    with conn() as c:
        c.execute("UPDATE alert_log SET draft_id=?, status='draft' WHERE id=?",
                  (draft_id, alert_id))
    log_action("drafted", alert["agent"], alert["row_key"], draft_id=draft_id)
    return jsonify({"ok": True, "draft_id": draft_id, "to": to_, "cc": cc_, "subject": subject})


@app.post("/api/alerts/<int:alert_id>/send")
def api_send(alert_id):
    """placeholder — flips status to 'sent'. real flow: vardan opens the
    gmail draft, edits if needed, hits send. dashboard polls gmail for status
    or relies on this manual flip."""
    update_alert_status(alert_id, "sent")
    log_action("sent", "ui", str(alert_id))
    return jsonify({"ok": True})


# ── agents ────────────────────────────────────────────────────────────────────
@app.get("/api/agents")
def api_agents():
    # primary sub_tab per agent — tile count uses ONLY this sub-tab to avoid
    # double-counting (e.g. agent_01 'overall_today' is the union of cc + temp +
    # vendor; counting all 4 sub-tabs would 4× the same store).
    PRIMARY_SUB = {
        "agent_01_attendance":   "overall_today",
        "agent_02_iph_pickers":  "overall_d0",
        "agent_03_iph_putaway":  "overall_d0",
        "agent_04_skips_picker": "store",
        # for agent_07 the tile sums BOTH partner_9411 + non_9411 (different
        # universes — noon-own + 3rd party), so no primary filter needed.
        # tile reflects the union of unique stores across the two sub-tabs.
    }
    out = []
    # agents whose alert_log rows always have sub_tab=NULL (single-grain).
    # used to apply NULL filter only for these to avoid summing across sub-tabs
    # for agents like agent_07 where sub_tabs are mutually exclusive universes.
    NULL_SUB_AGENTS = {"agent_05_defects", "agent_06_fefo", "agent_08_putaway_delays",
                       "agent_09_missing_inventory", "agent_10_skips_stocktake",
                       "agent_11_audit_scores", "agent_12_bt_pending_pick"}
    for aid, name, cadence in AGENTS:
        meta = latest_run(aid) or {}
        primary = PRIMARY_SUB.get(aid)
        if primary:
            sub_filter = " AND sub_tab = ?"
            args = [aid, primary]
        elif aid in NULL_SUB_AGENTS:
            sub_filter = " AND (sub_tab IS NULL OR sub_tab = '')"
            args = [aid]
        else:
            sub_filter = ""  # count across all sub_tabs (mutually-exclusive universes)
            args = [aid]
        with conn() as c:
            counts = c.execute(
                f"""SELECT
                   SUM(CASE WHEN tier=1 THEN 1 ELSE 0 END) AS t1,
                   SUM(CASE WHEN tier=2 THEN 1 ELSE 0 END) AS t2,
                   SUM(CASE WHEN tier=3 THEN 1 ELSE 0 END) AS t3,
                   COUNT(*) AS total
                   FROM alert_log
                   WHERE agent=? {sub_filter}
                     AND drafted_at > datetime('now','-48 hours')""",
                args
            ).fetchone()
        out.append({
            "id": aid,
            "name": name,
            "cadence": cadence,
            "last_run": meta,
            "counts_48h": dict(counts) if counts else None,
        })
    return jsonify(out)


@app.post("/api/agents/<agent_id>/run")
def api_run_agent(agent_id):
    """invoke `python -m agents.<agent_id>` as a subprocess, capture stdout."""
    valid_ids = {a[0] for a in AGENTS}
    if agent_id not in valid_ids:
        return jsonify({"ok": False, "error": f"unknown agent {agent_id}"}), 404
    try:
        out = subprocess.check_output(
            [sys.executable, "-m", f"agents.{agent_id}"],
            cwd=ROOT, stderr=subprocess.STDOUT, timeout=600
        )
        return jsonify({"ok": True, "stdout": out.decode("utf-8", errors="replace")})
    except subprocess.CalledProcessError as e:
        return jsonify({
            "ok": False,
            "error": f"agent {agent_id} failed",
            "stdout": e.output.decode("utf-8", errors="replace"),
        }), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "agent run timed out"}), 504


@app.post("/api/agents/run-all")
def api_run_all_agents():
    """run every agent × geo in parallel. wall-clock dominated by slowest pair (~60-180s).
    geos: `?geos=ae,sa` (default ae+sa). agents that are ae-only by design
    (attendance, iph_pickers, fefo) early-return for non-ae and finish in ms.
    geo is passed to each subprocess via AGENT_GEO env var (read by Agent.__init__).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    geos_param = request.args.get("geos", "ae,sa,eg")
    geos = [g.strip().lower() for g in geos_param.split(",") if g.strip()]
    valid = {"ae", "sa", "eg"}
    geos = [g for g in geos if g in valid] or ["ae"]

    def _run(agent_id, geo):
        env = os.environ.copy()
        env["AGENT_GEO"] = geo
        try:
            out = subprocess.check_output(
                [sys.executable, "-m", f"agents.{agent_id}"],
                cwd=ROOT, stderr=subprocess.STDOUT, timeout=300, env=env,
            )
            tail = out.decode("utf-8", errors="replace").splitlines()[-1:] or [""]
            return {"agent_id": agent_id, "geo": geo, "ok": True, "tail": tail[0][:200]}
        except subprocess.CalledProcessError as e:
            return {"agent_id": agent_id, "geo": geo, "ok": False,
                    "error": e.output.decode("utf-8", errors="replace")[-300:]}
        except subprocess.TimeoutExpired:
            return {"agent_id": agent_id, "geo": geo, "ok": False, "error": "timed out (300s)"}
        except Exception as e:
            return {"agent_id": agent_id, "geo": geo, "ok": False,
                    "error": f"{type(e).__name__}: {e}"}

    started = datetime.now()
    results = []
    pairs = [(a[0], g) for a in AGENTS for g in geos]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_run, aid, g) for (aid, g) in pairs]
        for fut in as_completed(futs):
            results.append(fut.result())
    elapsed = (datetime.now() - started).total_seconds()
    okc = sum(1 for r in results if r["ok"])
    return jsonify({
        "ok": okc == len(results),
        "geos": geos,
        "total": len(results),
        "ok_count": okc,
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    })


# ── routing ───────────────────────────────────────────────────────────────────
@app.get("/api/routing/ds")
def api_ds_routing():
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM ds_routing ORDER BY geo, city, ds_code"
        ).fetchall()]
    return jsonify(rows)


@app.get("/api/routing/vendors")
def api_vendor_routing():
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM vendor_routing ORDER BY shortcode"
        ).fetchall()]
    return jsonify(rows)


# ── todos ─────────────────────────────────────────────────────────────────────
@app.get("/api/todos")
def api_todos_list():
    return jsonify(open_todos())


@app.post("/api/todos")
def api_todos_create():
    body = request.json or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    new_id = add_todo(text, source_agent=body.get("source_agent"),
                      source_row_key=body.get("source_row_key"))
    return jsonify({"ok": True, "id": new_id})


@app.post("/api/todos/<int:todo_id>/complete")
def api_todos_complete(todo_id):
    complete_todo(todo_id)
    return jsonify({"ok": True})


# ── notes ─────────────────────────────────────────────────────────────────────
@app.get("/api/notes")
def api_notes_list():
    return jsonify(recent_notes())


@app.post("/api/notes")
def api_notes_create():
    body = request.json or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    new_id = add_note(text)
    return jsonify({"ok": True, "id": new_id})


# ── drafts (gmail) ────────────────────────────────────────────────────────────
@app.get("/api/drafts")
def api_drafts():
    try:
        return jsonify(list_matrix_drafts(query_prefix="[ds ops-", limit=200))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── thresholds ────────────────────────────────────────────────────────────────
@app.get("/api/thresholds/<agent_id>")
def api_thresholds(agent_id):
    return jsonify(latest_thresholds(agent_id))


# ── platform health (10 KPIs for the bottom strip) ───────────────────────────
@app.get("/api/platform_health")
def api_platform_health():
    country = request.args.get("country", "ae").lower()
    # support ?nocache=1 to invalidate BOTH the in-process cache AND the persistent
    # state.db query_cache for this country. dashboard refresh button uses this so
    # clicks actually re-hit BQ instead of returning 15-min-old cached numbers.
    if request.args.get("nocache"):
        from api.lib import platform_health as _ph
        for k in list(_ph._CACHE.keys()):
            if k.endswith(f"_{country}"):
                _ph._invalidate(key=k)
    return jsonify(platform_health_metrics(country))


# ── action plan (separate tab; main dashboard untouched) ─────────────────────
@app.get("/actions")
def actions_page():
    """serve the action plan html for morpheus. regenerates fresh on each hit
    (cheap; reads alert_log + owners.yaml)."""
    from flask import send_file as _send_file, abort as _abort
    country = request.args.get("country")
    if country:
        country = country.lower()
        if country not in ("uae", "ksa"):
            return _abort(400, "country must be uae or ksa or omitted")
    min_tier = int(request.args.get("min_tier") or 2)
    hours = int(request.args.get("hours") or 168)

    try:
        sys.path.insert(0, ROOT)
        from morpheus_action_plan import build_action_plan, render_html
        plan = build_action_plan(country=country, min_tier=min_tier, hours=hours)
        html_str = render_html(plan)
        out_path = os.path.join(ROOT, "dashboard", "morpheus-action-plan.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_str)
        return _send_file(out_path)
    except Exception as e:
        return f"<pre>action plan generation failed:\n{e}</pre>", 500


@app.post("/api/actions/take")
def api_actions_take():
    """3-mode action dispatcher. body: {action, recipient, country?}"""
    try:
        sys.path.insert(0, ROOT)
        from action_layer import actions as al_actions
        from morpheus_action_plan import build_action_plan
    except Exception as e:
        return jsonify({"error": f"action_layer import failed: {e}"}), 500

    payload = request.get_json(silent=True) or {}
    action_type = payload.get("action")
    recipient = payload.get("recipient")
    country = (payload.get("country") or "").lower() or None
    if action_type not in ("email", "whatsapp", "copy"):
        return jsonify({"error": f"invalid action: {action_type}"}), 400
    if not recipient:
        return jsonify({"error": "recipient required"}), 400
    if country == "all":
        country = None

    plan = build_action_plan(country=country, min_tier=2)
    by = plan["by_recipient"].get(recipient)
    if not by:
        return jsonify({"error": f"recipient {recipient} not found in current plan"}), 404
    name = (by.get("recipient") or {}).get("name")
    try:
        result = al_actions.take_action(action_type, recipient, by["alerts"], recipient_name=name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── morpheus static snapshot rebuild ─────────────────────────────────────────
# called from the static dashboard's refresh button: re-runs the generator
# (outputs/Morpheus/generate_static.py) which fetches a fresh per-country
# snapshot from flask and rewrites the canonical commandcenter html.
# the dashboard reload()s after this returns 200 so the user sees fresh data.
@app.post("/api/morpheus/regenerate-static")
def api_morpheus_regenerate_static():
    gen = r"C:\Users\vnagar\Documents\Claude\outputs\Morpheus\generate_static.py"
    if not os.path.exists(gen):
        return jsonify({"ok": False, "error": f"generator not found: {gen}"}), 500
    started = datetime.now()
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        out = subprocess.check_output(
            [sys.executable, gen], stderr=subprocess.STDOUT, timeout=180, env=env,
        )
        tail = out.decode("utf-8", errors="replace").splitlines()[-3:]
        return jsonify({
            "ok": True,
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
            "tail": tail,
        })
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False,
                        "error": e.output.decode("utf-8", errors="replace")[-500:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "regenerate timed out (180s)"}), 504


# ── oracle static snapshot rebuild ───────────────────────────────────────────
# parallel to morpheus regenerate-static. called from the static oracle file's
# refresh button so it can actually refresh in-place: re-runs the bq snapshot
# for the active country (or all 3), then re-runs outputs/Oracle/generate_static.py
# to rewrite the canonical oracle_gm_cockpit.html with fresh data. dashboard
# reload()s after this returns 200.
@app.post("/api/oracle/regenerate-static")
def api_oracle_regenerate_static():
    country = (request.args.get("country") or "").lower()
    gen = r"C:\Users\vnagar\Documents\Claude\outputs\Oracle\generate_static.py"
    if not os.path.exists(gen):
        return jsonify({"ok": False, "error": f"generator not found: {gen}"}), 500
    started = datetime.now()
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("BQ_BILLING_PROJECT", "noonbinimops")
    # step 1: BQ snapshot refresh for the active country (or all 3 if unspecified)
    snapshot_cmd = [sys.executable, "-m", "oracle_gm.snapshot"]
    if country in ("ae", "sa", "eg"):
        snapshot_cmd += ["--country", country]
    try:
        subprocess.check_output(
            snapshot_cmd, cwd=ROOT, env=env, stderr=subprocess.STDOUT, timeout=600,
        )
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "step": "snapshot",
                        "error": e.output.decode("utf-8", errors="replace")[-500:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "step": "snapshot", "error": "snapshot timed out (600s)"}), 504
    # step 2: regenerate the static file with the now-fresh BQ data
    try:
        out = subprocess.check_output(
            [sys.executable, gen], stderr=subprocess.STDOUT, timeout=180, env=env,
        )
        tail = out.decode("utf-8", errors="replace").splitlines()[-3:]
        return jsonify({
            "ok": True,
            "country": country or "all",
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
            "tail": tail,
        })
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "step": "regenerate",
                        "error": e.output.decode("utf-8", errors="replace")[-500:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "step": "regenerate", "error": "regenerate timed out (180s)"}), 504


# ── agent_05 raw complaint export (CSV) ──────────────────────────────────────
@app.get("/api/agents/agent_05_defects/export")
def api_defects_export():
    """return raw complaint rows for a ds_code as CSV. used by the dashboard CSV button."""
    import csv, io
    from datetime import date
    from api.lib.bigquery_client import run as bq_run
    ds_code = request.args.get("ds_code", "").strip()
    target  = request.args.get("date", date.today().isoformat())
    if not ds_code:
        return jsonify({"ok": False, "error": "ds_code required"}), 400
    sql = f"""
    SELECT
      order_nr, complain_date, complain_category, complain_reason,
      minutes_category_new AS category,
      partner_wh_code AS ds_code
    FROM `noonbinimksa.darkstore.complains_raw_all`
    WHERE complain_date = DATE('{target}')
      AND country_code   = 'ae'
      AND partner_wh_code = '{ds_code}'
    ORDER BY complain_category, complain_reason
    """
    try:
        rows = bq_run(sql)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not rows:
        return jsonify({"ok": False, "error": "no rows found"}), 404
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=defects_{ds_code}_{target}.csv"}
    )


# ── dashboard static serve ────────────────────────────────────────────────────
@app.get("/")
def serve_dashboard():
    from flask import send_file as _sf
    html = os.path.normpath(os.path.join(ROOT, "dashboard", "morpheus-dsops_commandcenter.html"))
    return _sf(html)


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("nim-agents-ops backend on http://localhost:5001")
    print("  main dashboard:   http://localhost:5001/  (unchanged)")
    print("  action plan tab:  http://localhost:5001/actions")
    app.run(host="127.0.0.1", port=5001, debug=False)
