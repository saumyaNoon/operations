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

app = Flask(__name__)
CORS(app, origins=["http://localhost:*", "http://127.0.0.1:*",
                   "file://*", "null"])

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
    }
    out = []
    for aid, name, cadence in AGENTS:
        meta = latest_run(aid) or {}
        primary = PRIMARY_SUB.get(aid)
        sub_filter = " AND sub_tab = ?" if primary else " AND (sub_tab IS NULL OR sub_tab = '')"
        args = [aid] + ([primary] if primary else [])
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
    return jsonify(platform_health_metrics(country))


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("nim-agents-ops backend on http://localhost:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
