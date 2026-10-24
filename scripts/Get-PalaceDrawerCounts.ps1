<#
.SYNOPSIS
  Audits drawer counts across MemPalace wings (trading, coding, etc.).

.DESCRIPTION
  Inspects the active MemPalace SQLite database (chroma.sqlite3) or directory and outputs drawer metrics.
#>

[CmdletBinding()]
param(
    [string]$PalacePath = $(
        if ($env:MEMPALACE_PALACE_PATH) {
            $env:MEMPALACE_PALACE_PATH
        } elseif ($env:MEMPALACE_PATH) {
            $env:MEMPALACE_PATH
        } else {
            Join-Path $env:USERPROFILE ".mempalace\palace"
        }
    ),
    [string[]]$Wings = @(
        "trading", "coding", "agents", "knowledge",
        "conversations", "digital_life", "file_system",
        "infra", "music", "personal", "teaching", "physical"
    ),
    [switch]$Json
)

function Get-PalaceDrawerMetrics {
    param(
        [string]$Path,
        [string[]]$TargetWings
    )

    # Resolve actual palace directory holding chroma.sqlite3
    $resolvedPath = $Path
    $sqlitePath = Join-Path $Path "chroma.sqlite3"
    if (-not (Test-Path -LiteralPath $sqlitePath)) {
        $sub = Join-Path $Path "palace\chroma.sqlite3"
        if (Test-Path -LiteralPath $sub) {
            $resolvedPath = Join-Path $Path "palace"
            $sqlitePath = $sub
        }
    }

    $results = [ordered]@{}
    $wingCounts = @{}
    $totalDrawers = 0
    $sqliteAvailable = $false

    if (Test-Path -LiteralPath $sqlitePath) {
        $pythonScript = @'
import sys, json, os, sqlite3

sqlite_path = sys.argv[1]
conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
try:
    rows = conn.execute("""
        SELECT COALESCE(w.string_value, '?'), COUNT(*)
        FROM embeddings e
        JOIN segments s ON e.segment_id = s.id
        JOIN collections c ON s.collection = c.id
        LEFT JOIN embedding_metadata w ON w.id = e.id AND w.key = 'wing'
        WHERE c.name = 'mempalace_drawers' AND s.scope = 'METADATA'
        GROUP BY COALESCE(w.string_value, '?')
    """).fetchall()
    print(json.dumps(dict(rows)))
finally:
    conn.close()
'@
        try {
            $pyOutput = $pythonScript | python - $sqlitePath 2>$null
            if ($LASTEXITCODE -eq 0 -and $pyOutput) {
                $rawHashtable = $pyOutput | ConvertFrom-Json -AsHashtable
                if ($rawHashtable) {
                    $wingCounts = $rawHashtable
                    $sqliteAvailable = $true
                }
            }
        } catch { }
    }

    foreach ($wing in $TargetWings) {
        $count = 0
        if ($sqliteAvailable) {
            if ($wingCounts.ContainsKey($wing)) {
                $count = [int]$wingCounts[$wing]
            }
        } else {
            # Filesystem directory fallback
            $wingDir = Join-Path -Path $resolvedPath -ChildPath "wings\$wing"
            if (Test-Path -LiteralPath $wingDir) {
                $count = (Get-ChildItem -LiteralPath $wingDir -Recurse -File -ErrorAction SilentlyContinue).Count
            }
        }
        $totalDrawers += $count
        $results[$wing] = [ordered]@{
            wing = $wing
            drawerCount = $count
            status = if ($count -gt 0) { "active" } else { "empty-or-unmined" }
        }
    }

    return [ordered]@{
        palacePath = $resolvedPath
        sqliteDatabase = if ($sqliteAvailable) { $sqlitePath } else { $null }
        source = if ($sqliteAvailable) { "sqlite" } else { "filesystem-fallback" }
        auditedAt = (Get-Date).ToString("o")
        totalDrawers = $totalDrawers
        wings = $results
    }
}

$metrics = Get-PalaceDrawerMetrics -Path $PalacePath -TargetWings $Wings
if ($Json) {
    Write-Output ($metrics | ConvertTo-Json -Depth 4)
} else {
    Write-Output "MemPalace Wing Drawer Count Audit:"
    Write-Output "  Palace Path: $($metrics.palacePath)"
    Write-Output "  Source:      $($metrics.source) $(if ($metrics.sqliteDatabase) { "($($metrics.sqliteDatabase))" })"
    Write-Output "  Total:       $($metrics.totalDrawers) drawers across $($metrics.wings.Count) target wings"
    Write-Output ""
    foreach ($key in $metrics.wings.Keys) {
        $w = $metrics.wings[$key]
        Write-Output "  - Wing: $($w.wing.PadRight(15)) | Drawers: $($w.drawerCount.ToString().PadLeft(8)) ($($w.status))"
    }
}
