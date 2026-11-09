$ErrorActionPreference = 'Stop'
$benchRoot = Split-Path -Parent $PSScriptRoot
foreach ($service in @('api','worker')) {
    $pidPath = Join-Path $benchRoot "runtime\$service.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $serviceId = [int](Get-Content -LiteralPath $pidPath)
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceId"
        if ($process -and $process.ExecutablePath -eq (Join-Path $benchRoot '.venv\Scripts\python.exe') -and $process.CommandLine -like '*goldbench.*') {
            Stop-Process -Id $serviceId
        }
    }
}
Write-Output 'Stopped the tracked GoldBench processes. Inflight paid calls require reconciliation on restart.'
