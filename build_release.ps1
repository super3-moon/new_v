param(
    [string]$DateTag = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Variant = ""
)

$ErrorActionPreference = "Stop"

$root = "E:\test"
$releaseRoot = Join-Path $root "release"
$workPath = Join-Path $root "_pkg_work"

if (-not (Test-Path -LiteralPath $releaseRoot)) {
    New-Item -ItemType Directory -Path $releaseRoot | Out-Null
}

$folderName = if ([string]::IsNullOrWhiteSpace($Variant)) {
    $DateTag
}
else {
    "${DateTag}_$Variant"
}

$distPath = Join-Path $releaseRoot $folderName
if (-not (Test-Path -LiteralPath $distPath)) {
    New-Item -ItemType Directory -Path $distPath | Out-Null
}

$specPath = $root
$entry = Join-Path $root "vmd_style_tool_qt6.py"
$styleDir = Join-Path $root "vmd_cube_styles"
$customJson = Join-Path $root "vmd_custom_styles.json"

Write-Output "Building release to: $distPath"

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "VMD_Multiwfn_StyleGenerator",
    "--distpath", $distPath,
    "--workpath", $workPath,
    "--specpath", $specPath,
    "--add-data", "$styleDir;vmd_cube_styles",
    "--add-data", "$customJson;.",
    $entry
)

& python @args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exePath = Join-Path $distPath "VMD_Multiwfn_StyleGenerator.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Release EXE not found: $exePath"
}

$logScript = Join-Path $root "append_sync_log.ps1"
if (Test-Path -LiteralPath $logScript) {
    & powershell -ExecutionPolicy Bypass -File $logScript `
      -Thread "packaging" `
      -Phase "DONE" `
      -Files "release\\$folderName\\VMD_Multiwfn_StyleGenerator.exe" `
      -Summary "release build completed ($folderName)" `
      -Result "success" | Out-Null
}

Write-Output "Release ready: $exePath"
