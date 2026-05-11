import json
import os
import asyncio
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from wakeonlan import send_magic_packet


load_dotenv()

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AGENT_TOKEN = os.getenv("HEIMDALL_AGENT_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

if not AGENT_TOKEN:
    raise RuntimeError("Missing HEIMDALL_AGENT_TOKEN in .env")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "active_instance": None,
            "dashboard_message_id": None,
            "last_status_signature": None,
            "instances_dashboard_message_id": None,
            "last_instances_signature": None,
            "empty_since": None,
        }

    with STATE_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state() -> None:
    with STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


config = load_config()
state = load_state()

GUILD_ID = int(config["guild_id"])
AGENT_BASE_URL = config["agent"]["base_url"].rstrip("/")
AGENT_STOP_PATH = "/stop"
DASHBOARD_CHANNEL_ID = int(config["dashboard"]["channel_id"])
DASHBOARD_INTERVAL = int(config["dashboard"].get("update_interval_seconds", 30))
INSTANCES_DASHBOARD_CHANNEL_ID = int(config["instances_dashboard"]["channel_id"])
INSTANCES_DASHBOARD_INTERVAL = int(config["instances_dashboard"].get("update_interval_seconds", 300))

intents = discord.Intents(guilds=True, message_content=True, reactions=True, dm_messages=True)
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


def parse_online_player_count(players_response: str | None) -> int | None:
    if not players_response:
        return None

    match = re.search(r"There are (\d+) of a max of (\d+) players online", players_response)
    if not match:
        return None

    return int(match.group(1))


async def get_owner_user() -> discord.User:
    owner_ids = config["permissions"]["owner_user_ids"]

    if not owner_ids:
        raise RuntimeError("No owner_user_ids configured.")

    owner_id = int(owner_ids[0])
    user = bot.get_user(owner_id)

    if user is None:
        user = await bot.fetch_user(owner_id)

    return user


async def wait_for_owner_reaction(message: discord.Message, timeout_seconds: int) -> bool | None:
    await message.add_reaction("✅")
    await message.add_reaction("❌")

    def check(owner_reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
        return (
            owner_reaction.message.id == message.id
            and user.id in config["permissions"]["owner_user_ids"]
            and str(owner_reaction.emoji) in ["✅", "❌"]
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=timeout_seconds, check=check)
    except asyncio.TimeoutError:
        return None

    if str(reaction.emoji) == "✅":
        return True

    return False


async def stop_current_server_if_needed() -> None:
    status_data = await get_full_status()

    if not status_data.get("agent_online"):
        return

    if not status_data.get("minecraft_online"):
        return

    await agent_request("POST", AGENT_STOP_PATH, {"shutdown_after": False}, timeout_seconds=20)

    for _ in range(24):
        await asyncio.sleep(5)
        status_data = await get_full_status()

        if not status_data.get("minecraft_online"):
            state["active_instance"] = None
            save_state()
            return

    raise RuntimeError("Server did not stop in time.")


async def ensure_agent_online_or_wake() -> bool:
    if await is_agent_online():
        return True

    wol_config = config["wake_on_lan"]

    if not wol_config["enabled"]:
        return False

    send_magic_packet(wol_config["mac_address"])
    return await wait_for_agent()


async def start_requested_instance(instance: str) -> None:
    if not await ensure_agent_online_or_wake():
        raise RuntimeError("Heimdall Agent did not become reachable.")

    status_data = await get_full_status()

    if status_data.get("minecraft_online"):
        current_instance = state.get("active_instance")

        if current_instance == instance:
            return

        await stop_current_server_if_needed()

    await agent_request("POST", "/start", {"instance": instance}, timeout_seconds=20)

    state["active_instance"] = instance
    state["empty_since"] = None
    save_state()

    await asyncio.sleep(3)
    await update_presence(await get_full_status())


async def get_full_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "agent_online": False,
        "minecraft_online": False,
        "rcon_online": False,
        "players_response": None,
        "active_instance": state.get("active_instance"),
        "error": None,
    }

    try:
        status_data = await agent_request("GET", "/status", timeout_seconds=5)
        result.update(status_data)
        result["agent_online"] = True

        if status_data.get("minecraft_online") and status_data.get("active_instance") != state.get("active_instance"):
            state["active_instance"] = status_data.get("active_instance")
            save_state()

        if status_data.get("rcon_online"):
            try:
                players_data = await agent_request("GET", "/players", timeout_seconds=5)
                result["players_response"] = players_data.get("response")
            except Exception as error:
                result["players_response"] = f"Could not query players: {error}"

        if not status_data.get("minecraft_online"):
            result["active_instance"] = None
            if state.get("active_instance") is not None:
                state["active_instance"] = None
                save_state()

    except Exception as error:
        result["error"] = str(error)

    return result


def get_status_signature(status_data: dict[str, Any]) -> str:
    relevant = {
        "agent_online": status_data.get("agent_online"),
        "minecraft_online": status_data.get("minecraft_online"),
        "rcon_online": status_data.get("rcon_online"),
        "players_response": status_data.get("players_response"),
        "active_instance": status_data.get("active_instance"),
        "error": status_data.get("error"),
    }

    return json.dumps(relevant, sort_keys=True)


def get_player_text(players_response: str | None) -> str:
    if not players_response:
        return "Unknown"

    cleaned = players_response.strip()
    return cleaned if cleaned else "No player data"


def make_truncated_code_block(text: str | None, empty_message: str = "No output.") -> str:
    if not text or not text.strip():
        return f"```{empty_message}```"

    cleaned = text.replace("```", "` ` `").strip()
    truncated_notice = "\n... output truncated."
    wrapper_length = len("```") + len("```")
    max_content_chars = 2000 - wrapper_length

    if len(cleaned) > max_content_chars:
        trimmed_length = max_content_chars - len(truncated_notice)
        cleaned = f"{cleaned[:trimmed_length]}{truncated_notice}"

    return f"```{cleaned}```"


def make_status_embed(status_data: dict[str, Any], dashboard: bool = False) -> discord.Embed:
    agent_online = status_data.get("agent_online", False)
    minecraft_online = status_data.get("minecraft_online", False)
    rcon_online = status_data.get("rcon_online", False)
    active_instance = status_data.get("active_instance")

    if not agent_online:
        title = "🔴 Heimdall Agent Offline"
        color = discord.Color.dark_red()
        description = "The Windows host agent is currently unreachable."
    elif minecraft_online:
        title = "🟢 Minecraft Server Online"
        color = discord.Color.green()
        description = "The Minecraft server is currently running."
    else:
        title = "⚫ Minecraft Server Offline"
        color = discord.Color.dark_grey()
        description = "No Minecraft server is currently running."

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    embed.add_field(name="Agent", value="Online" if agent_online else "Offline", inline=True)
    embed.add_field(name="Minecraft", value="Online" if minecraft_online else "Offline", inline=True)
    embed.add_field(name="RCON", value="Online" if rcon_online else "Offline", inline=True)

    embed.add_field(
        name="Active Instance",
        value=f"`{active_instance}`" if active_instance else "None",
        inline=True,
    )

    embed.add_field(
        name="Server Port",
        value=str(status_data.get("server_port", "Unknown")),
        inline=True,
    )

    embed.add_field(
        name="RCON Port",
        value=str(status_data.get("rcon_port", "Unknown")),
        inline=True,
    )

    if minecraft_online:
        embed.add_field(
            name="Players",
            value=f"`{get_player_text(status_data.get('players_response'))}`",
            inline=False,
        )

    if status_data.get("error"):
        embed.add_field(
            name="Error",
            value=f"`{status_data['error']}`",
            inline=False,
        )

    if dashboard:
        embed.set_footer(text="Heimdall Dashboard • Updates only when status changes")
    else:
        embed.set_footer(text="Heimdall Bot")

    return embed


async def update_presence(status_data: dict[str, Any]) -> None:
    agent_online = status_data.get("agent_online", False)
    minecraft_online = status_data.get("minecraft_online", False)
    active_instance = status_data.get("active_instance")

    if not agent_online:
        activity = discord.Activity(type=discord.ActivityType.watching, name="Agent offline")
        await bot.change_presence(status=discord.Status.dnd, activity=activity)
    elif minecraft_online:
        name = active_instance if active_instance else "Minecraft online"
        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
        await bot.change_presence(status=discord.Status.online, activity=activity)
    else:
        activity = discord.Activity(type=discord.ActivityType.watching, name="Minecraft offline")
        await bot.change_presence(status=discord.Status.idle, activity=activity)


async def get_dashboard_message(channel: discord.TextChannel) -> discord.Message | None:
    message_id = state.get("dashboard_message_id")

    if not message_id:
        return None

    if not isinstance(message_id, int | str):
        return None

    try:
        return await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def make_instances_embed(instances_data: dict[str, Any]) -> discord.Embed:
    items = instances_data.get("instances", [])

    embed = discord.Embed(
        title="🎮 Minecraft Server Instances",
        description="Currently configured Minecraft server instances.",
        color=discord.Color.blurple(),
    )

    if not items:
        embed.add_field(name="No instances", value="No Minecraft instances are configured.", inline=False)
        return embed

    active_instance = state.get("active_instance")

    for item in items:
        instance_id = item.get("id", "unknown")
        name = item.get("name", instance_id)

        marker = "🟢 Active" if instance_id == active_instance else "⚫ Available"

        embed.add_field(
            name=f"{marker} — {name}",
            value=f"`{instance_id}`",
            inline=False,
        )

    embed.set_footer(text="Heimdall Instances Dashboard • Updates only when instances change")
    return embed


async def get_instances_dashboard_message(channel: discord.TextChannel) -> discord.Message | None:
    message_id = state.get("instances_dashboard_message_id")

    if not message_id:
        return None

    if not isinstance(message_id, int | str):
        return None

    try:
        return await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


@tasks.loop(seconds=DASHBOARD_INTERVAL)
async def dashboard_loop() -> None:
    await bot.wait_until_ready()

    channel = bot.get_channel(DASHBOARD_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        return

    status_data = await get_full_status()
    signature = get_status_signature(status_data)

    if signature == state.get("last_status_signature"):
        return

    embed = make_status_embed(status_data, dashboard=True)
    message = await get_dashboard_message(channel)

    if message is None:
        message = await channel.send(embed=embed)
        state["dashboard_message_id"] = message.id
    else:
        await message.edit(embed=embed)

    state["last_status_signature"] = signature
    save_state()

    await update_presence(status_data)


@tasks.loop(seconds=INSTANCES_DASHBOARD_INTERVAL)
async def instances_dashboard_loop() -> None:
    await bot.wait_until_ready()

    channel = bot.get_channel(INSTANCES_DASHBOARD_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        return

    try:
        instances_data = await agent_request("GET", "/instances", timeout_seconds=5)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
        instances_data = {"instances": []}

    signature_payload = {
        "instances": instances_data.get("instances", []),
        "active_instance": state.get("active_instance"),
    }

    signature = json.dumps(signature_payload, sort_keys=True)

    if signature == state.get("last_instances_signature"):
        return

    embed = make_instances_embed(instances_data)
    message = await get_instances_dashboard_message(channel)

    if message is None:
        message = await channel.send(embed=embed)
        state["instances_dashboard_message_id"] = message.id
    else:
        await message.edit(embed=embed)

    state["last_instances_signature"] = signature
    save_state()


IDLE_CHECK_INTERVAL = int(config.get("idle_shutdown", {}).get("check_interval_seconds", 60))


@tasks.loop(seconds=IDLE_CHECK_INTERVAL)
async def idle_shutdown_loop() -> None:
    await bot.wait_until_ready()

    idle_config = config.get("idle_shutdown", {})

    if not idle_config.get("enabled", False):
        return

    status_data = await get_full_status()

    if not status_data.get("agent_online") or not status_data.get("minecraft_online") or not status_data.get("rcon_online"):
        state["empty_since"] = None
        save_state()
        return

    player_count = parse_online_player_count(status_data.get("players_response"))

    if player_count is None:
        return

    if player_count > 0:
        state["empty_since"] = None
        save_state()
        return

    now = time.time()

    if state.get("empty_since") is None:
        state["empty_since"] = now
        save_state()
        return

    idle_seconds = int(idle_config.get("idle_seconds", 3600))

    if now - float(state["empty_since"]) >= idle_seconds:
        await agent_request("POST", AGENT_STOP_PATH, {"shutdown_after": False}, timeout_seconds=20)

        state["active_instance"] = None
        state["empty_since"] = None
        save_state()

        owner = await get_owner_user()
        await owner.send("⏱️ Minecraft server was empty for 1 hour and has been stopped automatically.")


@bot.event
async def on_ready() -> None:
    guild = discord.Object(id=GUILD_ID)

    await bot.tree.sync(guild=guild)
    await bot.tree.sync()

    if not dashboard_loop.is_running():
        dashboard_loop.start()

    if not instances_dashboard_loop.is_running():
        instances_dashboard_loop.start()

    if not idle_shutdown_loop.is_running():
        idle_shutdown_loop.start()

    status_data = await get_full_status()
    await update_presence(status_data)

    print(f"Logged in as {bot.user}")


@bot.tree.command(name="status", description="Shows the Minecraft server status.")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        data = await get_full_status()
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

        embed = discord.Embed(
            title="🎮 Available Minecraft Instances",
            color=discord.Color.blurple(),
        )

        for item in items:
            embed.add_field(
                name=item["name"],
                value=f"`{item['id']}`",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

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
        state["active_instance"] = instance
        save_state()

        await interaction.followup.send(f"🟢 {result.get('message', 'Start command sent')}")

        status_data = await get_full_status()
        await update_presence(status_data)

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
        result = await agent_request("POST", AGENT_STOP_PATH, {"shutdown_after": shutdown_after})
        msg = "🔴 Minecraft stop command sent."

        if result.get("shutdown_after"):
            msg += "\n🟡 Host shutdown will follow."

        await interaction.followup.send(msg)

        state["active_instance"] = None
        save_state()

        await asyncio.sleep(3)
        status_data = await get_full_status()
        await update_presence(status_data)

    except Exception as error:
        await interaction.followup.send(f"❌ Could not stop server:\n`{error}`")


@bot.tree.command(name="players", description="Shows currently online Minecraft players.")
async def players(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        data = await agent_request("GET", "/players")
        response = data.get("response", "").strip()
        embed = discord.Embed(
            title="👥 Minecraft Players",
            description=f"`{response}`" if response else "No player data.",
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed)
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
        encoded_message = quote(message)
        await agent_request("POST", f"/say?message={encoded_message}")
        await interaction.followup.send("✅ Message sent.")
    except Exception as error:
        await interaction.followup.send(f"❌ Could not send message:\n`{error}`")


@bot.tree.command(name="rcon", description="Runs an RCON command on the Minecraft server.")
@app_commands.describe(command="Minecraft command to run")
async def rcon(interaction: discord.Interaction, command: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        data = await agent_request("POST", "/rcon", {"command": command}, timeout_seconds=10)
        response = data.get("response", "")

        if not response or not response.strip():
            await interaction.followup.send("Command executed. No response.", ephemeral=True)
            return

        await interaction.followup.send(make_truncated_code_block(response), ephemeral=True)
    except Exception as error:
        await interaction.followup.send(f"Could not run RCON command:\n`{error}`", ephemeral=True)


@bot.tree.command(name="logs", description="Shows recent Minecraft server log lines.")
@app_commands.describe(lines="Number of lines to show, from 1 to 200")
async def logs(interaction: discord.Interaction, lines: int = 50) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("You are not allowed to view server logs.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        requested_lines = max(1, min(lines, 200))
        data = await agent_request("GET", f"/logs?lines={requested_lines}", timeout_seconds=10)
        await interaction.followup.send(
            make_truncated_code_block(data.get("log", ""), empty_message="No log output."),
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(f"Could not load logs:\n`{error}`", ephemeral=True)


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


@bot.tree.command(name="request-start", description="Requests the owner to start a Minecraft instance.")
@app_commands.describe(instance="The instance id")
async def request_start(interaction: discord.Interaction, instance: str) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        owner = await get_owner_user()

        embed = discord.Embed(
            title="🟡 Minecraft Start Request",
            description=f"{interaction.user.mention} requested to start instance `{instance}`.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Requested Instance", value=f"`{instance}`", inline=False)

        message = await owner.send(embed=embed)

        await interaction.followup.send(
            f"📨 Start request for `{instance}` sent to the owner."
        )

        timeout = int(config["requests"].get("owner_accept_timeout_seconds", 300))
        decision = await wait_for_owner_reaction(message, timeout)

        if decision is None:
            await owner.send(f"⌛ Start request for `{instance}` timed out. Nothing happened.")
            return

        if not decision:
            await owner.send(f"❌ Start request for `{instance}` declined.")
            return

        await owner.send(f"✅ Start request for `{instance}` accepted. Processing...")

        await start_requested_instance(instance)

        await owner.send(f"🟢 Instance `{instance}` was started.")

    except Exception as error:
        await interaction.followup.send(f"❌ Could not process start request:\n`{error}`")


@bot.tree.command(name="request-stop", description="Requests the owner to stop the running Minecraft server.")
async def request_stop(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)

    try:
        status_data = await get_full_status()

        if not status_data.get("agent_online"):
            await interaction.followup.send("⚫ Host/Agent is offline. No stop request was sent.")
            return

        if not status_data.get("minecraft_online"):
            await interaction.followup.send("⚫ No Minecraft server is currently running. No stop request was sent.")
            return

        owner = await get_owner_user()
        active_instance = state.get("active_instance") or "unknown"

        embed = discord.Embed(
            title="🔴 Minecraft Stop Request",
            description=f"{interaction.user.mention} requested to stop the running server.",
            color=discord.Color.red(),
        )
        embed.add_field(name="Active Instance", value=f"`{active_instance}`", inline=False)

        message = await owner.send(embed=embed)

        await interaction.followup.send("📨 Stop request sent to the owner.")

        timeout = int(config["requests"].get("owner_accept_timeout_seconds", 300))
        decision = await wait_for_owner_reaction(message, timeout)

        if decision is None:
            await owner.send("⌛ Stop request timed out. Nothing happened.")
            return

        if not decision:
            await owner.send("❌ Stop request declined.")
            return

        await owner.send("✅ Stop request accepted. Stopping server...")

        await agent_request("POST", AGENT_STOP_PATH, {"shutdown_after": False}, timeout_seconds=20)

        state["active_instance"] = None
        state["empty_since"] = None
        save_state()

        await owner.send("🔴 Minecraft stop command sent.")

    except Exception as error:
        await interaction.followup.send(f"❌ Could not process stop request:\n`{error}`")


bot.run(DISCORD_TOKEN)
