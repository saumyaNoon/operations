# morpheus · setup guide (run from clone)

instructions for running the morpheus ds-ops command center on a fresh machine
after `git clone https://github.com/vnnoon/operations`.

---

## prerequisites

- **windows 10/11** (linux/mac will work too, swap `.bat` for shell scripts)
- **python 3.12** (3.10+ should work)
- **gcloud cli** with access to `noonbinimops` BigQuery project
- a noon corporate google account with read access to the source datasets:
  - `noonbinimksa.darkstore.*` (ipp_daily, stock_take_base, complains_raw_*, etc.)
  - `noonbinimops.fulfillment.*` (adjustments_master_*, putaway_pendency_v2, ipp, geomap)
  - `noonbinimdwh.modelling.fifo_report_logs`
  - `noondwh.mxdcss_dcss.*` (job, warehouse for stocktake adherence)
  - `noondwh.zsku_catexsp.psku` + `noonbinimprc.pricing.cost_price_retail`

---

## step 1 · clone + install

```powershell
cd C:\Users\<you>\Documents
git clone https://github.com/vnnoon/operations nim-agents-ops
cd nim-agents-ops

pip install -r requirements.txt
```

---

## step 2 · authenticate to BigQuery

```powershell
gcloud auth application-default login
```

a browser pops to authorize. confirm you can hit BQ:

```powershell
python -c "from google.cloud import bigquery; bigquery.Client(project='noonbinimops').query('SELECT 1').result(); print('bq ok')"
```

---

## step 3 · seed routing tables (one-time)

loads 156 UAE darkstores + 87 vendors into `state.db`:

```powershell
python -m api.lib.seed
```

expected: `seeded 156 ds_routing rows + 87 vendor_routing rows (id_vendor=143 marked out_of_scope)`

---

## step 4 · run agents (populate alert_log)

run all 11 agents fresh:

```powershell
$env:BQ_BILLING_PROJECT="noonbinimops"
$env:GMAIL_MODE="file"     # writes drafts as .eml files; switch to oauth later

foreach ($a in @(
  "agent_01_attendance","agent_02_iph_pickers","agent_03_iph_putaway",
  "agent_04_skips_picker","agent_05_defects","agent_06_fefo",
  "agent_07_adjustments","agent_08_putaway_delays","agent_09_missing_inventory",
  "agent_10_skips_stocktake","agent_11_audit_scores"
)) {
  Write-Host "=== $a ==="; python -m agents.$a
}
```

each agent prints a summary like:
```
{'agent': 'agent_05_defects', 'scanned': 154, 't1': 0, 't2': 0, 't3': 5, 'drafts': 0}
```

---

## step 5 · start the api + dashboard

**terminal 1 — flask api on :5001**

```powershell
cd C:\Users\<you>\Documents\nim-agents-ops
$env:BQ_BILLING_PROJECT="noonbinimops"
$env:GMAIL_MODE="file"     # or unset for gmail oauth (see step 7)
python -m api.app
```

verify: `curl http://127.0.0.1:5001/api/health`

**terminal 2 — open the dashboard**

```powershell
start C:\Users\<you>\Documents\nim-agents-ops\dashboard\morpheus-dsops_commandcenter.html
```

or just double-click the file in explorer. it'll open in your default browser
and start polling the api on :5001.

if the page is blank: hard-refresh (`Ctrl+Shift+R`).

---

## step 6 · automate · windows task scheduler (optional)

register all 11 agents with the cadence schedule from the matrix:

```powershell
powershell -ExecutionPolicy Bypass -File scheduler\windows_tasks.ps1
```

inspect: `Get-ScheduledTask -TaskPath \nim-agents-ops\`

---

## step 7 · gmail draft creation (optional)

by default `GMAIL_MODE=file` writes drafts as `.eml` files under `drafts/<date>/`.
to land them directly in gmail:

1. google cloud console → APIs & Services → enable Gmail API
2. credentials → create OAuth client ID → type **Desktop app** → download JSON
3. save as `nim-agents-ops/credentials/gmail_oauth.json`
4. restart flask without `GMAIL_MODE=file` (defaults to oauth)
5. on first draft creation, a browser pops to authorize → token saved to `credentials/token.json`

drafts land in the gmail "matrix" label (auto-created on first run).

---

## file layout

```
nim-agents-ops/
├── PLAN.md                                       original design doc
├── README.md                                     repo overview
├── SETUP.md                                      this file
├── requirements.txt
├── state.db                                      sqlite (created on first run, gitignored)
├── credentials/                                  gmail_oauth.json + token.json (gitignored)
├── drafts/                                       .eml files when GMAIL_MODE=file (gitignored)
├── pinned/                                       handoff reference: matrix v0.9, vendor dir, saumy ref pdf
├── agents/                                       11 agent modules
│   ├── _base.py                                  Agent base class · run lifecycle
│   ├── agent_01_attendance.py                   ...
│   └── agent_11_audit_scores.py
├── api/
│   ├── app.py                                    flask routes
│   └── lib/
│       ├── db.py                                 sqlite schema + helpers
│       ├── bigquery_client.py                    bq wrapper
│       ├── gmail_client.py                       gmail oauth + file-mode dispatcher
│       ├── tiering.py                            gate-then-tier shared logic
│       ├── routing.py                            (to, cc) resolver per agent + tier
│       ├── draft_builder.py                      one template per agent
│       ├── platform_health.py                    12 KPIs for the bottom strip
│       ├── seed.py                               loads ds_routing + vendor_routing from xlsx
│       └── calibrate.py                          regenerates threshold L7D distribution xlsx
├── dashboard/
│   ├── morpheus-dsops_commandcenter.html         live dashboard (open via file://)
│   ├── THRESHOLDS.md                             locked thresholds reference
│   ├── QUERY_REFERENCE.md                        full SQL per agent (for analyst review)
│   ├── calibration.xlsx                          L7D distribution + suggested cuts
│   ├── README.md
│   └── snapshots/                                historical dashboard snapshots
└── scheduler/
    ├── start_flask.bat
    ├── start_dashboard.bat
    ├── run_agent.bat
    └── windows_tasks.ps1
```

---

## sanity checks

```powershell
# api up?
curl http://127.0.0.1:5001/api/health

# how many alerts in the last 48h?
curl "http://127.0.0.1:5001/api/alerts?hours=48&limit=2000" | python -c "import sys,json; print(len(json.load(sys.stdin)))"

# bottom-strip KPIs
curl http://127.0.0.1:5001/api/platform_health | python -m json.tool
```

---

## troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| `quota exceeded` from BQ | gcloud ADC has no quota project | `gcloud auth application-default set-quota-project noonbinimops` |
| dashboard says "backend offline" | flask not running | start flask (step 5) |
| dashboard shows old data after edits | browser cache | `Ctrl+Shift+R` (or open devtools + refresh) |
| agent run says "scanned 0" | wrong target_date for that table (D-1 vs today) | check the agent's `target_date` override in `__init__` |
| gmail draft creation hangs | `credentials/gmail_oauth.json` missing | use file mode or set up oauth (step 7) |

---

ping vardan if anything above doesn't behave as documented.
