<#
.SYNOPSIS
  Idempotent bootstrap of the local Synthea toolchain (no system-wide install).

.DESCRIPTION
  1. Portable Eclipse Temurin JRE (default 21, Windows x64 zip via api.adoptium.net) -> tools/jre/
  2. Latest *semver-tagged* Synthea release synthea-with-dependencies.jar (api.github.com) -> tools/synthea/
     GitHub's "latest" release is the rolling tag `master-branch-latest`, which is not reproducible;
     the script falls back to the newest vX.Y.Z release and records the tag.
  3. Writes config/synthea/VERSION.json {synthea_tag, jar_sha256, jre_version, downloaded_at}.

  Re-running is a no-op when the JRE runs and the jar matches VERSION.json. Use -Force to redownload.
#>
[CmdletBinding()]
param(
    [int]$JreMajor = 21,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x faster without the progress bar (PS 5.1)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root       = Split-Path -Parent $PSScriptRoot
$JreDir     = Join-Path $Root 'tools\jre'
$SyntheaDir = Join-Path $Root 'tools\synthea'
$CfgDir     = Join-Path $Root 'config\synthea'
$VersionFile = Join-Path $CfgDir 'VERSION.json'
$Jar        = Join-Path $SyntheaDir 'synthea-with-dependencies.jar'
$Java       = Join-Path $JreDir 'bin\java.exe'
$Headers    = @{ 'User-Agent' = 'anticipa-bootstrap' }

New-Item -ItemType Directory -Force $JreDir, $SyntheaDir, $CfgDir | Out-Null

function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower() }

function Get-JreVersion {
    if (-not (Test-Path $Java)) { return $null }
    # `java -version` prints to stderr; run via cmd so PS 5.1 does not wrap it in ErrorRecords.
    $out = cmd /c "`"$Java`" -version 2>&1"
    if ($LASTEXITCODE -ne 0) { return $null }
    if (($out -join "`n") -match 'version "([^"]+)"') { return $Matches[1] }   # join: -match on an array does not set $Matches
    return $null
}

$prev = $null
if ((Test-Path $VersionFile) -and -not $Force) { $prev = Get-Content $VersionFile -Raw | ConvertFrom-Json }

# ---- JRE -------------------------------------------------------------------------------------
$jreVersion = Get-JreVersion
if ($Force -or -not $jreVersion -or -not $jreVersion.StartsWith("$JreMajor")) {
    Write-Host "Resolving Temurin JRE $JreMajor (windows x64)..."
    $assets = Invoke-RestMethod -Headers $Headers -Uri "https://api.adoptium.net/v3/assets/latest/$JreMajor/hotspot?architecture=x64&image_type=jre&os=windows"
    $pkg = ($assets | Where-Object { $_.binary.package.name -like '*.zip' } | Select-Object -First 1).binary.package
    if (-not $pkg) { throw "No Windows x64 JRE zip found for Temurin $JreMajor" }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("anticipa-jre-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        $zip = Join-Path $tmp $pkg.name
        Write-Host "Downloading $($pkg.name) ($([math]::Round($pkg.size/1MB,1)) MB)..."
        Invoke-WebRequest -Headers $Headers -Uri $pkg.link -OutFile $zip
        if ((Get-Sha256 $zip) -ne $pkg.checksum.ToLower()) { throw "JRE checksum mismatch for $($pkg.name)" }
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $tmp 'x') -Force
        $inner = Get-ChildItem (Join-Path $tmp 'x') -Directory | Select-Object -First 1
        if (Test-Path $JreDir) { Remove-Item -Recurse -Force $JreDir }
        Move-Item -LiteralPath $inner.FullName -Destination $JreDir
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    $jreVersion = Get-JreVersion
    if (-not $jreVersion) { throw 'Downloaded JRE does not run' }
} else { Write-Host "JRE $jreVersion already present." }

# ---- Synthea jar -----------------------------------------------------------------------------
Write-Host 'Resolving Synthea release...'
$rel = Invoke-RestMethod -Headers $Headers -Uri 'https://api.github.com/repos/synthetichealth/synthea/releases/latest'
if ($rel.tag_name -notmatch '^v\d+\.\d+\.\d+$') {
    $all = Invoke-RestMethod -Headers $Headers -Uri 'https://api.github.com/repos/synthetichealth/synthea/releases?per_page=30'
    $rel = $all | Where-Object { $_.tag_name -match '^v\d+\.\d+\.\d+$' -and -not $_.prerelease -and -not $_.draft } |
        Sort-Object { [version]($_.tag_name.TrimStart('v')) } -Descending | Select-Object -First 1
}
if (-not $rel) { throw 'No semver-tagged Synthea release found' }
$asset = $rel.assets | Where-Object { $_.name -eq 'synthea-with-dependencies.jar' } | Select-Object -First 1
if (-not $asset) { throw "Release $($rel.tag_name) has no synthea-with-dependencies.jar" }

$needJar = $Force -or -not (Test-Path $Jar) -or -not $prev -or ($prev.synthea_tag -ne $rel.tag_name) -or ((Get-Sha256 $Jar) -ne $prev.jar_sha256)
if ($needJar) {
    Write-Host "Downloading Synthea $($rel.tag_name) ($([math]::Round($asset.size/1MB,1)) MB)..."
    $part = "$Jar.part"
    Invoke-WebRequest -Headers $Headers -Uri $asset.browser_download_url -OutFile $part
    if ((Get-Item $part).Length -ne $asset.size) { Remove-Item $part -Force; throw 'Synthea jar size mismatch (truncated download)' }
    Move-Item -Force $part $Jar
} else { Write-Host "Synthea $($rel.tag_name) already present." }

# ---- VERSION.json ----------------------------------------------------------------------------
$downloadedAt = if ($needJar -or -not $prev) { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } else { $prev.downloaded_at }
[ordered]@{
    synthea_tag   = $rel.tag_name
    jar_sha256    = Get-Sha256 $Jar
    jre_version   = $jreVersion
    downloaded_at = $downloadedAt
} | ConvertTo-Json | Set-Content -Encoding utf8 $VersionFile
Write-Host "OK  Synthea $($rel.tag_name), JRE $jreVersion  ->  $VersionFile"
