$ErrorActionPreference = "Stop"

$ConfigPath = Join-Path $PSScriptRoot "deploy.local.json"

if (!(Test-Path $ConfigPath)) {
    throw "Missing deploy.local.json. Create it from deploy.local.json.example."
}

$DeployConfig = Get-Content $ConfigPath -Raw | ConvertFrom-Json

$RemoteUser = $DeployConfig.remoteUser
$RemoteHost = $DeployConfig.remoteHost
$RemotePath = $DeployConfig.remotePath
$ServiceName = $DeployConfig.serviceName

$Remote = "${RemoteUser}@${RemoteHost}"

Write-Host "Deploying Heimdall-Bot to ${Remote}:${RemotePath}"

ssh $Remote "mkdir -p '$RemotePath'"

scp `
  bot.py `
  requirements.txt `
  config.json `
  .env `
  README.md `
  config.json `
  .env `
  "${Remote}:${RemotePath}/"

ssh $Remote "cd '$RemotePath' && if [ ! -d .venv ]; then python3 -m venv .venv; fi"
ssh $Remote "cd '$RemotePath' && . .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"
ssh $Remote "sudo cp '$RemotePath/heimdall-bot.service' '/etc/systemd/system/$ServiceName.service'"
ssh $Remote "sudo systemctl restart '$ServiceName'"
ssh $Remote "sudo systemctl status '$ServiceName' --no-pager"

Write-Host "Done."