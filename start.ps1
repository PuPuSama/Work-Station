$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Data = Join-Path $Root "data"

New-Item -ItemType Directory -Force -Path $Data | Out-Null

Start-Process `
  -FilePath (Join-Path $Backend ".venv\Scripts\python.exe") `
  -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000") `
  -WorkingDirectory $Backend `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Data "backend.log") `
  -RedirectStandardError (Join-Path $Data "backend.err.log")

Start-Process `
  -FilePath "npm.cmd" `
  -ArgumentList @("run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000") `
  -WorkingDirectory $Frontend `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Data "frontend.log") `
  -RedirectStandardError (Join-Path $Data "frontend.err.log")

Write-Host "Backend:  http://127.0.0.1:8000"
Write-Host "Frontend: http://127.0.0.1:3000"
Write-Host "Logs:     $Data"
