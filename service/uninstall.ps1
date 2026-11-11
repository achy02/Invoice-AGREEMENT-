<#
.SYNOPSIS
    Stop and remove the scheduled task(s) registered by install.ps1.

.DESCRIPTION
    Removing one pipeline never touches the other. `-Pipeline agreement` leaves the invoice
    app registered and running, which is the whole reason the two were registered separately.

.PARAMETER Pipeline
    invoice, agreement, or both (default).

.PARAMETER KeepRunning
    Remove the task but leave the process alive until it is next stopped by hand.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File service\uninstall.ps1 -Pipeline agreement
#>
[CmdletBinding()]
param(
    [ValidateSet("invoice", "agreement", "both")]
    [string]$Pipeline = "both",
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"

$definitions = @(
    @{ Key = "invoice";   Name = "ExtractionInvoiceApp";   Port = 8000 }
    @{ Key = "agreement"; Name = "ExtractionAgreementApp"; Port = 8100 }
)
$wanted = if ($Pipeline -eq "both") { $definitions } else { $definitions | Where-Object { $_.Key -eq $Pipeline } }

foreach ($def in $wanted) {
    $task = Get-ScheduledTask -TaskName $def.Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "not registered  $($def.Name)"
    } else {
        try { Stop-ScheduledTask -TaskName $def.Name -ErrorAction Stop } catch { }
        Unregister-ScheduledTask -TaskName $def.Name -Confirm:$false
        Write-Host "removed  $($def.Name)"
    }

    if (-not $KeepRunning) {
        # Stopping the task does not always reap the python child it launched through cmd.
        $listening = Get-NetTCPConnection -LocalPort $def.Port -State Listen -ErrorAction SilentlyContinue
        foreach ($connection in $listening) {
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "  freed port $($def.Port)"
        }
    }
}
