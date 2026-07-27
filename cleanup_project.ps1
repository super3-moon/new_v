param(
    [switch]$Apply,
    [string]$RecoveryRoot = "",
    [switch]$SkipReleaseArchive,
    [switch]$KeepBuildCache
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$releaseRoot = Join-Path $root "release"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

function Test-IsWithinProject {
    param([string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $prefix = $root.TrimEnd("\") + "\"
    return $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Remove-ProjectPath {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not (Test-IsWithinProject -Path $Path)) {
        throw "Refusing to remove a path outside the project: $Path"
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Assert-ZipReadable {
    param([string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        if ($zip.Entries.Count -eq 0) {
            throw "Recovery archive is empty: $Path"
        }
    }
    finally {
        $zip.Dispose()
    }
}

$releaseRows = @()
if (Test-Path -LiteralPath $releaseRoot) {
    foreach ($directory in Get-ChildItem -LiteralPath $releaseRoot -Directory) {
        if ($directory.Name -notmatch "^(?<date>\d{4}-\d{2}-\d{2})(?:_v(?<version>\d+))?$") {
            continue
        }
        $version = if ($Matches.version) { [int]$Matches.version } else { 1 }
        $releaseRows += [PSCustomObject]@{
            Name = $directory.Name
            Date = $Matches.date
            Version = $version
            Path = $directory.FullName
        }
    }
}

$keptReleases = @()
$oldReleases = @()
foreach ($group in $releaseRows | Group-Object Date) {
    $ordered = @($group.Group | Sort-Object Version)
    if ($ordered.Count -eq 0) {
        continue
    }
    $keptReleases += $ordered[-1]
    if ($ordered.Count -gt 1) {
        $oldReleases += $ordered[0..($ordered.Count - 2)]
    }
}

$cachePaths = @()
if (-not $KeepBuildCache) {
    foreach ($relative in @("_pkg_work", "__pycache__", "tests\__pycache__")) {
        $candidate = Join-Path $root $relative
        if (Test-Path -LiteralPath $candidate) {
            $cachePaths += $candidate
        }
    }
}

$generatedScripts = @()
foreach ($file in Get-ChildItem -LiteralPath $root -File -Filter "*.cmd") {
    if ($file.Name -ieq "AutoCube_OneClick.cmd") {
        continue
    }
    $head = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if ($head -match "Auto-generated software paths") {
        $generatedScripts += $file.FullName
    }
}

Write-Output "Project cleanup preview"
Write-Output "Keep releases:"
$keptReleases | Sort-Object Date | ForEach-Object { Write-Output "  $($_.Name)" }
Write-Output "Remove same-day intermediate releases:"
$oldReleases | Sort-Object Date, Version | ForEach-Object { Write-Output "  $($_.Name)" }
Write-Output "Remove build caches:"
$cachePaths | ForEach-Object { Write-Output "  $($_)" }
Write-Output "Remove generated root scripts:"
$generatedScripts | ForEach-Object { Write-Output "  $($_)" }

if (-not $Apply) {
    Write-Output "Preview only. Run again with -Apply to perform the cleanup."
    exit 0
}

if ($oldReleases.Count -gt 0) {
    if ([string]::IsNullOrWhiteSpace($RecoveryRoot)) {
        $RecoveryRoot = Join-Path (Split-Path -Parent $root) "test_cleanup_recovery_$timestamp"
    }
    New-Item -ItemType Directory -Path $RecoveryRoot -Force | Out-Null
    $archivePath = Join-Path $RecoveryRoot "intermediate_releases.zip"
    if ($SkipReleaseArchive) {
        if (-not (Test-Path -LiteralPath $archivePath)) {
            throw "SkipReleaseArchive requires an existing recovery archive: $archivePath"
        }
    }
    else {
        if (Test-Path -LiteralPath $archivePath) {
            $archivePath = Join-Path $RecoveryRoot "intermediate_releases_$timestamp.zip"
        }
        Compress-Archive `
            -LiteralPath @($oldReleases.Path) `
            -DestinationPath $archivePath `
            -CompressionLevel Optimal
    }
    Assert-ZipReadable -Path $archivePath
    @(
        "Created: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
        "Project: $root"
        "Recovery archive: $archivePath"
        "Removed releases:"
        ($oldReleases | Sort-Object Date, Version | ForEach-Object { "  $($_.Name)" })
    ) | Set-Content -LiteralPath (Join-Path $RecoveryRoot "cleanup_$timestamp.txt") -Encoding utf8
}

foreach ($release in $oldReleases) {
    Remove-ProjectPath -Path $release.Path
}
foreach ($path in $cachePaths) {
    Remove-ProjectPath -Path $path
}
foreach ($path in $generatedScripts) {
    Remove-ProjectPath -Path $path
}

Write-Output "Cleanup completed."
