$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$pidFile = Join-Path $project 'spool\agent.pid'
$agent = Join-Path $project 'topaz_agent.py'

if (Test-Path -LiteralPath $pidFile) {
    $agentPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$agentPid)
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$agentPid" -ErrorAction SilentlyContinue
    if ($running -and $running.CommandLine -like "*$agent*") {
        Stop-Process -Id $agentPid
    }
    Remove-Item -LiteralPath $pidFile
}

Push-Location $project
try {
    docker compose stop folio-worker
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить контейнер FolioImageWorker.' }
} finally {
    Pop-Location
}
Write-Host 'FolioImageWorker и агент Topaz остановлены.'
