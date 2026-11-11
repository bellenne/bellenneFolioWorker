@echo off
if not exist "%~dp0dist\FolioImageWorker\FolioImageWorker.exe" (
  echo Сначала запустите Build Windows.cmd
  pause
  exit /b 1
)
start "" "%~dp0dist\FolioImageWorker\FolioImageWorker.exe"
if errorlevel 1 pause
