# dashboard

single-file HTML dashboard for the morpheus ds-ops command center.
fetches live data from the flask api on `localhost:5001`.

## files

| file | purpose |
|---|---|
| `morpheus-dsops_commandcenter.html` | live dashboard · open via `file://` after starting flask |
| `THRESHOLDS.md` | locked thresholds reference for all 11 agents |
| `calibration.xlsx` | L7D distribution + suggested thresholds (regenerate with `python -m api.lib.calibrate`) |
| `snapshots/` | historical snapshots of the dashboard html |

## how to run

```
# start flask backend (from repo root)
cd ..
set BQ_BILLING_PROJECT=noonbinimops
set GMAIL_MODE=oauth          # or file for the no-gmail fallback
python -m api.app

# open the dashboard in chrome
start dashboard/morpheus-dsops_commandcenter.html
```

dashboard polls `/api/health`, `/api/agents`, `/api/alerts?hours=48&limit=5000`,
`/api/routing/ds`, and `/api/platform_health` every 60 seconds.

## tracked outputs (mirrored from outputs/Morpheus/)

the user's working copies live at:
`C:\Users\vnagar\Documents\Claude\outputs\Morpheus\`

these get mirrored into this folder so the repo carries the full deliverable.
when you edit the live file, copy the result back here and commit.
