$ErrorActionPreference = 'Stop'
$benchRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $benchRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing .venv; follow LOCAL_DEVELOPMENT.md installation instructions.' }
if (-not (Test-Path -LiteralPath (Join-Path $benchRoot 'frontend\dist\index.html'))) { throw 'Build frontend first: cd frontend; npm ci; npm run build' }
$runtimePath = Join-Path $benchRoot 'runtime'
New-Item -ItemType Directory -Force $runtimePath | Out-Null
foreach ($keyName in @('TUSHARE_TOKEN','POE_API_KEY')) {
    if (-not [Environment]::GetEnvironmentVariable($keyName,'Process')) {
        $keyValue = [Environment]::GetEnvironmentVariable($keyName,'User')
        if ($keyValue) { [Environment]::SetEnvironmentVariable($keyName,$keyValue,'Process') }
    }
}
$running = $false
try { $healthResult = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2; $running = $healthResult.ok } catch {}
if (-not $running) {
    $server = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','goldbench.api:app','--host','127.0.0.1','--port','8765') -WorkingDirectory $benchRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'api.log') -RedirectStandardError (Join-Path $runtimePath 'api-error.log')
    $server.Id | Set-Content -LiteralPath (Join-Path $runtimePath 'api.pid')
}
$workerActive = $false
$workerPidPath = Join-Path $runtimePath 'worker.pid'
if (Test-Path -LiteralPath $workerPidPath) {
    $workerId = [int](Get-Content -LiteralPath $workerPidPath)
    $found = Get-CimInstance Win32_Process -Filter "ProcessId=$workerId"
    $workerActive = $found -and ($found.CommandLine -like '*-m goldbench.worker*')
}
if (-not $workerActive) {
    $worker = Start-Process -FilePath $pythonPath -ArgumentList @('-m','goldbench.worker') -WorkingDirectory $benchRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'worker.log') -RedirectStandardError (Join-Path $runtimePath 'worker-error.log')
    $worker.Id | Set-Content -LiteralPath $workerPidPath
}
Write-Output 'GoldBench: http://127.0.0.1:8765'
Write-Output 'Logs: runtime/ ; close browser without stopping worker.'
