rem Build exe + installer in one go.
rem   dist\<app name>\<app name>.exe              portable folder
rem   dist\installer\<app name>-1.0.0-setup.exe  per-user installer, no UAC
rem Requires Inno Setup 6 (ISCC.exe) installed.
rem NOTE: ASCII-only on purpose - cmd.exe reads .bat as GBK.
setlocal
cd /d "%~dp0"

call build_exe.bat
if errorlevel 1 exit /b 1

set ISCC=
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe

if "%ISCC%"=="" (
  echo [FAILED] Inno Setup 6 not found ^(ISCC.exe^).
  echo Install it from https://jrsoftware.org/isdl.php and run this again.
  exit /b 1
)

"%ISCC%" installer.iss
if errorlevel 1 (
  echo.
  echo [FAILED] ISCC failed, see the log above.
  exit /b 1
)

echo.
echo [OK] Installer built in dist\installer
endlocal
