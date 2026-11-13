<#
.SYNOPSIS
  Publish a code-only snapshot of the repository to GitHub (origin/main).

.DESCRIPTION
  Owner rule (2026-11-08): GitHub receives only code, the root README.md, and the
  walkthrough/ and diagrams/ folders. Docs/, CLAUDE.md and every other *.md file stay local.

  Local history (which contains the documentation) is never pushed. Each publish builds the
  snapshot in a temporary worktree on the orphan branch `publish`, commits it, and pushes
  `publish` to <Remote>/main. Publishing aborts if a documentation path, an env file, or a
  key-shaped string would be included.

.EXAMPLE
  ./scripts/publish.ps1 -DryRun        # list what would be published, change nothing
  ./scripts/publish.ps1 -Message "wave-1 integration"
#>
[CmdletBinding()]
param(
    [string]$Source = "HEAD",
    [string]$Remote = "origin",
    [string]$Message = "",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Include = @("backend", "frontend", "scripts", "config", "docker", "docker-compose.yml",
             ".gitignore", ".gitattributes", ".env.example", "README.md", "LICENSE", "walkthrough", "diagrams")
$KeepAllUnder = @("walkthrough/", "diagrams/")   # whitelisted folders are published as-is
$SecretPattern = 'sk-ant-[a-z]{3}-[A-Za-z0-9_-]{10,}|sb_secret_[A-Za-z0-9]{8,}|rnd_[A-Za-z0-9]{10,}|v1\.CmQK[A-Za-z0-9]{10,}|eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sbp_[a-f0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}'

function Invoke-Git {
    # Windows PowerShell 5.1 turns git's informational stderr into errors; capture it and judge by exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = @(& git @args 2>&1) } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed ($LASTEXITCODE): $($output -join ' ')" }
}

function Test-Git {
    # Runs git and returns $true on exit code 0, without turning stderr into terminating errors.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & git @args 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $previous }
}

$root = (& git rev-parse --show-toplevel).Trim()
Set-Location $root
$sha = (& git rev-parse $Source).Trim()
$short = $sha.Substring(0, 8)
$worktree = Join-Path ([IO.Path]::GetTempPath()) "anticipa-publish-$PID"

Invoke-Git worktree add --quiet --detach $worktree $sha
try {
    Push-Location $worktree
    if (Test-Git show-ref --verify --quiet refs/heads/publish) {
        Invoke-Git checkout --quiet publish
    } else {
        Invoke-Git checkout --quiet --orphan publish
    }

    # Start from an empty tree, then copy only the whitelisted paths from the source commit.
    Test-Git rm -r -q --cached --ignore-unmatch . | Out-Null
    Get-ChildItem -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force
    foreach ($path in $Include) {
        if (Test-Git cat-file -e "${sha}:$path") { Invoke-Git checkout $sha -- $path }
    }

    # Drop every markdown file except the root README and the whitelisted folders.
    foreach ($file in @(& git ls-files -- "*.md")) {
        if ($file -eq "README.md") { continue }
        if (@($KeepAllUnder | Where-Object { $file.StartsWith($_) }).Count -gt 0) { continue }
        Invoke-Git rm -q --cached -- $file
        Remove-Item -Force -LiteralPath $file
    }
    # Large video files are published as GitHub Release assets, never as repository files.
    foreach ($file in @(& git ls-files -- "walkthrough/*.mp4" "walkthrough/*.webm" "walkthrough/*.mov" "walkthrough/*.mkv")) {
        Invoke-Git rm -q --cached -- $file
        Remove-Item -Force -LiteralPath $file
    }
    Invoke-Git add -A

    # Safety gates: no documentation, no env files, no key-shaped strings.
    $files = @(& git ls-files)
    $forbidden = @($files | Where-Object {
        $_ -match '^(Docs/|CLAUDE\.md$)' -or $_ -match '(^|/)\.env$' -or $_ -match '(^|/)\.env\.(?!example$)[^/]+$'
    })
    if ($forbidden.Count -gt 0) { throw "Refusing to publish: $($forbidden -join ', ')" }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $secretHits = @(& git grep -l -E $SecretPattern -- . 2>$null)
    $ErrorActionPreference = $previous
    if ($secretHits.Count -gt 0) { throw "Refusing to publish: key-shaped strings in $($secretHits -join ', ')" }

    "Snapshot of $short : $($files.Count) files"
    $files | ForEach-Object { ($_ -split "/")[0] } | Group-Object | Sort-Object Name |
        ForEach-Object { "  {0,6}  {1}" -f $_.Count, $_.Name }

    if ($DryRun) { "Dry run: nothing committed or pushed."; return }

    if (@(& git status --porcelain).Count -eq 0) {
        "No changes since the last publish."
    } else {
        $suffix = if ($Message) { " - $Message" } else { "" }
        Invoke-Git commit -q -m "publish: code snapshot of $short$suffix"
    }
    Invoke-Git push $Remote publish:main
    "Published $short to $Remote/main."
}
finally {
    Pop-Location
    Test-Git worktree remove --force $worktree | Out-Null
}
