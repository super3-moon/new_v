param(
    [string]$DateTag = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Variant = ""
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$releaseRoot = Join-Path $root "release"
$workPath = Join-Path $root "_pkg_work"
$specFile = Join-Path $root "VMD_Multiwfn_StyleGenerator.spec"
$entry = Join-Path $root "vmd_style_tool_qt6.py"
$styleDir = Join-Path $root "vmd_cube_styles"
$customJson = Join-Path $root "vmd_custom_styles.default.json"

foreach ($required in @($specFile, $entry, $styleDir, $customJson)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required build input not found: $required"
    }
}

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null

if ([string]::IsNullOrWhiteSpace($Variant)) {
    $versions = @()
    foreach ($directory in Get-ChildItem -LiteralPath $releaseRoot -Directory) {
        if ($directory.Name -eq $DateTag) {
            $versions += 1
        }
        elseif ($directory.Name -match "^$([regex]::Escape($DateTag))_v(?<version>\d+)$") {
            $versions += [int]$Matches.version
        }
    }
    $nextVersion = if ($versions.Count -eq 0) {
        1
    }
    else {
        ([int](($versions | Measure-Object -Maximum).Maximum)) + 1
    }
    $folderName = if ($nextVersion -eq 1) { $DateTag } else { "${DateTag}_v$nextVersion" }
}
else {
    $folderName = "${DateTag}_$Variant"
    if (Test-Path -LiteralPath (Join-Path $releaseRoot $folderName)) {
        throw "Release folder already exists; choose another Variant: $folderName"
    }
}

$distPath = Join-Path $releaseRoot $folderName
New-Item -ItemType Directory -Path $distPath | Out-Null

Write-Output "Building release to: $distPath"

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $distPath,
    "--workpath", $workPath,
    $specFile
)

& python @args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exePath = Join-Path $distPath "VMD_Multiwfn_StyleGenerator.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Release EXE not found: $exePath"
}

Write-Output "Release ready: $exePath"
