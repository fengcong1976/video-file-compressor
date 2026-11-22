rem Build the portable app folder: dist\<app name>\<app name>.exe
rem Requires: pip install -r requirements.txt pyinstaller
rem Requires the ffmpeg runtime in ffmpeg\bin\ (ffmpeg.exe + ffprobe.exe + DLLs).
rem Uses build.spec so the exclude list / size trimming stays in one place.
rem NOTE: this file is intentionally ASCII-only. cmd.exe reads .bat as GBK,
rem so Chinese path names here would turn into mojibake and break the build.
setlocal
cd /d "%~dp0"

rem Prefer the project venv, same as run.bat
set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

if not exist "ffmpeg\bin\ffmpeg.exe" (
  echo [FAILED] ffmpeg\bin\ffmpeg.exe not found.
  echo Copy the shared ffmpeg build ^(ffmpeg.exe, ffprobe.exe, *.dll^) into ffmpeg\bin\ first.
  exit /b 1
)

%PY% -m PyInstaller --noconfirm --clean "build.spec"
if errorlevel 1 (
  echo.
  echo [FAILED] PyInstaller build failed, see the log above.
  exit /b 1
)

echo.
echo [OK] PyInstaller build finished, see the dist folder.
endlocal
