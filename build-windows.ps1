$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$venv = Join-Path $project '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    py -3.14 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python 3.14.' }
}
& $python -m pip install -r (Join-Path $project 'requirements.txt') 'pyinstaller==6.22.3'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости сборки.' }

Push-Location $project
try {
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name FolioImageWorker --collect-submodules uvicorn `
        --hidden-import folio_worker.app `
        folio_desktop.py
    if ($LASTEXITCODE -ne 0) { throw 'Сборка FolioImageWorker.exe завершилась с ошибкой.' }
    $result = Join-Path $project 'dist\FolioImageWorker\FolioImageWorker.exe'
    $probe = Join-Path $project 'dist\FolioImageWorker\self-test.ok'
    Start-Process -FilePath $result -ArgumentList @('--self-test', '--self-test-output', $probe) -WindowStyle Hidden -Wait
    if (-not (Test-Path -LiteralPath $probe)) { throw 'Собранное приложение не прошло проверку запуска.' }
    Remove-Item -LiteralPath $probe
    $version = & $python -c "from folio_worker import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось определить версию.' }
    $archive = Join-Path $project "dist\FolioImageWorker-$version.zip"
    Compress-Archive -Path (Join-Path $project 'dist\FolioImageWorker') -DestinationPath $archive -Force
    Write-Host "Готово: $result (версия $version), архив: $archive"
} finally {
    Pop-Location
}
