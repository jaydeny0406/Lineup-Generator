param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = "C:\Users\jayde\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

$listenerLines = netstat -ano -p tcp |
    Select-String -Pattern "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"

$listenerIds = foreach ($line in $listenerLines) {
    if ($line.Matches.Count -gt 0) {
        [int]$line.Matches[0].Groups[1].Value
    }
}

foreach ($processId in ($listenerIds | Sort-Object -Unique)) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }
    if ($process.ProcessName -notlike "python*") {
        throw "Port $Port is used by non-Python process $($process.ProcessName) (PID $processId)."
    }
    Stop-Process -Id $processId -Force
}

if (Test-Path -LiteralPath $bundledPython) {
    $python = $bundledPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (Get-Command py).Source
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
} else {
    throw "Python was not found."
}

Set-Location -LiteralPath $appDirectory
Write-Host "Starting the current Track Lineup Optimizer at http://127.0.0.1:$Port"
& $python app.py $Port
