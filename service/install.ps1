<#
.SYNOPSIS
    Register the extraction web apps so they start at logon and survive a closed terminal.

.DESCRIPTION
    Each pipeline gets its OWN scheduled task. That is the point of the split: removing the
    agreement pipeline later is `uninstall.ps1 -Pipeline agreement` plus deleting a folder,
    with nothing to unpick from a shared definition.

    Scheduled tasks rather than true Windows services, deliberately. A real service needs a
    service-aware entry point or a third-party wrapper such as NSSM, and gains nothing here:
    these apps bind to localhost for one logged-in person. A logon task is built in, needs
    no download and no stored password, and is visible in Task Scheduler where the user can
    see and stop it. The reason the apps kept dying was that they were children of a
    terminal; a task is not, which is the actual problem being solved.

.PARAMETER Pipeline
    invoice, agreement, or both (default).

.PARAMETER NoStart
    Register the task(s) but do not start them now.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File service\install.ps1
    powershell -ExecutionPolicy Bypass -File service\install.ps1 -Pipeline agreement
#>
[CmdletBinding()]
param(
    [ValidateSet("invoice", "agreement", "both")]
    [string]$Pipeline = "both",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$definitions = @(
    @{ Key = "invoice";   Name = "ExtractionInvoiceApp";   Port = 8000
       Script = Join-Path $here "invoice-server.cmd"
       Description = "Invoice extraction web app on http://127.0.0.1:8000" }
    @{ Key = "agreement"; Name = "ExtractionAgreementApp"; Port = 8100
       Script = Join-Path $here "agreement-server.cmd"
       Description = "Agreement extraction web app on http://127.0.0.1:8100" }
)

$wanted = if ($Pipeline -eq "both") { $definitions } else { $definitions | Where-Object { $_.Key -eq $Pipeline } }

foreach ($def in $wanted) {
    if (-not (Test-Path $def.Script)) {
        Write-Warning "$($def.Script) is missing; skipping $($def.Key)."
        continue
    }

    $action = New-ScheduledTaskAction -Execute $def.Script -WorkingDirectory $here
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # Interactive, so it runs as this person with their environment and their venvs, and
    # needs no stored credentials.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -Hidden `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)      # a server is meant to run forever
    $settings.MultipleInstances = "IgnoreNew"       # never two copies fighting for the port

    Register-ScheduledTask -TaskName $def.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $def.Description -Force | Out-Null
    Write-Host "registered  $($def.Name)  -> port $($def.Port)"

    if (-not $NoStart) {
        # A copy started from a terminal still owns the port; stop it before taking over.
        $existing = Get-NetTCPConnection -LocalPort $def.Port -State Listen -ErrorAction SilentlyContinue
        foreach ($connection in $existing) {
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "  stopped the process already on port $($def.Port)"
        }
        Start-ScheduledTask -TaskName $def.Name
        Write-Host "  started"
    }
}

if (-not $NoStart) {
    Write-Host "`nwaiting for the app(s) to come up..."
    foreach ($def in $wanted) {
        $up = $false
        foreach ($attempt in 1..40) {
            Start-Sleep -Seconds 3
            try {
                $code = (Invoke-WebRequest "http://127.0.0.1:$($def.Port)" -TimeoutSec 5 -UseBasicParsing).StatusCode
                Write-Host "  port $($def.Port) : UP ($code)"
                $up = $true
                break
            } catch { }
        }
        if (-not $up) {
            Write-Warning "  port $($def.Port) did not answer. See logs\$($def.Key)-server.log"
        }
    }
}

Write-Host "`nManage these in Task Scheduler, or:"
Write-Host "  Get-ScheduledTask ExtractionInvoiceApp, ExtractionAgreementApp | Format-Table TaskName, State"
Write-Host "  powershell -File service\uninstall.ps1 -Pipeline both"
