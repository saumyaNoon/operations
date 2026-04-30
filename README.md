# nim-agents · ops · ds command center (morpheus)

operational sister build to `nim-agents · sc`. 11 darkstore-operations agents
covering attendance, iph, inventory health, ops hygiene, and audit. mirrors
the sc architecture exactly — same flask + react + sqlite + gmail-api pattern.

- flask api on `localhost:5001` (sc lives on 5000/5055)
- single static dashboard at `C:\Users\vnagar\Documents\Claude\outputs\Morpheus\morpheus-dsops_commandcenter.html`
- sqlite at `state.db`
- gmail drafts dropped into "matrix" label
- subject prefix `[ds ops-...]` (sc uses `[exec/struct/fnv/...]`)
- phase 1 scope: UAE only · 156 darkstores · 6 area managers

## quick start

```powershell
cd C:\Users\vnagar\Documents\nim-agents-ops

# 1. install python deps
pip install -r requirements.txt

# 2. seed routing tables (156 ds + 87 vendors from matrix xlsx)
python -m api.lib.seed

# 3. start flask api on :5001
.\scheduler\start_flask.bat

# 4. open the dashboard (single html, fetches live from :5001)
.\scheduler\start_dashboard.bat
#    or open C:\Users\vnagar\Documents\Claude\outputs\Morpheus\morpheus-dsops_commandcenter.html

# 5. register windows task scheduler entries (run as admin once)
powershell -ExecutionPolicy Bypass -File .\scheduler\windows_tasks.ps1
```

## the 11 agents

| # | id | bucket | cadence | source table |
|---|-----|--------|---------|--------------|
| 1 | `agent_01_attendance`        | manpower    | 1× 09:00 | Biometric_base_v2_3 + ipp_daily_ae |
| 2 | `agent_02_iph_pickers`       | manpower    | hourly 06–23 | ipp_daily_ae (outbound) |
| 3 | `agent_03_iph_putaway`       | manpower    | hourly 06–23 | ipp_daily_ae (inbound)  |
| 4 | `agent_04_skips_picker`      | inv health  | hourly 06–23 | scgoms_cwpicking |
| 5 | `agent_05_defects`           | inv health  | 1× 10:00 | complains_raw_ae_bh_agg |
| 6 | `agent_06_fefo`              | inv health  | 1× 10:00 | central_expiry_raw + central_expiry_raw_adjusted |
| 7 | `agent_07_adjustments`       | ops hygiene | hourly 06–23 | mxdcss_dcss adjustments |
| 8 | `agent_08_putaway_delays`    | ops hygiene | hourly 06–18 | mxfulfillment_norms.box + box_item + putaway_job_line |
| 9 | `agent_09_missing_inventory` | ops hygiene | hourly 06–23 | stock_take_base |
| 10 | `agent_10_skips_stocktake`  | ops hygiene | hourly 06–23 | mxdcss_dcss.job |
| 11 | `agent_11_audit_scores`     | audit       | 1× 11:00 | historic_score1 |

run a single agent locally for testing:

```powershell
python -m agents.agent_01_attendance
```

## architecture notes

### agent 1 has 3 sub-tabs

`agent_01_attendance` runs the gate-then-tier logic three times — once per
sub-tab — on its own grain:

- `cc`     ds-grain · `HR_Desig` matches CORE/Senior CC/Barista/Warehouse Associate/Team Leader
- `temp`   ds-grain · `HR_Desig LIKE '%TEMP%'`
- `vendor` vendor-grain (cross-stores) using a 3-step join chain
  - bio.name → user → vendor (~81%)
  - bio prefix → vendor.shortcode prefix (~14%)
  - residual unknown (no alert)

vendor sub-tab excludes `id_vendor=143` ('non uae' = noon CC catch-all).
those absences roll into the cc-absent ds-grain instead — the seed script
marks `id_vendor=143` with `in_scope=0` in `vendor_routing`.

shared thresholds across all 3 sub-tabs:

- t3 absent% > 8% OR absent_count > 10
- t2 absent% > 5% OR absent_count > 5
- t1 absent% > 3%

rostered (denominator) excludes everyone on planned leave (week off, annual,
sick, comp off, maternity, marriage etc). only people expected to show up
and didn't are counted as absent.

### rolling-threshold agents (2 + 3)

agents 2 (iph pickers) and 3 (iph putaway) compute per-bucket p20/p50/p80
nightly off the L7D distribution. buckets: small (<500 opd) / medium
(500–1500) / large (1500+). thresholds saved to `agent_thresholds`.

```
t3  ds_iph < bucket_p20  AND  ≥10 jobs
t2  ds_iph p20–p50       AND  ≥10 jobs
t1  ds_iph p50–p80       AND  ≥10 jobs
```

### precedence

when matrix xlsx and PLAN.md disagree on source tables, PLAN.md wins.
table mappings corrected in PLAN for agents 2, 3, 6, 8.

## escalation routing

ds-grain alerts (everything except agent 1 vendor sub-tab + agent 11):

| tier | to | cc |
|------|-----|------|
| t1 | ds AM | — |
| t2 | ds AM | sharath |
| t3 | ds AM | sharath + ali (+ saro for inv health agents) |

agent 1 vendor sub-tab override:

| tier | to | cc |
|------|-----|------|
| t1 | vendor.email | — |
| t2 | vendor.email | sharath |
| t3 | vendor.email | sharath + harish |

agent 11 audit override: t3 → ds AM + lead_name + logistic_head_name
(from row) · cc ali

## state.db schema

see `api/lib/db.py`. tables:

- `alert_log`           dedup state, drafted alerts, status
- `actions`             draft / dismiss / send action log
- `ds_routing`          156 UAE ds → AM email
- `vendor_routing`      87 vendors → email + in_scope
- `agent_run_history`   per-run telemetry
- `agent_thresholds`    rolling p20/p50/p80 per opd-bucket (agents 2/3)
- `todo`                personal daily todos
- `notes`               compose panel notes

## gmail oauth

drop the gmail oauth client_id json into `credentials/gmail_oauth.json`. on
first agent run, a browser pops for one-time OAuth, then `credentials/token.json`
is created and reused.

if gmail api is blocked by workspace admin, set
`GMAIL_MODE=file` to dump drafts as `.eml` files under `drafts/<date>/`
which can be batch-pushed via the local gmail mcp later.

```powershell
setx GMAIL_MODE file
```

## dashboard zones

`morpheus-dsops_commandcenter.html` is a single self-contained file. open it
via file:// (or `scheduler\start_dashboard.bat`) and it polls the flask api at
`:5001` every 30s. mirrors the sc panel pattern. 6 zones:

1. email triage (top-left, ~30%)
2. operational alert cards (center, ~50%) — 11 cards, agent 1 with sub-tabs
3. personal daily todo (top-right, ~20%)
4. weekly plan (right, below todo)
5. compose / dictate panel (bottom-right)
6. pinned artefacts (bottom strip)

## dedup

re-running an agent within 48h does NOT create duplicate drafts. dedup key
is `(agent, sub_tab, ds_code)` for ds-grain alerts and
`(agent, vendor_shortcode)` for the vendor sub-tab.

## acceptance test

trigger an absent% breach and verify draft created with correct subject +
recipient + cc:

```powershell
python -m agents.agent_01_attendance
# check gmail "matrix" label or drafts/<today>/ for the .eml
```

## phase 2 (deferred)

- KSA + EGY ds mapping
- WhatsApp integration
- vendor email overrides
- meeting prep briefs, commitments tracker, KPI dashboard, decisions log
