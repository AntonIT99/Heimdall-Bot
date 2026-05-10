# Heimdall Bot

Discord bot frontend for Heimdall Agent.

Heimdall Bot runs on a Raspberry Pi and controls a Windows-based Minecraft host through Heimdall Agent.

## Features

- Discord slash commands
- Minecraft server status
- Start configured Minecraft instances
- Stop Minecraft cleanly through Heimdall Agent
- Query players
- Send chat messages
- Wake-on-LAN support
- Optional host shutdown
- Token-based access to Heimdall Agent

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