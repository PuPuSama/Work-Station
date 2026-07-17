$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$BuildRoot = Join-Path $Root "packaging\build"
$PackageRoot = Join-Path $Root "dist\ArticleAgent-Portable"
$ZipPath = Join-Path $Root "dist\ArticleAgent-Portable.zip"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"

function Assert-WorkspacePath([string]$Path) {
  $full = [System.IO.Path]::GetFullPath($Path)
  $rootPrefix = $Root.TrimEnd('\') + '\'
  if (-not $full.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to modify a path outside the repository: $full"
  }
}

function Copy-Utf8BomText([string]$Source, [string]$Destination) {
  $content = [System.IO.File]::ReadAllText($Source, [System.Text.Encoding]::UTF8)
  $encoding = [System.Text.UTF8Encoding]::new($true)
  [System.IO.File]::WriteAllText($Destination, $content, $encoding)
}

foreach ($target in @($BuildRoot, $PackageRoot, $ZipPath)) {
  Assert-WorkspacePath $target
  if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $PackageRoot | Out-Null

Write-Host "[1/4] Building the production frontend..."
Push-Location $Frontend
try {
  npm.cmd run build
}
finally {
  Pop-Location
}

Write-Host "[2/4] Building the standalone backend..."
$PyInstallerDist = Join-Path $BuildRoot "pyinstaller-dist"
$PyInstallerWork = Join-Path $BuildRoot "pyinstaller-work"
$PyInstallerSpec = Join-Path $BuildRoot "pyinstaller-spec"
New-Item -ItemType Directory -Force -Path $PyInstallerDist, $PyInstallerWork, $PyInstallerSpec | Out-Null

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name ArticleAgentBackend `
  --paths $Backend `
  --additional-hooks-dir (Join-Path $PSScriptRoot "pyinstaller-hooks") `
  --add-data "$(Join-Path $Backend 'prompts');prompts" `
  --distpath $PyInstallerDist `
  --workpath $PyInstallerWork `
  --specpath $PyInstallerSpec `
  (Join-Path $Backend "portable_server.py")
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host "[3/4] Assembling the portable directory..."
$BackendDestination = Join-Path $PackageRoot "backend"
$FrontendDestination = Join-Path $PackageRoot "frontend"
New-Item -ItemType Directory -Force -Path $BackendDestination, $FrontendDestination | Out-Null

Copy-Item -Path (Join-Path $PyInstallerDist "ArticleAgentBackend\*") -Destination $BackendDestination -Recurse -Force

$Standalone = Join-Path $Frontend ".next\standalone"
Copy-Item -Path (Join-Path $Standalone "*") -Destination $FrontendDestination -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $FrontendDestination ".next") | Out-Null
Copy-Item -LiteralPath (Join-Path $Frontend ".next\static") -Destination (Join-Path $FrontendDestination ".next\static") -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $Frontend "public")) {
  Copy-Item -LiteralPath (Join-Path $Frontend "public") -Destination (Join-Path $FrontendDestination "public") -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $Frontend "package-lock.json") -Destination (Join-Path $FrontendDestination "package-lock.json") -Force
Push-Location $FrontendDestination
try {
  npm.cmd ci --omit=dev --ignore-scripts
  if ($LASTEXITCODE -ne 0) {
    throw "npm production dependency install failed with exit code $LASTEXITCODE."
  }
}
finally {
  Pop-Location
}
Copy-Item -LiteralPath "C:\Program Files\nodejs\node.exe" -Destination (Join-Path $FrontendDestination "node.exe") -Force

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "portable-config.yaml") -Destination (Join-Path $PackageRoot "config.yaml") -Force
Copy-Utf8BomText `
  (Join-Path $PSScriptRoot "Start-ArticleAgent.ps1") `
  (Join-Path $PackageRoot "Start-ArticleAgent.ps1")
Copy-Utf8BomText `
  (Join-Path $PSScriptRoot "Stop-ArticleAgent.ps1") `
  (Join-Path $PackageRoot "Stop-ArticleAgent.ps1")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "start.cmd") -Destination $PackageRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "stop.cmd") -Destination $PackageRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "OPERATIONS-README.txt") -Destination (Join-Path $PackageRoot "使用说明.txt") -Force
$EnvironmentLines = @()
foreach ($source in @((Join-Path $Root ".env"), (Join-Path $Backend ".env"))) {
  if (Test-Path -LiteralPath $source) {
    $EnvironmentLines += Get-Content -LiteralPath $source -Encoding UTF8
  }
}
Set-Content -LiteralPath (Join-Path $PackageRoot ".env") -Value $EnvironmentLines -Encoding utf8

$PromptSource = "D:\article\降ai提示词-未测试效果版.txt"
if (-not (Test-Path -LiteralPath $PromptSource)) {
  throw "Humanization prompt not found: $PromptSource"
}
New-Item -ItemType Directory -Force -Path `
  (Join-Path $PackageRoot "prompts"), `
  (Join-Path $PackageRoot "data\topic-library"), `
  (Join-Path $PackageRoot "data\knowledge"), `
  (Join-Path $PackageRoot "data\workspace"), `
  (Join-Path $PackageRoot "data\state"), `
  (Join-Path $PackageRoot "logs"), `
  (Join-Path $PackageRoot "runtime") | Out-Null
Copy-Item -LiteralPath $PromptSource -Destination (Join-Path $PackageRoot "prompts\humanize.txt") -Force

Set-Content -LiteralPath (Join-Path $PackageRoot "data\state\tasks.json") -Value "[]" -Encoding utf8
Set-Content -LiteralPath (Join-Path $PackageRoot "data\topic-library\请将Excel话题文件放到这里.txt") -Value "每个官网对应一个 Excel 话题文件。放入后，在首页点击同步话题库。" -Encoding utf8
Set-Content -LiteralPath (Join-Path $PackageRoot "data\knowledge\请按官网域名建立资料文件夹.txt") -Value "示例：data\knowledge\www.example.com\产品资料.docx" -Encoding utf8
Set-Content -LiteralPath (Join-Path $PackageRoot "data\workspace\此处自动生成文章项目.txt") -Value "文章、图片、Word 和交付包会自动生成在这个目录中。" -Encoding utf8

Write-Host "[4/4] Creating ZIP archive..."
Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -CompressionLevel Optimal

$packageSize = [math]::Round((Get-ChildItem -LiteralPath $PackageRoot -File -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
$zipSize = [math]::Round((Get-Item -LiteralPath $ZipPath).Length / 1MB, 1)
Write-Host "Portable directory: $PackageRoot ($packageSize MB)"
Write-Host "ZIP package:        $ZipPath ($zipSize MB)"
