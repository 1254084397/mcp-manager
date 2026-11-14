param([string]$Name = "MCPManager")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$agents = Join-Path $PSScriptRoot "mcpmanager\agents"
$catalog = Join-Path $PSScriptRoot "mcpmanager\catalog"
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name $Name `
  --distpath dist `
  --workpath build `
  --specpath build `
  --add-data "$agents;mcpmanager/agents" `
  --add-data "$catalog;mcpmanager/catalog" `
  mcp-manager.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Write-Host "Built: $PSScriptRoot\dist\$Name.exe"
