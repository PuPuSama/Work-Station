$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Data = Join-Path $Root "data"

New-Item -ItemType Directory -Force -Path $Data | Out-Null

function Get-ListeningProcessId([int]$Port) {
  $match = netstat -ano | Select-String "LISTENING\s+(\d+)$" | Where-Object {
    $_.Line -match ":$Port\s+"
  } | Select-Object -First 1
  if ($match -and $match.Line -match "LISTENING\s+(\d+)$") {
    return [int]$Matches[1]
  }
  return $null
}

$Conflicts = @()
foreach ($Port in @(8000, 3000)) {
  $ProcessId = Get-ListeningProcessId $Port
  if ($ProcessId) {
    $Conflicts += "Port $Port is already used by process $ProcessId"
  }
}
if ($Conflicts.Count -gt 0) {
  throw (($Conflicts -join "; ") + ". Stop the old project process, then run start.ps1 again.")
}

$BackendProcess = Start-Process `
  -FilePath (Join-Path $Backend ".venv\Scripts\python.exe") `
  -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000") `
  -WorkingDirectory $Backend `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Data "backend.log") `
  -RedirectStandardError (Join-Path $Data "backend.err.log") `
  -PassThru

$FrontendProcess = Start-Process `
  -FilePath "npm.cmd" `
  -ArgumentList @("run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000") `
  -WorkingDirectory $Frontend `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Data "frontend.log") `
  -RedirectStandardError (Join-Path $Data "frontend.err.log") `
  -PassThru

Start-Sleep -Seconds 2
if ($BackendProcess.HasExited -or $FrontendProcess.HasExited) {
  if (-not $BackendProcess.HasExited) {
    Stop-Process -Id $BackendProcess.Id -Force -ErrorAction SilentlyContinue
  }
  if (-not $FrontendProcess.HasExited) {
    Stop-Process -Id $FrontendProcess.Id -Force -ErrorAction SilentlyContinue
  }
  throw "Project startup failed. Check backend.err.log and frontend.err.log under $Data."
}

Write-Host "Backend:  http://127.0.0.1:8000"
Write-Host "Frontend: http://127.0.0.1:3000"
Write-Host "Logs:     $Data"
