#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Validate repo-local Markdown links.

.DESCRIPTION
    Scans Markdown files in the repository and verifies that relative local
    links resolve on disk. External URLs are ignored. The check skips common
    generated folders and fenced code blocks.
#>

param(
    [string]$RepoPath = $null
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    param([string]$Candidate)
    if ($Candidate) { return (Resolve-Path $Candidate).Path }
    $root = git -C (Get-Location) rev-parse --show-toplevel 2>$null
    if (-not $root) { throw "Unable to determine git repository root. Pass -RepoPath." }
    return (Resolve-Path $root).Path
}

function Resolve-MarkdownTarget {
    param(
        [string]$RepoRoot,
        [string]$SourceDirectory,
        [string]$Target
    )

    $cleanTarget = $Target.Trim()
    if ($cleanTarget.StartsWith('<') -and $cleanTarget.EndsWith('>')) {
        $cleanTarget = $cleanTarget.Substring(1, $cleanTarget.Length - 2)
    }

    if ([string]::IsNullOrWhiteSpace($cleanTarget)) { return $null }
    if ($cleanTarget -match '^(https?|mailto|tel|mdc|file):') { return $null }
    if ($cleanTarget.StartsWith('#')) { return $null }
    if ($cleanTarget.StartsWith('data:')) { return $null }

    $cleanTarget = $cleanTarget.Split('#')[0].Split('?')[0]
    if ([string]::IsNullOrWhiteSpace($cleanTarget)) { return $null }

    $candidateBases = @()
    if ($cleanTarget.StartsWith('/')) {
        $relativeTarget = $cleanTarget.TrimStart('/','\')
        $websiteRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot 'website'))
        $sourceFullPath = [System.IO.Path]::GetFullPath($SourceDirectory)
        if ($sourceFullPath.StartsWith($websiteRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            $candidateBases += [System.IO.Path]::GetFullPath((Join-Path $websiteRoot $relativeTarget))
        }
        $candidateBases += [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $relativeTarget))
    }
    else {
        $candidateBases += [System.IO.Path]::GetFullPath((Join-Path $SourceDirectory $cleanTarget))
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidateBases) {
        $candidates.Add($candidate)
        if ([string]::IsNullOrWhiteSpace([System.IO.Path]::GetExtension($candidate))) {
            $candidates.Add("$candidate.md")
            $candidates.Add("$candidate.mdx")
            $candidates.Add((Join-Path $candidate 'index.md'))
            $candidates.Add((Join-Path $candidate 'index.mdx'))
        }
    }

    return $candidates | Sort-Object -Unique
}

function Test-IsRepoLocalPath {
    param(
        [string]$RepoRoot,
        [string]$CandidatePath
    )

    $normalizedRoot = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\','/')
    $normalizedCandidate = [System.IO.Path]::GetFullPath($CandidatePath)
    return $normalizedCandidate.StartsWith($normalizedRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($normalizedCandidate, $normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)
}

$repoRoot = Get-RepoRoot -Candidate $RepoPath
$markdownFiles = @()

try {
    $stagedRelative = @(git -C $repoRoot diff --cached --name-only --diff-filter=ACMR -- '*.md' '*.mdx' 2>$null)
    foreach ($relative in $stagedRelative) {
        if ([string]::IsNullOrWhiteSpace($relative)) { continue }
        $full = Join-Path $repoRoot $relative
        if (Test-Path -LiteralPath $full) {
            $markdownFiles += Get-Item -LiteralPath $full
        }
    }
} catch {
    $markdownFiles = @()
}

if (-not $markdownFiles) {
    try {
        $trackedRelative = @(git -C $repoRoot ls-files '*.md' '*.mdx' 2>$null)
        foreach ($relative in $trackedRelative) {
            if ([string]::IsNullOrWhiteSpace($relative)) { continue }
            $full = Join-Path $repoRoot $relative
            if (Test-Path -LiteralPath $full) {
                $markdownFiles += Get-Item -LiteralPath $full
            }
        }
    } catch {
        $markdownFiles = @()
    }
}

if (-not $markdownFiles) {
    $markdownFiles = Get-ChildItem -Path $repoRoot -Recurse -File -Include *.md,*.mdx |
        Where-Object {
            $_.FullName -notmatch '\\(\.git|node_modules|dist|build|coverage|\.next|\.turbo|out|vendor|bin|obj)\\'
        }
}

$markdownFiles = $markdownFiles | Sort-Object FullName -Unique

if (-not $markdownFiles) {
    Write-Host "[Docs] No Markdown files found. Skipping." -ForegroundColor DarkGray
    exit 0
}

$broken = New-Object System.Collections.Generic.List[object]
$pattern = '\[[^\]]+\]\((?!https?://|mailto:|tel:|#)([^)]+)\)'

foreach ($file in $markdownFiles) {
    $lines = Get-Content -LiteralPath $file.FullName
    $inFence = $false

    for ($index = 0; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        $trimmed = $line.TrimStart()
        if ($trimmed.StartsWith('```')) {
            $inFence = -not $inFence
            continue
        }

        if ($inFence) { continue }

        $matches = [regex]::Matches($line, $pattern)
        foreach ($match in $matches) {
            $target = $match.Groups[1].Value
            $resolvedCandidates = @(Resolve-MarkdownTarget -RepoRoot $repoRoot -SourceDirectory $file.DirectoryName -Target $target)
            if ($resolvedCandidates.Count -eq 0) { continue }

            $repoLocalCandidates = @(
                $resolvedCandidates | Where-Object {
                    Test-IsRepoLocalPath -RepoRoot $repoRoot -CandidatePath $_
                }
            )
            if ($repoLocalCandidates.Count -eq 0) { continue }

            $targetExists = $false
            foreach ($candidate in $repoLocalCandidates) {
                if (Test-Path -LiteralPath $candidate) {
                    $targetExists = $true
                    break
                }
            }

            if (-not $targetExists) {
                $broken.Add([pscustomobject]@{
                    File = $file.FullName
                    Line = $index + 1
                    Target = $target
                })
            }
        }
    }
}

if ($broken.Count -gt 0) {
    Write-Host "[Docs] Broken local Markdown links detected:" -ForegroundColor Red
    foreach ($item in $broken) {
        Write-Host ("  {0}:{1} -> {2}" -f $item.File, $item.Line, $item.Target) -ForegroundColor Red
    }
    exit 1
}

Write-Host "[Docs] Local Markdown links look valid." -ForegroundColor Green
exit 0
