@echo off
if not exist "%~dp0dist\FolioImageWorker\FolioImageWorker.exe" exit /b 1
"%~dp0dist\FolioImageWorker\FolioImageWorker.exe" --stop
if errorlevel 1 pause
