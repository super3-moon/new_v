@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul

set "A_DIR=%CD%"
if "%A_DIR:~-1%"=="\" set "A_DIR=%A_DIR:~0,-1%"
cd /d "%A_DIR%"

echo [INFO] A folder: %A_DIR%
echo.

rem ===== User editable software paths (edit these two lines) =====
set "MULTIWFN_EXE=E:\path\to\Multiwfn.exe"
set "VMD_EXE=E:\path\to\vmd.exe"
rem ===============================================================

if not exist "%MULTIWFN_EXE%" (
  echo [ERROR] Multiwfn path is invalid: %MULTIWFN_EXE%
  pause
  exit /b 1
)

if not exist "%VMD_EXE%" (
  echo [ERROR] VMD path is invalid: %VMD_EXE%
  pause
  exit /b 1
)

echo [INFO] Multiwfn: %MULTIWFN_EXE%
echo [INFO] VMD: %VMD_EXE%
for %%D in ("%MULTIWFN_EXE%") do set "MULTIWFN_DIR=%%~dpD"
if "%MULTIWFN_DIR:~-1%"=="\" set "MULTIWFN_DIR=%MULTIWFN_DIR:~0,-1%"
set "Multiwfnpath=%MULTIWFN_DIR%"
echo [INFO] Multiwfnpath: %Multiwfnpath%
echo.
echo [INFO] Launching Multiwfn...
echo [INFO] Generate ONE .cub file in this A folder, then exit Multiwfn.
start "" /wait "%MULTIWFN_EXE%"

set "CUBE_FILE="
for /f "delims=" %%F in ('dir /b /a:-d /o:-d "%A_DIR%\*.cub" 2^>nul') do (
  if not defined CUBE_FILE set "CUBE_FILE=%A_DIR%\%%F"
)

if not defined CUBE_FILE (
  echo [ERROR] No .cub file found in A folder: %A_DIR%
  pause
  exit /b 1
)

for %%B in ("%CUBE_FILE%") do set "CUBE_BASE=%%~nB"
echo [INFO] Using cube file: %CUBE_FILE%

:ask_iso
set "ISO_RAW="
set "ISO_NORM="
set /p ISO_RAW=Enter isovalue (positive number, e.g. 0.05): 
if not defined ISO_RAW goto ask_iso

for /f "usebackq delims=" %%I in (`powershell -NoLogo -NoProfile -Command "$v=0.0; $raw=$env:ISO_RAW; $ok=[double]::TryParse($raw,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$v); if(-not $ok){$ok=[double]::TryParse($raw,[ref]$v)}; if($ok){$v=[Math]::Abs($v); if($v -gt 0){$v.ToString('0.############',[Globalization.CultureInfo]::InvariantCulture)}}"`) do set "ISO_NORM=%%I"

if not defined ISO_NORM (
  echo [WARN] Invalid number. Try again.
  goto ask_iso
)

set "CUBE_TCL=%CUBE_FILE:\=/%"
set "A_TCL=%A_DIR:\=/%"
set "TCL_FILE=%TEMP%\autocube_%RANDOM%%RANDOM%%RANDOM%.tcl"

> "%TCL_FILE%" (
  echo # Auto-generated single-file AutoCube workflow
  echo set AUTO_CUBE_FILE "%CUBE_TCL%"
  echo set AUTO_ISOVAL %ISO_NORM%
  echo set AUTO_OUTDIR "%A_TCL%"
  echo set AUTO_BASENAME "%CUBE_BASE%"
  echo.
  echo proc _autocube_unique_path {target} {
  echo     set candidate $target
  echo     if {![file exists $candidate]} {
  echo         return $candidate
  echo     }
  echo     set ext [file extension $target]
  echo     set root [file rootname $target]
  echo     if {$ext eq ""} {
  echo         set root $target
  echo     }
  echo     set i 1
  echo     while {[file exists $candidate]} {
  echo         set candidate "${root}_$i$ext"
  echo         incr i
  echo     }
  echo     return $candidate
  echo }
  echo.
  echo if {[llength [info commands _autocube_builtin_render]] == 0} {
  echo     rename render _autocube_builtin_render
  echo     proc render {args} {
  echo         global AUTO_OUTDIR AUTO_BASENAME
  echo         set passthrough [list list hasaa aasamples aosamples formats format options default]
  echo         if {[llength $args] == 0} {
  echo             return [uplevel 1 [list _autocube_builtin_render]]
  echo         }
  echo         set cmd0 [lindex $args 0]
  echo         if {[lsearch -exact $passthrough $cmd0] ^>= 0} {
  echo             return [uplevel 1 [list _autocube_builtin_render {*}$args]]
  echo         }
  echo         if {[llength $args] ^< 2} {
  echo             return [uplevel 1 [list _autocube_builtin_render {*}$args]]
  echo         }
  echo.
  echo         set method [lindex $args 0]
  echo         set requested [lindex $args 1]
  echo         if {$requested eq ""} {
  echo             set requested "${AUTO_BASENAME}_render"
  echo         }
  echo.
  echo         set filenameOnly [file tail $requested]
  echo         if {$filenameOnly eq ""} {
  echo             set filenameOnly "${AUTO_BASENAME}_render"
  echo         }
  echo.
  echo         set target [file normalize [file join $AUTO_OUTDIR $filenameOnly]]
  echo         set target [_autocube_unique_path $target]
  echo.
  echo         set newargs [list $method $target]
  echo         if {[llength $args] ^> 2} {
  echo             set newargs [concat $newargs [lrange $args 2 end]]
  echo         }
  echo.
  echo         set code [catch {uplevel 1 [list _autocube_builtin_render {*}$newargs]} msg opts]
  echo         if {$code != 0} {
  echo             return -options $opts $msg
  echo         }
  echo.
  echo         puts "AutoCube: Render output saved to $target"
  echo         foreach i [molinfo list] {
  echo             mol delete $i
  echo         }
  echo         puts "AutoCube: Deleted current molecule and isosurfaces in VMD."
  echo         return $msg
  echo     }
  echo }
  echo.
  echo set mater Glossy
  echo color Display Background white
  echo display depthcue off
  echo display rendermode GLSL
  echo axes location Off
  echo color Name C tan
  echo color change rgb tan 0.700000 0.560000 0.360000
  echo material change mirror Opaque 0.0
  echo material change outline Opaque 4.000000
  echo material change outlinewidth Opaque 0.5
  echo material change ambient Glossy 0.1
  echo material change diffuse Glossy 0.600000
  echo material change opacity Glossy 0.75
  echo material change shininess Glossy 1.0
  echo light 3 on
  echo.
  echo foreach i [molinfo list] {
  echo     mol delete $i
  echo }
  echo.
  echo mol new $AUTO_CUBE_FILE type cube waitfor all
  echo mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000
  echo mol addrep top
  echo mol modstyle 1 top Isosurface $AUTO_ISOVAL 0 0 0 1 1
  echo mol modcolor 1 top ColorID 12
  echo mol modmaterial 1 top $mater
  echo mol addrep top
  echo set negiso [expr {-$AUTO_ISOVAL}]
  echo mol modstyle 2 top Isosurface $negiso 0 0 0 1 1
  echo mol modcolor 2 top ColorID 22
  echo mol modmaterial 2 top $mater
  echo display distance -8.0
  echo display height 10
  echo.
  echo menu main on
  echo menu graphics on
  echo menu render on
  echo.
  echo puts "AutoCube: Isosurface drawing is ready (not rendered automatically)."
  echo puts "AutoCube: Render manually in VMD. Output will be forced into A folder."
  echo puts "AutoCube: After each successful render, current molecule and surfaces are deleted."
)

if not exist "%TCL_FILE%" (
  echo [ERROR] Failed to generate temporary VMD Tcl script.
  pause
  exit /b 1
)

echo [INFO] Launching VMD and loading drawing script...
start "" /wait "%VMD_EXE%" -e "%TCL_FILE%"

del /q "%TCL_FILE%" >nul 2>nul
for %%E in (cub dat) do (
  for /f "delims=" %%F in ('dir /b /a:-d "%A_DIR%\*.%%E" 2^>nul') do (
    del /q "%A_DIR%\%%F" >nul 2>nul
  )
)
for /f "delims=" %%F in ('dir /b /a:-d "%A_DIR%\*" 2^>nul') do (
  if "%%~xF"=="" del /q "%A_DIR%\%%F" >nul 2>nul
)
echo [INFO] Deleted .cub and .dat files in A folder.
echo [INFO] Workflow finished.
pause
exit /b 0

