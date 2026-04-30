# nim-agents · ops · ds command center

## context

this is the operational sister build to the existing `nim-agents · sc` (supply chain command center) which already runs at `C:\Users\vnagar\Documents\nim-agents\` with 11 supply chain agents writing gmail drafts to the "matrix" label

this build adds 11 darkstore-operations agents covering attendance, iph, inventory health, ops hygiene, and audit. mirrors the sc architecture exactly — same flask + react + sqlite + gmail-api pattern

target install path: `C:\Users\vnagar\Documents\nim-agents-ops\`
domain: ops · ds (darkstore operations)
phase 1 scope: UAE only · 156 darkstores · 6 area managers
phase 2: KSA + EGY (deferred)

reference the locked design doc at `pinned/nim-agents-ops-ds-matrix-v09-2026-04-27.xlsx` for full agent specs, thresholds, escalation routing, and ds → AM mapping

---

## architecture (mirrors nim-agents · sc)

local stack
- flask api on `localhost:5001` (sc is on 5000 — keep separate)
- sqlite at `state.db` for dedup state, alert log, run history
- gmail api with one-time OAuth, drafts written to "matrix" label
- windows task scheduler for cadence
- react front-end on `localhost:3001` for the dashboard

dashboard (the new piece — see DASHBOARD section below)

---

## the 11 agents

reference SQL is split between 2 google docs that should be added to `pinned/`:
- `Code Repo - DS - Cenral Ops` (saumsingh@noon.com) — DS-side queries: live qty, non-live qty, IPP+biometric login, stock_take, full_location_adherence, defects, expiry, putaway pendency at DS. THIS IS THE PRIMARY REFERENCE for darkstore agents
- `[Code repo] 2026 Ops` (suarya@noon.com / Sauya Singh) — primarily WH-side queries (warehouse skipped line, WH putaway pendency D-2++, barcode history). useful for context but NOT primary for DS agents. Sauya/Shivanshu work on WH ops, not DS

per-agent SQL reference mapping

| # | agent | bucket | cadence | source table | sql ref doc |
|---|-------|--------|---------|--------------|-------------|
| 1 | attendance and absenteeism | manpower | 1× daily 09:00 | Biometric_base_v2_3 + ipp_daily_ae (joined) | DS-Cenral Ops doc → "DS-AE login IPP&Biometric in/out" |
| 2 | iph pickers (outbound) | manpower | hourly 06–24 | ipp_daily_ae (use outbound_qty * 3600 / outbound_time_; do NOT use mp_order+picking_job which is WH-side) | DS-Cenral Ops doc → outbound_iph block in same query |
| 3 | iph putaway (inbound) | manpower | hourly 06–24 | ipp_daily_ae (use inbound_qty * 3600 / inbound_time; same single table for inbound + outbound) | DS-Cenral Ops doc → inbound_iph block in same query |
| 4 | skips (picker) | inv health | hourly 06–24 | scgoms_cwpicking | 2026 Ops doc → cwpicking skipped_items section (scope filter to DS) |
| 5 | defects (customer complaints) | inv health | 1× daily 10:00 | UAE: complains_raw_ae · KSA: complains_raw_sa | DS-Cenral Ops doc → "DS Defects Base" (NOTE: saumy's doc has UAE/KSA labels swapped — use the correct ae/sa mapping above) |
| 6 | fefo adherence | inv health | 1× daily 10:00 | central_expiry_raw (system) + central_expiry_raw_adjusted (KL-adjusted) — country-suffixed for KSA (_sa) | DS-Cenral Ops doc → "DS Expiry (raw and Adjusted)". CORRECTED — was previously listed as fifo_report_logs which is wrong |
| 7 | adjustments | ops hygiene | hourly 06–24 | mxdcss_dcss adjustments | 2026 Ops doc → "Adj DS" |
| 8 | putaway delays | ops hygiene | hourly 06–18 | mxfulfillment_norms.box + box_item + putaway_job_line — bucketed 0-3hr / 3-6hr / >6hr | DS-Cenral Ops doc → "Store Putaway Pendency Buckets" + "Putaway Pendency at DS Ageing Raw Level". CORRECTED — was previously SLA per-storage-condition (ambient >6h, chilled >60min, frozen >30min); actual logic uses uniform 3-bucket aging |
| 9 | missing inventory | ops hygiene | hourly 06–24 | stock_take_base | DS-Cenral Ops doc → "Stock Take Base" + "Full Location Adherence" |
| 10 | skips (incident stocktake) | ops hygiene | hourly 06–24 | mxdcss_dcss.job | DS-Cenral Ops doc → stocktake force_closed |
| 11 | audit scores and status | audit | 1× daily 11:00 | historic_score1 | (no code repo entry — direct table reference in matrix file) |

each agent has its own python module under `agents/` with the same structure used in `nim-agents · sc`. drop in the existing scaffolding, swap the SQL + thresholds per the matrix file

### precedence note for claude code

the matrix v0.9 xlsx in `pinned/` is the design reference for thresholds, escalation, and dashboard layout. but for SQL source tables, this PLAN.md is now the source of truth — the table above corrects 4 agents (2, 3, 6, 8) where the matrix had earlier guesses. when the matrix says `fifo_report_logs` for agent 6 or `mp_order+picking_job` for agent 2, ignore those and use what's in this PLAN. matrix will be rebased to v1.0 after phase 1 stands up

specifically for agent 8 putaway delays — the threshold model changes too:
- old (matrix v0.9): per-storage-condition SLA (ambient >6h, chilled >60min, frozen >30min)
- new (saumy's confirmed): uniform 0-3hr / 3-6hr / >6hr aging buckets across all storage types
- tier triggers should fire on the >6hr bucket as the primary signal: t3 if pending_qty_above_6hr > 50, t2 if 25-50, t1 if 10-25 (claude code to validate against L7D distribution)

### rolling-threshold agents (phase 1)

agents 2 (iph pickers) and 3 (iph putaway) use rolling p-XX thresholds within opd-buckets, not hardcoded values. this is built into phase 1 — not a deferred "self-tuning" feature. mechanics:
- compute per-ds daily opd from ds_sku_daily_sales; assign each ds to small (<500) / medium (500-1500) / large (1500+) bucket
- compute p20 / p50 / p80 of ds-iph within each bucket using L7D rolling window
- t3 = ds_iph < bucket_p20 AND ≥10 jobs; t2 = bucket_p20–p50 AND ≥10 jobs; t1 = bucket_p50–p80 AND ≥10 jobs
- thresholds rebase nightly off L7D — no manual recalibration needed
- store the latest computed thresholds in `state.db` table `agent_thresholds(agent, bucket, p20, p50, p80, computed_at)` so the dashboard can display current cuts

---

## tiering (gate-then-tier, identical to sc)

8 steps per agent run
1. scope filter — active uae ds list (156)
2. absolute floor — metric > floor (per agent)
3. significance gate — store_contribution > 5pp of geo total OR worst 5
4. tier 3 — passed gate AND top 3 by contribution (min 10% share) AND metric > t3 critical
5. tier 2 — passed gate AND one of (top driver / above critical)
6. tier 1 — passed gate AND neither t2/t3
7. dedup — skip if (agent, ds, row_key) drafted in past 48h
8. consolidate — roll all alerts up to ds_code level

state stored in `state.db` table `alert_log(agent, ds_code, row_key, tier, drafted_at, draft_id)`

---

## escalation routing

ds-grain alerts (agents 1 ds-tabs, 2-10)

| tier | to | cc |
|------|-----|------|
| t1 | ds AM | — |
| t2 | ds AM | country ops lead (sharath@noon.com) |
| t3 | ds AM | country ops lead + ali (akh@noon.com) + commercial (saro for inv health agents) |

agent 1 vendor sub-tab override

| tier | to | cc |
|------|-----|------|
| t1 | vendor.email | — |
| t2 | vendor.email | sharath |
| t3 | vendor.email | sharath + harish (hgaudi@noon.com) |

agent 11 audit scores override
- t3 → ds AM + lead_name + logistic_head_name (from audit row) · cc ali (akh@)

ds → AM → email lookup is in the matrix file `ds_area_manager` sheet (156 rows). load this into `state.db` table `ds_routing` on first run

---

## agent 1 specifics — the only restructured agent

3 sub-tabs in card. each sub-tab independently runs the gate-then-tier logic on its own grain

### shared logic across all 3 sub-tabs

absent_flag derivation (from biometric.punch_status)
- present: punch_status = 'Punched Properly'
- excluded from rostered (planned leaves): punch_status IN ('Week Off', 'Annual Leave') OR LOWER(punch_status) LIKE '%leave'
- absent: everything else (No Punch, Single Punch, etc)

rostered (denominator) = active - week_off - al_leave - other_leave

absent% = absent / rostered

CRITICAL — DO NOT include people on Week Off, Annual Leave, Sick Leave, Comp Off, Maternity Leave, Marriage Leave, etc in the rostered count. these are planned absences and should NEVER trigger an alert. only count people who were expected to show up and didn't

reference query (production-ready) is in `pinned/agent_01_attendance_reference.sql` — claude code should use this as the canonical implementation. the manpower CTE in that file handles the logic exactly. apply the same pattern at temp-grain (HR_Desig LIKE '%TEMP%') and vendor-grain (after the bio→user→vendor join chain)

### sub-tab 1: cc-absent (ds-grain)
- filter: HR_Desig matches CORE/Senior CC/Barista/WH Assoc/Team Leader (i.e. NOT temp)
- routing: ds AM (t1) + sharath (t2) + harish g (t3)
- subject: `[ops-attendance][uae][t{n}][cc][{ds_code}]`

### sub-tab 2: temp-absent (ds-grain)
- filter: HR_Desig LIKE '%TEMP%'
- routing: ds AM (t1) + sharath (t2) + harish g (t3)
- subject: `[ops-attendance][uae][t{n}][temp][{ds_code}]`

### sub-tab 3: vendor (cross-stores)
- grain: vendor (one row per vendor, summed across all 156 ds)
- attribution chain (3-step):
  - step 1 primary: `LOWER(TRIM(biometric.name)) = LOWER(TRIM(CONCAT(user.first_name, ' ', user.last_name)))` → vendor (~81% match)
  - step 2 fallback: `REGEXP_EXTRACT(biometric.employee_id, r'^([A-Z]+)') = SUBSTR(vendor.shortcode, 1, 3)` for active externals (~14%)
  - step 3 residual: 'unknown' bucket — reported in card but no alert routed
- excluded: id_vendor=143 ('non uae' = noon CC catch-all). these absences roll into cc-absent ds-grain instead
- routing: vendor.email (t1) + sharath (t2) + harish g (t3)
- subject: `[ops-attendance][uae][t{n}][vendor][{vendor_shortcode}]`
- denominator at vendor-grain follows the same logic: rostered = active - week_off - al_leave - other_leave (just summed across all stores per vendor)

### shared thresholds (all 3 sub-tabs)
- t3: absent% > 8% OR absent_count > 10
- t2: absent% > 5% OR absent_count > 5
- t1: absent% > 3%

---

## DASHBOARD — the new piece

build a local react app at `localhost:3001` that mirrors the sc dashboard structure (`nim_panel.html`) but for ops · ds. 6 zones, identical layout to sc

zone 1: email triage (top-left, ~30% width)
- shows the gmail "matrix" label inbox (ops-* drafts only — filter on subject prefix `[ops-`)
- columns: subject · ds_code · tier · drafted_at · status (draft / sent / dismissed)
- click row → opens pre-formatted draft in gmail compose (popup)
- bulk actions: send selected · dismiss selected

zone 2: operational alert cards (center, ~50% width, scrollable)
- one card per agent (11 cards, color-coded by tier of newest alert)
- card header: agent name, last run timestamp, total t3/t2/t1 counts
- card body: top 5 alerts by contribution, click to expand
- agent 1 card has 3 sub-tabs visible (cc / temp / vendor) with the vendor sub-tab showing top vendors as separate rows
- card actions: re-run agent on demand · view full alert log · drill to bigquery

zone 3: personal daily todo (top-right, ~20% width)
- list of items vardan needs to action today
- check to close, rolls over uncompleted items to next day
- pre-populated with today's t3 alerts as todos

zone 4: weekly plan (right side, below todo)
- 7-day calendar view of agent runs + planned interventions
- shows which days had t3 spikes per agent

zone 5: compose / dictate panel (bottom-right)
- text area for vardan to write notes or dictate updates
- saves to `state.db` table `notes` for retrieval

zone 6: pinned artefacts (bottom strip)
- quick links to: matrix file (xlsx), ds_area_manager mapping, vendor_directory, this PLAN.md
- clickable opens locally

---

## data model — `state.db` schemas

```sql
CREATE TABLE alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    ds_code TEXT,
    vendor_shortcode TEXT,
    row_key TEXT NOT NULL,
    tier INTEGER NOT NULL,  -- 1, 2, 3
    metric_value REAL,
    contribution_pct REAL,
    drafted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    draft_id TEXT,  -- gmail draft id
    status TEXT DEFAULT 'draft',  -- draft, sent, dismissed
    UNIQUE(agent, ds_code, vendor_shortcode, row_key, drafted_at)
);
CREATE INDEX idx_alert_dedup ON alert_log(agent, ds_code, vendor_shortcode, row_key, drafted_at);

CREATE TABLE ds_routing (
    ds_code TEXT PRIMARY KEY,
    ds_name TEXT,
    geo TEXT,
    city TEXT,
    am_name TEXT,
    am_email TEXT,
    asst_mgr TEXT,
    supervisor TEXT,
    tl_name TEXT
);

CREATE TABLE vendor_routing (
    id_vendor INTEGER PRIMARY KEY,
    shortcode TEXT,
    vendor_name TEXT,
    vendor_email TEXT,
    vendor_type TEXT,  -- external, internal
    in_scope BOOLEAN DEFAULT 1
);

CREATE TABLE agent_run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    rows_scanned INTEGER,
    t1_count INTEGER,
    t2_count INTEGER,
    t3_count INTEGER,
    drafts_created INTEGER,
    error_message TEXT
);

CREATE TABLE todo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    rolled_over_count INTEGER DEFAULT 0
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_thresholds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    bucket TEXT,  -- 'small' / 'medium' / 'large' for iph agents; null for fixed-threshold agents
    p20 REAL,
    p50 REAL,
    p80 REAL,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent, bucket, computed_at)
);
```

---

## file layout

```
nim-agents-ops/
├── PLAN.md  (this file)
├── README.md
├── requirements.txt
├── state.db  (created on first run)
├── credentials/
│   └── gmail_oauth.json  (one-time setup)
├── pinned/
│   ├── nim-agents-ops-ds-matrix-v09-2026-04-27.xlsx
│   ├── vendor_directory_for_validation.xlsx
│   ├── non_uae_detail_for_validation.xlsx
│   ├── code_repo_ds_central_ops.pdf  (saumsingh — primary DS sql reference)
│   └── code_repo_2026_ops.pdf  (suarya/sauya — wh-focused, secondary reference for skips + adjustments)
├── api/
│   ├── app.py  (flask entrypoint, port 5001)
│   ├── routes/
│   │   ├── alerts.py
│   │   ├── agents.py
│   │   ├── routing.py
│   │   ├── todos.py
│   │   └── notes.py
│   └── lib/
│       ├── db.py
│       ├── bigquery_client.py
│       ├── gmail_client.py
│       └── tiering.py  (gate-then-tier logic, shared)
├── agents/
│   ├── _base.py  (Agent base class)
│   ├── agent_01_attendance.py
│   ├── agent_02_iph_pickers.py
│   ├── agent_03_iph_putaway.py
│   ├── agent_04_skips_picker.py
│   ├── agent_05_defects.py
│   ├── agent_06_fefo.py
│   ├── agent_07_adjustments.py
│   ├── agent_08_putaway_delays.py
│   ├── agent_09_missing_inventory.py
│   ├── agent_10_skips_stocktake.py
│   └── agent_11_audit_scores.py
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   └── src/
│       ├── App.jsx
│       ├── components/
│       │   ├── EmailTriage.jsx
│       │   ├── AlertCard.jsx
│       │   ├── AgentDashboard.jsx
│       │   ├── TodoPanel.jsx
│       │   ├── WeeklyPlan.jsx
│       │   ├── ComposePanel.jsx
│       │   └── PinnedArtefacts.jsx
│       └── api.js  (calls localhost:5001)
└── scheduler/
    └── windows_tasks.ps1  (creates scheduled tasks for all 11 agents)
```

---

## acceptance criteria

phase 1 ships when
1. all 11 agents run on schedule and write drafts to gmail "matrix" label
2. dashboard at localhost:3001 shows live data with all 6 zones populated
3. dedup works — re-running an agent within 48h doesn't create duplicate drafts
4. ds_routing table loaded with all 156 UAE rows from matrix xlsx
5. vendor_routing table loaded with all 87 vendors from matrix xlsx
6. agent 1 vendor sub-tab correctly excludes id_vendor=143
7. one full end-to-end test: trigger an absent% breach, verify draft created with correct subject + recipient + cc

---

## priorities for build

build order (recommended)
1. scaffolding: flask + sqlite + gmail OAuth (copy from sc, swap port)
2. agent_01_attendance — biggest, most complex (3 sub-tabs). build it first to validate the framework
3. agents 2-11 in parallel — same pattern once #2 is solid
4. dashboard — last, once API endpoints are live

defer to phase 2
- KSA + EGY ds mapping
- WhatsApp integration
- additional dashboard widgets that were originally drafted on sc PLAN.md but never built (meeting prep briefs, commitments tracker, 1:1 tracker, KPI dashboard, vendor/RO pipeline tracker, decisions log) — same backlog applies to ops-ds dashboard, all deferred

---

## what's NOT in scope for phase 1

- email auto-send (drafts only — vardan reviews + sends manually)
- KSA biometric (out of scope — agent 1 is uae-only)
- vendor email overrides (vardan validating in parallel; if any flagged, apply in v1.0 of matrix and re-load vendor_routing)
- the dashboard widgets from the deferred list above (meeting prep brief, commitments tracker, KPI dashboard, decisions log) — these were drafted in the original sc PLAN.md but never built. ops-ds will not build them in phase 1 either; they remain on the phase 2 backlog
