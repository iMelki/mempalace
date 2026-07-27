<#
.SYNOPSIS
  Audits drawer counts across MemPalace wings (trading, coding, etc.).

.DESCRIPTION
  Inspects the active MemPalace database or directory and outputs drawer metrics.
#>

[CmdletBinding()]
param(
    [string]$PalacePath = "$env:USERPROFILE\.mempalace",
    [string[]]$Wings = @("trading", "coding", "agents", "knowledge"),
    [switch]$Json
)

function Get-PalaceDrawerMetrics {
    param(
        [string]$Path,
        [string[]]$TargetWings
    )

    $results = [ordered]@{}
    foreach ($wing in $TargetWings) {
        $wingDir = Join-Path -Path $Path -ChildPath "wings\$wing"
        $count = 0
        if (Test-Path -LiteralPath $wingDir) {
            $count = (Get-ChildItem -LiteralPath $wingDir -Recurse -File -ErrorAction SilentlyContinue).Count
        }
        $results[$wing] = [ordered]@{
            wing = $wing
            drawerCount = $count
            status = if ($count -gt 0) { "active" } else { "empty-or-unmined" }
        }
    }

    return [ordered]@{
        palacePath = $Path
        auditedAt = (Get-Date).ToString("o")
        wings = $results
    }
}

$metrics = Get-PalaceDrawerMetrics -Path $PalacePath -TargetWings $Wings
if ($Json) {
    Write-Output ($metrics | ConvertTo-Json -Depth 3)
} else {
    Write-Output "MemPalace Wing Drawer Count Audit:"
    foreach ($key in $metrics.wings.Keys) {
        $w = $metrics.wings[$key]
        Write-Output "  - Wing: $($w.wing) | Drawers: $($w.drawerCount) ($($w.status))"
    }
}
