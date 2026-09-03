#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Negative-proof fixtures for mempalace gates discovered by repo-health.

.DESCRIPTION
    Proves each undeclared gate can fail for the intended reason, then restores
    and captures a pass. Exit 0 only when every ritual completes.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stamp = Get-Date -Format 'yyyyMMddTHHmmssZ'
$workRoot = Join-Path $repoRoot ".tmp/gate-negative-proof-$stamp"
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Test-WorkflowContract {
    param(
        [Parameter(Mandatory)][string]$WorkflowPath,
        [Parameter(Mandatory)][string[]]$RequiredSubstrings,
        [string]$Label
    )
    $text = Get-Content -LiteralPath $WorkflowPath -Raw
    foreach ($needle in $RequiredSubstrings) {
        # Use IndexOf — PowerShell -like treats [] as a character class.
        if ($text.IndexOf($needle, [System.StringComparison]::Ordinal) -lt 0) {
            throw "Workflow contract failed for ${Label}: missing required fragment '$needle'."
        }
    }
}

Write-Host "== 1) scripts/check-markdown-links.ps1 =="
$mdCase = Join-Path $workRoot 'markdown-links'
New-Item -ItemType Directory -Path $mdCase -Force | Out-Null
Push-Location $mdCase
try {
    git init -q | Out-Null
    git config user.email 'gate-proof@example.invalid'
    git config user.name 'Gate Proof'
    Set-Content -LiteralPath (Join-Path $mdCase 'README.md') -Value "# ok`n" -Encoding utf8
    git add README.md
    git commit -qm 'seed'
    $checkScript = Join-Path $repoRoot 'scripts/check-markdown-links.ps1'
    & pwsh -NoProfile -File $checkScript -RepoPath $mdCase
    Assert-True ($LASTEXITCODE -eq 0) "Expected clean markdown fixture to pass. Exit=$LASTEXITCODE"
    Set-Content -LiteralPath (Join-Path $mdCase 'broken.md') -Value "[missing](./does-not-exist-gate-proof.md)`n" -Encoding utf8
    git add broken.md
    git commit -qm 'break-link'
    Assert-True ((Get-Content (Join-Path $mdCase 'broken.md') -Raw) -match 'does-not-exist-gate-proof') 'BREAK DID NOT APPLY for markdown link fixture'
    & pwsh -NoProfile -File $checkScript -RepoPath $mdCase
    $brokenExit = $LASTEXITCODE
    Assert-True ($brokenExit -eq 1) "Expected broken markdown link to exit 1. Got $brokenExit"
    git rm -q broken.md
    git commit -qm 'restore'
    & pwsh -NoProfile -File $checkScript -RepoPath $mdCase
    Assert-True ($LASTEXITCODE -eq 0) "Expected restored markdown fixture to pass. Exit=$LASTEXITCODE"
    Write-Host "markdown-links: fail=$brokenExit restore=0 OK"
}
finally {
    Pop-Location
}

Write-Host "== 2) .github/workflows/version-guard.yml (local agreement mirror) =="
$vgCase = Join-Path $workRoot 'version-guard'
New-Item -ItemType Directory -Path $vgCase -Force | Out-Null
$versionFiles = @(
    'mempalace/version.py',
    'pyproject.toml',
    '.claude-plugin/marketplace.json',
    '.claude-plugin/plugin.json',
    '.codex-plugin/plugin.json'
)
foreach ($rel in $versionFiles) {
    $dest = Join-Path $vgCase $rel
    New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot $rel) -Destination $dest -Force
}

function Get-VersionAgreementStatus {
    param([string]$Root)
    $py = ([regex]::Match((Get-Content (Join-Path $Root 'mempalace/version.py') -Raw), '__version__\s*=\s*"([^"]+)"')).Groups[1].Value
    $pyproject = ([regex]::Match((Get-Content (Join-Path $Root 'pyproject.toml') -Raw), '(?m)^version\s*=\s*"([^"]+)"')).Groups[1].Value
    $marketplace = ((Get-Content (Join-Path $Root '.claude-plugin/marketplace.json') -Raw | ConvertFrom-Json).plugins[0].version)
    $plugin = ((Get-Content (Join-Path $Root '.claude-plugin/plugin.json') -Raw | ConvertFrom-Json).version)
    $codex = ((Get-Content (Join-Path $Root '.codex-plugin/plugin.json') -Raw | ConvertFrom-Json).version)
    $all = @($py, $pyproject, $marketplace, $plugin, $codex)
    if (@($all | Where-Object { $_ -ne $py }).Count -gt 0) {
        return [pscustomobject]@{ ok = $false; py = $py; values = $all }
    }
    return [pscustomobject]@{ ok = $true; py = $py; values = $all }
}

$before = Get-VersionAgreementStatus -Root $vgCase
Assert-True ($before.ok) "Baseline version agreement should pass before break. values=$($before.values -join ',')"
$pyprojectPath = Join-Path $vgCase 'pyproject.toml'
$originalPyproject = Get-Content -LiteralPath $pyprojectPath -Raw
$brokenPyproject = $originalPyproject -replace '(?m)^version\s*=\s*"3\.3\.4"', 'version = "0.0.0-gate-proof"'
Assert-True ($brokenPyproject -ne $originalPyproject) 'BREAK DID NOT APPLY for version-guard pyproject mutation'
Set-Content -LiteralPath $pyprojectPath -Value $brokenPyproject -Encoding utf8NoBOM
$broken = Get-VersionAgreementStatus -Root $vgCase
Assert-True (-not $broken.ok) "Expected version mismatch after breaking pyproject. values=$($broken.values -join ',')"
Assert-True ($broken.values -contains '0.0.0-gate-proof') 'Broken version string missing from agreement check'
Set-Content -LiteralPath $pyprojectPath -Value $originalPyproject -Encoding utf8NoBOM
$restored = Get-VersionAgreementStatus -Root $vgCase
Assert-True ($restored.ok) "Expected restored version agreement to pass. values=$($restored.values -join ',')"
# Workflow file itself must still contain the agreement step (contract).
Test-WorkflowContract -WorkflowPath (Join-Path $repoRoot '.github/workflows/version-guard.yml') -Label 'version-guard' -RequiredSubstrings @(
    'Verify all sources agree',
    'check "pyproject.toml"',
    "startsWith(github.ref, 'refs/tags/v')"
)
Write-Host 'version-guard: fail=mismatch restore=ok OK'

Write-Host "== 3) .github/workflows/ci.yml contract =="
$ciPath = Join-Path $repoRoot '.github/workflows/ci.yml'
$ciSha = Get-FileSha256 $ciPath
$ciRequired = @(
    "github.head_ref != 'dev'",
    'test-windows:',
    'runs-on: windows-latest',
    'concurrency:'
)
Test-WorkflowContract -WorkflowPath $ciPath -Label 'ci' -RequiredSubstrings $ciRequired
$ciTmp = Join-Path $workRoot 'ci.yml'
Copy-Item -LiteralPath $ciPath -Destination $ciTmp -Force
$ciBroken = (Get-Content -LiteralPath $ciTmp -Raw) -replace "github\.head_ref != 'dev'", "github.head_ref != 'never-match-gate-proof'"
Assert-True ($ciBroken -ne (Get-Content -LiteralPath $ciPath -Raw)) 'BREAK DID NOT APPLY for ci.yml skip-dev guard'
Set-Content -LiteralPath $ciTmp -Value $ciBroken -Encoding utf8NoBOM
try {
    Test-WorkflowContract -WorkflowPath $ciTmp -Label 'ci-broken' -RequiredSubstrings $ciRequired
    throw 'Expected broken ci.yml contract to fail, but it passed.'
}
catch {
    Assert-True ($_.Exception.Message -match "missing required fragment") "Wrong failure reason for ci.yml: $($_.Exception.Message)"
}
Assert-True ((Get-FileSha256 $ciPath) -eq $ciSha) 'ci.yml must remain unchanged after temp-mutation proof'
Write-Host 'ci.yml: fail=missing-skip-dev restore=unchanged OK'

Write-Host "== 4) .github/workflows/deploy-docs.yml contract =="
$deployPath = Join-Path $repoRoot '.github/workflows/deploy-docs.yml'
$deploySha = Get-FileSha256 $deployPath
$deployRequired = @(
    'branches: [main]',
    'bun run docs:build',
    'actions/deploy-pages'
)
Test-WorkflowContract -WorkflowPath $deployPath -Label 'deploy-docs' -RequiredSubstrings $deployRequired
$deployTmp = Join-Path $workRoot 'deploy-docs.yml'
Copy-Item -LiteralPath $deployPath -Destination $deployTmp -Force
$deployBroken = (Get-Content -LiteralPath $deployTmp -Raw) -replace 'branches: \[main\]', 'branches: [develop]'
Assert-True ($deployBroken -ne (Get-Content -LiteralPath $deployPath -Raw)) 'BREAK DID NOT APPLY for deploy-docs main branch'
Set-Content -LiteralPath $deployTmp -Value $deployBroken -Encoding utf8NoBOM
try {
    Test-WorkflowContract -WorkflowPath $deployTmp -Label 'deploy-docs-broken' -RequiredSubstrings $deployRequired
    throw 'Expected broken deploy-docs contract to fail, but it passed.'
}
catch {
    Assert-True ($_.Exception.Message -match "missing required fragment") "Wrong failure reason for deploy-docs: $($_.Exception.Message)"
}
Assert-True ((Get-FileSha256 $deployPath) -eq $deploySha) 'deploy-docs.yml must remain unchanged'
Write-Host 'deploy-docs.yml: fail=main-branch-removed restore=unchanged OK'

Write-Host "== 5) .github/workflows/sync-upstream.yml contract =="
$syncPath = Join-Path $repoRoot '.github/workflows/sync-upstream.yml'
$syncSha = Get-FileSha256 $syncPath
$syncRequired = @(
    'mergeUpstream',
    'err.status === 409',
    'core.setFailed'
)
Test-WorkflowContract -WorkflowPath $syncPath -Label 'sync-upstream' -RequiredSubstrings $syncRequired
$syncTmp = Join-Path $workRoot 'sync-upstream.yml'
Copy-Item -LiteralPath $syncPath -Destination $syncTmp -Force
$syncBroken = (Get-Content -LiteralPath $syncTmp -Raw) -replace 'core\.setFailed', 'core.warning'
Assert-True ($syncBroken -ne (Get-Content -LiteralPath $syncPath -Raw)) 'BREAK DID NOT APPLY for sync-upstream setFailed'
Set-Content -LiteralPath $syncTmp -Value $syncBroken -Encoding utf8NoBOM
try {
    Test-WorkflowContract -WorkflowPath $syncTmp -Label 'sync-upstream-broken' -RequiredSubstrings $syncRequired
    throw 'Expected broken sync-upstream contract to fail, but it passed.'
}
catch {
    Assert-True ($_.Exception.Message -match "missing required fragment") "Wrong failure reason for sync-upstream: $($_.Exception.Message)"
}
Assert-True ((Get-FileSha256 $syncPath) -eq $syncSha) 'sync-upstream.yml must remain unchanged'
Write-Host 'sync-upstream.yml: fail=setFailed-removed restore=unchanged OK'

Write-Host ''
Write-Host 'mempalace gate-negative-proof rituals passed'
exit 0
