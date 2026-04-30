# nim-agents-ops · windows task scheduler registration
#
# registers scheduled tasks for all 11 agents per the cadence column in
# pinned/nim-agents-ops-ds-matrix-v09. cadence buckets:
#   daily  agents 1, 5, 6, 11   → 09:00 / 10:00 / 10:00 / 11:00 local
#   hourly agents 2-4, 7-10     → 0 6-23 * * * (every hour 06-23 local)
#                                 agent 8 stops at 18:00 per matrix
#
# run as admin to register tasks. tasks live under \nim-agents-ops\.
# unregister with:
#   Get-ScheduledTask -TaskPath \nim-agents-ops\ | Unregister-ScheduledTask -Confirm:$false

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
$batch = Join-Path $root "scheduler\run_agent.bat"

function Register-AgentTask {
    param(
        [string]$AgentId,
        [string]$Trigger
    )
    $taskName = "nim-agents-ops\$AgentId"
    $action = New-ScheduledTaskAction -Execute $batch -Argument $AgentId -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

    if ($Trigger -eq "daily-09") {
        $t = New-ScheduledTaskTrigger -Daily -At "09:00"
    } elseif ($Trigger -eq "daily-10") {
        $t = New-ScheduledTaskTrigger -Daily -At "10:00"
    } elseif ($Trigger -eq "daily-11") {
        $t = New-ScheduledTaskTrigger -Daily -At "11:00"
    } elseif ($Trigger -eq "hourly-06-23") {
        $t = @()
        06..23 | ForEach-Object { $t += New-ScheduledTaskTrigger -Daily -At ("{0:00}:00" -f $_) }
    } elseif ($Trigger -eq "hourly-06-18") {
        $t = @()
        06..18 | ForEach-Object { $t += New-ScheduledTaskTrigger -Daily -At ("{0:00}:00" -f $_) }
    } else {
        throw "unknown trigger spec: $Trigger"
    }

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $t `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "registered $taskName ($Trigger)"
}

# per matrix v0.9 cadence column
Register-AgentTask "agent_01_attendance"        "daily-09"
Register-AgentTask "agent_02_iph_pickers"       "hourly-06-23"
Register-AgentTask "agent_03_iph_putaway"       "hourly-06-23"
Register-AgentTask "agent_04_skips_picker"      "hourly-06-23"
Register-AgentTask "agent_05_defects"           "daily-10"
Register-AgentTask "agent_06_fefo"              "daily-10"
Register-AgentTask "agent_07_adjustments"       "hourly-06-23"
Register-AgentTask "agent_08_putaway_delays"    "hourly-06-18"
Register-AgentTask "agent_09_missing_inventory" "hourly-06-23"
Register-AgentTask "agent_10_skips_stocktake"   "hourly-06-23"
Register-AgentTask "agent_11_audit_scores"      "daily-11"

Write-Host "`nall 11 agents registered under \nim-agents-ops\"
Write-Host "view: Get-ScheduledTask -TaskPath \nim-agents-ops\"
