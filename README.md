# Heimdall Bot

Discord bot frontend for Heimdall Agent.

Heimdall Bot runs on a Raspberry Pi and controls a Windows-based Minecraft host through Heimdall Agent.

## Features

- Discord slash commands
- Minecraft server status monitoring
- Start configured Minecraft instances
- Stop Minecraft cleanly through Heimdall Agent
- Query online players
- Send Minecraft chat messages through RCON
- Wake-on-LAN support
- Optional Windows host shutdown
- Token-based authentication with Heimdall Agent
- Automatic host wakeup before server start
- Linux systemd integration
- GitHub-safe configuration structure

## Architecture

```text
Discord
  ↓
Heimdall Bot on Raspberry Pi
  ↓
LAN HTTP API
  ↓
Heimdall Agent on Windows
  ↓
Minecraft Server
```

## Components

### Heimdall Bot

Runs on a Raspberry Pi and provides:

- Discord slash commands
- Permission handling
- Wake-on-LAN support
- Communication with Heimdall Agent
- Status embeds and notifications

### Heimdall Agent

Runs as a Windows background service and provides:

- Minecraft instance orchestration
- RCON integration
- Windows host control
- HTTP API for the bot
- Safe server shutdown handling

## Security

Sensitive files are excluded from GitHub:

- `.env`
- `config.json`
- `deploy.local.json`

Only template/example files are committed.

## Project Structure

```text
Heimdall-Bot/
├── bot.py
├── deploy.ps1
├── deploy.local.json          # local only, ignored by git
├── config.json                # local only, ignored by git
├── config.example.json
├── .env                       # local only, ignored by git
├── .env.example
├── heimdall-bot.service
├── requirements.txt
├── README.md
└── .gitignore
```

## Setup

### Clone repository

```bash
git clone <your-repository>
cd Heimdall-Bot
```

### Create virtual environment

```bash
python3 -m venv .venv
```

### Activate virtual environment

Linux:

```bash
source .venv/bin/activate
```

Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### Install dependencies

```bash
pip install -r requirements.txt
```

## Configuration

### Create local configuration files

```bash
cp config.example.json config.json
cp .env.example .env
```

### Configure `.env`

```env
DISCORD_TOKEN=your_discord_bot_token
HEIMDALL_AGENT_TOKEN=your_agent_token
```

### Configure `config.json`

Configure:

- Discord guild ID
- Owner user IDs
- Admin role IDs
- Heimdall Agent URL
- Wake-on-LAN MAC address

## Running Locally

```bash
python bot.py
```

## Raspberry Pi Deployment

### Deployment target

```text
/home/pi/heimdall-bot
```

### Deploy from Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

The deploy script:

- uploads the bot
- uploads local configuration
- updates dependencies
- restarts the systemd service
- automatically enables the service on boot

## systemd Integration

The bot runs as a native Linux service:

```bash
sudo systemctl status heimdall-bot
```

### Useful commands

#### Restart bot

```bash
sudo systemctl restart heimdall-bot
```

#### Stop bot

```bash
sudo systemctl stop heimdall-bot
```

#### View logs

```bash
journalctl -u heimdall-bot -f
```

## Discord Commands

### Public Commands

- `/status`
- `/instances`
- `/players`

### Admin Commands

- `/wake-host`
- `/start-server`
- `/stop-server`
- `/say`

### Owner Commands

- `/shutdown-host`
- `/cancel-shutdown`

## Infrastructure Overview

### Raspberry Pi

- Heimdall Bot
- Discord integration
- Wake-on-LAN sender
- Linux service management

### Windows Host

- Heimdall Agent
- Minecraft server execution
- RCON management
- Windows shutdown control
