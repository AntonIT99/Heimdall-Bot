import json
import os
import asyncio
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from wakeonlan import send_magic_packet


load_dotenv()

CONFIG_PATH = Path("config.json")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AGENT_TOKEN = os.getenv("HEIMDALL_AGENT_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

if not AGENT_TOKEN:
    raise RuntimeError("Missing HEIMDALL_AGENT_TOKEN in .env")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


config = load_config()

GUILD_ID = int(config["guild_id"])
AGENT_BASE_URL = config["agent"]["base_url"].rstrip("/")

intents = discord.Intents(guilds=True)

bot = commands.Bot(command_prefix="!", intents=intents)


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in config["permissions"]["owner_user_ids"]


def is_admin(interaction: discord.Interaction) -> bool:
    if is_owner(interaction):
        return True

    if not isinstance(interaction.user, discord.Member):
        return False

    admin_role_ids = set(config["permissions"]["admin_role_ids"])
    return any(role.id in admin_role_ids for role in interaction.user.roles)


async def agent_request(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    url = f"{AGENT_BASE_URL}{path}"

    headers = {
        "Authorization": f"Bearer {AGENT_TOKEN}",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.request(
            method,
            url,
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                data = {"raw": await response.text()}

            if response.status >= 400:
                raise RuntimeError(f"Agent error {response.status}: {data}")

            return data


async def is_agent_online() -> bool:
    try:
        await agent_request("GET", "/", timeout_seconds=3)
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
        return False


async def wait_for_agent() -> bool:
    attempts = int(config["startup"]["agent_retry_attempts"])
    delay = int(config["startup"]["agent_retry_seconds"])

    for _ in range(attempts):
        if await is_agent_online():
            return True
        await asyncio.sleep(delay)

    return False


def make_status_embed(server_status: dict[str, Any]) -> discord.Embed:
    minecraft_online = server_status.get("minecraft_online", False)
    rcon_online = server_status.get("rcon_online", False)

    if minecraft_online:
        title = "🟢 Minecraft Server Online"
        color = discord.Color.green()
    else:
        title = "🔴 Minecraft Server Offline"
        color = discord.Color.red()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Minecraft", value="Online" if minecraft_online else "Offline", inline=True)
    embed.add_field(name="RCON", value="Online" if rcon_online else "Offline", inline=True)
    embed.add_field(name="Server Port", value=str(server_status.get("server_port")), inline=True)
    embed.add_field(name="RCON Port", value=str(server_status.get("rcon_port")), inline=True)

    return embed


@bot.event
async def on_ready() -> None:
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    print(f"Logged in as {bot.user}")


@bot.tree.command(name="status", description="Shows the Minecraft server status.")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        data = await agent_request("GET", "/status")
        await interaction.followup.send(embed=make_status_embed(data))
    except Exception as error:
        await interaction.followup.send(f"❌ Could not reach Heimdall Agent:\n`{error}`")


@bot.tree.command(name="instances", description="Lists available Minecraft server instances.")
async def instances(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    try:
        data = await agent_request("GET", "/instances")
        items = data.get("instances", [])

        if not items:
            await interaction.followup.send("No instances configured.")
            return

        text = "\n".join(f"- `{item['id']}` — {item['name']}" for item in items)
        await interaction.followup.send(f"🎮 Available instances:\n{text}")

    except Exception as error:
        await interaction.followup.send(f"❌ Could not load instances:\n`{error}`")


@bot.tree.command(name="wake-host", description="Wakes the Minecraft host PC using Wake-on-LAN.")
async def wake_host(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    wol_config = config["wake_on_lan"]

    if not wol_config["enabled"]:
        await interaction.followup.send("Wake-on-LAN is disabled in config.")
        return

    mac = wol_config["mac_address"]
    send_magic_packet(mac)

    await interaction.followup.send(f"🟡 Wake-on-LAN packet sent to `{mac}`.")


@bot.tree.command(name="start-server", description="Starts a Minecraft server instance.")
@app_commands.describe(instance="The instance id")
async def start_server(interaction: discord.Interaction, instance: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You are not allowed to start servers.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    if not await is_agent_online():
        wol_config = config["wake_on_lan"]

        if wol_config["enabled"]:
            send_magic_packet(wol_config["mac_address"])
            await interaction.followup.send("🟡 Host seems offline. Wake-on-LAN packet sent. Waiting for Heimdall Agent...")

            if not await wait_for_agent():
                await interaction.followup.send("❌ Heimdall Agent did not become reachable in time.")
                return
        else:
            await interaction.followup.send("❌ Heimdall Agent is offline and Wake-on-LAN is disabled.")
            return

    try:
        result = await agent_request("POST", "/start", {"instance": instance})
        await interaction.followup.send(f"🟢 {result.get('message', 'Start command sent')}")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not start server:\n`{error}`")


@bot.tree.command(name="stop-server", description="Stops the running Minecraft server cleanly.")
@app_commands.describe(shutdown_after="Also shut down the Windows host after stopping Minecraft")
async def stop_server(interaction: discord.Interaction, shutdown_after: bool = False) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You are not allowed to stop servers.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        result = await agent_request("POST", "/stop", {"shutdown_after": shutdown_after})
        msg = "🔴 Minecraft stop command sent."

        if result.get("shutdown_after"):
            msg += "\n🟡 Host shutdown will follow."

        await interaction.followup.send(msg)

    except Exception as error:
        await interaction.followup.send(f"❌ Could not stop server:\n`{error}`")


@bot.tree.command(name="players", description="Shows currently online Minecraft players.")
async def players(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        data = await agent_request("GET", "/players")
        await interaction.followup.send(f"👥 `{data.get('response', '').strip()}`")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not query players:\n`{error}`")


@bot.tree.command(name="say", description="Sends a message to the Minecraft server chat.")
@app_commands.describe(message="Message to send")
async def say(interaction: discord.Interaction, message: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await agent_request("POST", f"/say?message={message}")
        await interaction.followup.send("✅ Message sent.")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not send message:\n`{error}`")


@bot.tree.command(name="shutdown-host", description="Shuts down the Windows Minecraft host.")
async def shutdown_host(interaction: discord.Interaction) -> None:
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Only the owner can shut down the host.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        data = await agent_request("POST", "/shutdown-host")
        await interaction.followup.send(f"🟡 {data.get('message', 'Shutdown scheduled')}")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not shut down host:\n`{error}`")


@bot.tree.command(name="cancel-shutdown", description="Cancels a scheduled host shutdown.")
async def cancel_shutdown(interaction: discord.Interaction) -> None:
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Only the owner can cancel host shutdown.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        data = await agent_request("POST", "/cancel-shutdown")
        await interaction.followup.send(f"✅ {data.get('message', 'Shutdown cancelled')}")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not cancel shutdown:\n`{error}`")


bot.run(DISCORD_TOKEN)
