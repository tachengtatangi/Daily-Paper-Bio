@echo off
setlocal

:: Sync this repository's Codex skill folders into the current user's Codex skills directory.
:: Existing _shared\user-config.json and _shared\user-config.local.json are preserved.
:: Usage: run from the repository root, or double-click this file.

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DST=%USERPROFILE%\.codex\skills"
set "BACKUP=%TEMP%\daily-paper-bio-sync-%RANDOM%"

echo [sync] %SRC% -^> %DST%
if not exist "%DST%" mkdir "%DST%"
mkdir "%BACKUP%" >nul 2>nul

if exist "%DST%\_shared\user-config.json" copy /Y "%DST%\_shared\user-config.json" "%BACKUP%\user-config.json" >nul
if exist "%DST%\_shared\user-config.local.json" copy /Y "%DST%\_shared\user-config.local.json" "%BACKUP%\user-config.local.json" >nul

for %%D in (_shared paper-reader daily-papers daily-papers-fetch daily-papers-review daily-papers-notes generate-mocs playwright) do (
  echo [sync] %%D
  if not exist "%DST%\%%D" mkdir "%DST%\%%D"
  xcopy /Y /E /Q "%SRC%\%%D\" "%DST%\%%D\" >nul
)

if exist "%BACKUP%\user-config.json" copy /Y "%BACKUP%\user-config.json" "%DST%\_shared\user-config.json" >nul
if exist "%BACKUP%\user-config.local.json" copy /Y "%BACKUP%\user-config.local.json" "%DST%\_shared\user-config.local.json" >nul
rmdir /S /Q "%BACKUP%" >nul 2>nul

echo [sync] Done. Existing user config files were preserved.
pause