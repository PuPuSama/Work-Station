$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root "runtime"

foreach ($name in @("backend", "frontend")) {
  $pidFile = Join-Path $Runtime "$name.pid"
  if (-not (Test-Path -LiteralPath $pidFile)) { continue }
  $processId = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
  if ($processId) {
    Stop-Process -Id ([int]$processId) -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Write-Host "文章工具已停止。"
