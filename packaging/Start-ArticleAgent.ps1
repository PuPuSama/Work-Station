$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root "runtime"
$Logs = Join-Path $Root "logs"
$BackendExe = Join-Path $Root "backend\ArticleAgentBackend.exe"
$FrontendRoot = Join-Path $Root "frontend"
$NodeExe = Join-Path $FrontendRoot "node.exe"
$FrontendServer = Join-Path $FrontendRoot "server.js"

New-Item -ItemType Directory -Force -Path $Runtime, $Logs | Out-Null

function Test-Url([string]$Url) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
  }
  catch {
    return $false
  }
}

if ((Test-Url "http://127.0.0.1:8000/api/health") -and (Test-Url "http://127.0.0.1:3000")) {
  Start-Process "http://127.0.0.1:3000"
  exit 0
}

$listeners = netstat -ano | Select-String ":8000|:3000" | Where-Object { $_.Line -match "LISTENING" }
if ($listeners) {
  throw "端口 8000 或 3000 已被其他程序占用。请先关闭旧的文章工具，再重新启动。"
}

$env:ARTICLE_AGENT_ROOT = $Root
$env:ARTICLE_AGENT_CONFIG = Join-Path $Root "config.yaml"
$env:ARTICLE_AGENT_BACKEND_HOST = "127.0.0.1"
$env:ARTICLE_AGENT_BACKEND_PORT = "8000"
$env:HOSTNAME = "127.0.0.1"
$env:PORT = "3000"
$env:NODE_ENV = "production"

$backend = Start-Process `
  -FilePath $BackendExe `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Logs "backend.log") `
  -RedirectStandardError (Join-Path $Logs "backend.err.log") `
  -PassThru

$frontend = Start-Process `
  -FilePath $NodeExe `
  -ArgumentList @($FrontendServer) `
  -WorkingDirectory $FrontendRoot `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Logs "frontend.log") `
  -RedirectStandardError (Join-Path $Logs "frontend.err.log") `
  -PassThru

Set-Content -LiteralPath (Join-Path $Runtime "backend.pid") -Value $backend.Id -Encoding ascii
Set-Content -LiteralPath (Join-Path $Runtime "frontend.pid") -Value $frontend.Id -Encoding ascii

$deadline = (Get-Date).AddSeconds(45)
do {
  if ($backend.HasExited -or $frontend.HasExited) {
    break
  }
  if ((Test-Url "http://127.0.0.1:8000/api/health") -and (Test-Url "http://127.0.0.1:3000")) {
    Start-Process "http://127.0.0.1:3000"
    exit 0
  }
  Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

if (-not $backend.HasExited) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
if (-not $frontend.HasExited) { Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue }
throw "文章工具启动失败，请把 logs 文件夹发给技术人员检查。"
