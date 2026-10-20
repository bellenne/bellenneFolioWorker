$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$spool = Join-Path $project 'spool'
$pidFile = Join-Path $spool 'agent.pid'
$agent = Join-Path $project 'topaz_agent.py'
$python = 'C:\Python314\python.exe'
$topaz = 'D:\Soft\Gigapixel\bin\gigapixel.bat'

if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $topaz)) {
    throw 'Python или Topaz Gigapixel не найден по настроенному пути.'
}
New-Item -ItemType Directory -Path $spool -Force | Out-Null
Push-Location $project
try {
    docker compose up --build -d folio-worker
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить контейнер FolioImageWorker.' }
} finally {
    Pop-Location
}

if (Test-Path -LiteralPath $pidFile) {
    $agentPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$agentPid)
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$agentPid" -ErrorAction SilentlyContinue
    if ($running -and $running.CommandLine -like "*$agent*") {
        Write-Host "Агент Topaz уже работает (PID $agentPid)."
        exit 0
    }
}

$process = Start-Process -FilePath $python -ArgumentList @(
    $agent, '--spool', $spool, '--mode', 'auto', '--exe', $topaz,
    '--config', (Join-Path $project '.env')
) -WorkingDirectory $project -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $spool 'agent.stdout.log') `
    -RedirectStandardError (Join-Path $spool 'agent.stderr.log') -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id
Write-Host "FolioImageWorker и агент Topaz запущены (PID $($process.Id))."
