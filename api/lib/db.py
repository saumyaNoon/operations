"""
nim-agents-ops api/lib/db.py

sqlite schema for state.db. mirrors nim-agents-sc but with the ops-ds
specific tables: ds_routing, vendor_routing, agent_thresholds, todos, notes
"""
import sqlite3, json, os
from datetime import datetime, timedelta
from contextlib import contextmanager

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(ROOT, "state.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30.0)  # 30s busy-timeout for concurrent writes
    c.row_factory = sqlite3.Row
    # WAL mode: readers don't block writers and writers don't block readers.
    # Critical when oracle snapshot, morpheus run-all, and platform_health
    # all hit state.db at once (per /pulse plan 2026-05-11).
    # PRAGMA is per-connection but journal_mode persists at the file level —
    # first connection that sets it sticks. Idempotent.
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, ~10x faster than FULL
    except sqlite3.OperationalError:
        # if another process is currently holding an exclusive lock the PRAGMA
        # may fail transiently; that's fine, the mode is already set on the file.
        pass
    try:
        yield c
    finally:
        c.commit()
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            sub_tab TEXT,
            ds_code TEXT,
            vendor_shortcode TEXT,
            row_key TEXT NOT NULL,
            tier INTEGER NOT NULL,
            metric_name TEXT,
            metric_value REAL,
            contribution_pct REAL,
            payload_json TEXT,
            drafted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            draft_id TEXT,
            status TEXT DEFAULT 'draft'
        );
        CREATE INDEX IF NOT EXISTS idx_alert_dedup
          ON alert_log(agent, ds_code, vendor_shortcode, row_key, drafted_at);
        CREATE INDEX IF NOT EXISTS idx_alert_recent
          ON alert_log(agent, drafted_at);

        CREATE TABLE IF NOT EXISTS ds_routing (
            ds_code TEXT PRIMARY KEY,
            ds_name TEXT,
            geo TEXT,
            city TEXT,
            ds_status TEXT,
            am_name TEXT,
            am_email TEXT,
            asst_mgr TEXT,
            supervisor TEXT,
            tl_name TEXT
        );

        CREATE TABLE IF NOT EXISTS vendor_routing (
            id_vendor INTEGER PRIMARY KEY,
            shortcode TEXT,
            vendor_name TEXT,
            vendor_email TEXT,
            vendor_type TEXT,
            in_scope INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS agent_run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            rows_scanned INTEGER,
            t1_count INTEGER DEFAULT 0,
            t2_count INTEGER DEFAULT 0,
            t3_count INTEGER DEFAULT 0,
            drafts_created INTEGER DEFAULT 0,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_run_recent
          ON agent_run_history(agent, started_at);

        CREATE TABLE IF NOT EXISTS todo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source_agent TEXT,
            source_row_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            rolled_over_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS agent_thresholds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            bucket TEXT,
            p20 REAL,
            p50 REAL,
            p80 REAL,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_thr_agent
          ON agent_thresholds(agent, computed_at);

        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            agent TEXT NOT NULL,
            row_key TEXT NOT NULL,
            gmail_draft_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_actions_dedup
          ON actions(agent, row_key, action_type, created_at);

        /* persistent BQ query cache — survives flask restart so first hit
           after restart doesn't pay BQ round-trip latency. Per /pulse plan
           2026-05-11. Default TTL 900s (15 min) — bumped from 300s. */
        CREATE TABLE IF NOT EXISTS query_cache (
            cache_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL DEFAULT 900
        );
        CREATE INDEX IF NOT EXISTS idx_cache_age
          ON query_cache(fetched_at);
        """)


# ──────────────────────────────────────────────────────────────────────────────
# query_cache helpers (persistent layer for platform_health._cached etc.)
# ──────────────────────────────────────────────────────────────────────────────
def cache_get(key, ttl_seconds=900):
    """return cached value for key if fresher than ttl_seconds, else None.
    silently returns None on any error (cache is best-effort, never fatal)."""
    import time
    try:
        with conn() as c:
            r = c.execute(
                "SELECT value_json, fetched_at FROM query_cache WHERE cache_key=?",
                (key,)
            ).fetchone()
            if not r:
                return None
            if time.time() - r["fetched_at"] > ttl_seconds:
                return None
            return json.loads(r["value_json"])
    except Exception:
        return None


def cache_set(key, value, ttl_seconds=900):
    """upsert a cached value. silently swallows errors."""
    import time
    try:
        with conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO query_cache (cache_key, value_json, fetched_at, ttl_seconds) VALUES (?,?,?,?)",
                (key, json.dumps(value, default=str), time.time(), ttl_seconds)
            )
    except Exception:
        pass


def cache_invalidate(prefix=None, key=None):
    """invalidate by exact key or by prefix (e.g. all entries starting with 'pct_defects_')."""
    try:
        with conn() as c:
            if key:
                c.execute("DELETE FROM query_cache WHERE cache_key=?", (key,))
            elif prefix:
                c.execute("DELETE FROM query_cache WHERE cache_key LIKE ?", (prefix + "%",))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# alert_log helpers
# ──────────────────────────────────────────────────────────────────────────────
def log_alert(agent, row_key, tier, *, sub_tab=None, ds_code=None,
              vendor_shortcode=None, metric_name=None, metric_value=None,
              contribution_pct=None, payload=None, draft_id=None,
              status="draft"):
    with conn() as c:
        c.execute(
            """INSERT INTO alert_log
            (agent, sub_tab, ds_code, vendor_shortcode, row_key, tier,
             metric_name, metric_value, contribution_pct, payload_json,
             draft_id, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (agent, sub_tab, ds_code, vendor_shortcode, row_key, tier,
             metric_name, metric_value, contribution_pct,
             json.dumps(payload, default=str) if payload else None,
             draft_id, status)
        )


def upsert_alert(agent, row_key, tier, *, sub_tab=None, ds_code=None,
                 vendor_shortcode=None, metric_name=None, metric_value=None,
                 contribution_pct=None, payload=None, status="breach"):
    """update payload + metric on existing same-day alert; insert if none exists.
    draft_id and status are preserved on existing rows so drafted alerts aren't reset."""
    payload_json = json.dumps(payload, default=str) if payload else None
    today = datetime.now().date().isoformat()
    with conn() as c:
        existing = c.execute(
            "SELECT id, draft_id, status FROM alert_log "
            "WHERE agent=? AND row_key=? AND DATE(drafted_at)=? LIMIT 1",
            (agent, row_key, today)
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE alert_log SET
                   tier=?, metric_value=?, contribution_pct=?, payload_json=?,
                   drafted_at=datetime('now')
                   WHERE id=?""",
                (tier, metric_value, contribution_pct, payload_json, existing["id"])
            )
        else:
            c.execute(
                """INSERT INTO alert_log
                (agent, sub_tab, ds_code, vendor_shortcode, row_key, tier,
                 metric_name, metric_value, contribution_pct, payload_json,
                 draft_id, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent, sub_tab, ds_code, vendor_shortcode, row_key, tier,
                 metric_name, metric_value, contribution_pct, payload_json,
                 None, status)
            )


def was_alerted_recently(agent, row_key, hours=48):
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with conn() as c:
        r = c.execute(
            "SELECT 1 FROM alert_log WHERE agent=? AND row_key=? AND drafted_at>? LIMIT 1",
            (agent, row_key, cutoff)
        ).fetchone()
        return r is not None


def recent_alerts(agent=None, hours=48, limit=200):
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    sql = "SELECT * FROM alert_log WHERE drafted_at>?"
    args = [cutoff]
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    sql += " ORDER BY drafted_at DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def update_alert_status(alert_id, status):
    with conn() as c:
        c.execute("UPDATE alert_log SET status=? WHERE id=?", (status, alert_id))


# ──────────────────────────────────────────────────────────────────────────────
# action log (drafted / dismissed / sent)
# ──────────────────────────────────────────────────────────────────────────────
def log_action(action_type, agent, row_key, draft_id=None):
    with conn() as c:
        c.execute(
            "INSERT INTO actions (action_type, agent, row_key, gmail_draft_id) VALUES (?,?,?,?)",
            (action_type, agent, row_key, draft_id)
        )


def get_action_history(agent, row_key, limit=20):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT action_type, gmail_draft_id, created_at FROM actions "
            "WHERE agent=? AND row_key=? ORDER BY created_at DESC LIMIT ?",
            (agent, row_key, limit)
        ).fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# routing lookups
# ──────────────────────────────────────────────────────────────────────────────
def get_ds_routing(ds_code):
    with conn() as c:
        r = c.execute("SELECT * FROM ds_routing WHERE ds_code=?", (ds_code,)).fetchone()
        return dict(r) if r else None


def all_ds_codes(geo="uae"):
    with conn() as c:
        return [r["ds_code"] for r in c.execute(
            "SELECT ds_code FROM ds_routing WHERE geo=? AND ds_status='Live'", (geo,)
        ).fetchall()]


def get_vendor_routing(id_vendor=None, shortcode=None):
    with conn() as c:
        if id_vendor is not None:
            r = c.execute("SELECT * FROM vendor_routing WHERE id_vendor=?", (id_vendor,)).fetchone()
        elif shortcode:
            r = c.execute("SELECT * FROM vendor_routing WHERE shortcode=?", (shortcode,)).fetchone()
        else:
            return None
        return dict(r) if r else None


# ──────────────────────────────────────────────────────────────────────────────
# agent run history
# ──────────────────────────────────────────────────────────────────────────────
def start_run(agent):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO agent_run_history (agent, started_at) VALUES (?,?)",
            (agent, datetime.now().isoformat())
        )
        return cur.lastrowid


def finish_run(run_id, *, rows_scanned=0, t1=0, t2=0, t3=0, drafts=0, error=None):
    with conn() as c:
        c.execute(
            """UPDATE agent_run_history SET
               completed_at=?, rows_scanned=?, t1_count=?, t2_count=?, t3_count=?,
               drafts_created=?, error_message=? WHERE id=?""",
            (datetime.now().isoformat(), rows_scanned, t1, t2, t3, drafts, error, run_id)
        )


def latest_run(agent):
    with conn() as c:
        r = c.execute(
            "SELECT * FROM agent_run_history WHERE agent=? ORDER BY started_at DESC LIMIT 1",
            (agent,)
        ).fetchone()
        return dict(r) if r else None


# ──────────────────────────────────────────────────────────────────────────────
# thresholds (for rolling-threshold agents)
# ──────────────────────────────────────────────────────────────────────────────
def save_thresholds(agent, bucket, p20, p50, p80):
    with conn() as c:
        c.execute(
            "INSERT INTO agent_thresholds (agent, bucket, p20, p50, p80) VALUES (?,?,?,?,?)",
            (agent, bucket, p20, p50, p80)
        )


def latest_thresholds(agent):
    """return {bucket: {p20, p50, p80, computed_at}} for the most recent rebase per bucket."""
    with conn() as c:
        rows = c.execute(
            """SELECT bucket, p20, p50, p80, MAX(computed_at) AS computed_at
               FROM agent_thresholds WHERE agent=? GROUP BY bucket""",
            (agent,)
        ).fetchall()
        return {r["bucket"]: dict(r) for r in rows}


# ──────────────────────────────────────────────────────────────────────────────
# todos + notes
# ──────────────────────────────────────────────────────────────────────────────
def add_todo(text, source_agent=None, source_row_key=None):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO todo (text, source_agent, source_row_key) VALUES (?,?,?)",
            (text, source_agent, source_row_key)
        )
        return cur.lastrowid


def open_todos():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM todo WHERE completed_at IS NULL ORDER BY created_at DESC"
        ).fetchall()]


def complete_todo(todo_id):
    with conn() as c:
        c.execute("UPDATE todo SET completed_at=? WHERE id=?",
                  (datetime.now().isoformat(), todo_id))


def add_note(text):
    with conn() as c:
        cur = c.execute("INSERT INTO notes (text) VALUES (?)", (text,))
        return cur.lastrowid


def recent_notes(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()]
