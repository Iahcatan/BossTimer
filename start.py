import asyncio
import os
import traceback
from datetime import datetime, timezone

import discord
import bot as bot_module

# ============================================================
# SKYNET STARTUP / DISCORD COMMAND BOOTSTRAP
# ============================================================
# start.py owns command synchronization only.
# bot.py remains the owner of Firebase, Boss Timer, /kill,
# /setvoice, /status, TTS, Voice, Dashboard and background tasks.

COMMAND_SYNC_DELAY = float(os.environ.get("COMMAND_SYNC_DELAY", "1.0"))

# ------------------------------------------------------------
# Preserve persistent bot setup without doing any Gateway work.
# discord.py runs setup_hook BEFORE READY, so never wait_until_ready()
# or sync guild commands from setup_hook.
# ------------------------------------------------------------
async def patched_setup_hook():
    try:
        if hasattr(bot_module, "QuickActionsView"):
            bot_module.bot.add_view(bot_module.QuickActionsView())
            print("✅ QuickActionsView registered")
    except Exception as exc:
        print(f"⚠️ QuickActionsView registration skipped: {exc!r}")

bot_module.bot.setup_hook = patched_setup_hook

# ------------------------------------------------------------
# Protect startup from malformed legacy custom_bosses data.
# ------------------------------------------------------------
_original_load_custom_bosses = getattr(bot_module, "load_custom_bosses", None)
if _original_load_custom_bosses is not None:
    async def safe_load_custom_bosses():
        try:
            await _original_load_custom_bosses()
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            print(f"⚠️ custom_bosses invalid/legacy data skipped: {type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"⚠️ load_custom_bosses failed safely: {exc!r}")
            traceback.print_exc()
    bot_module.load_custom_bosses = safe_load_custom_bosses

# ------------------------------------------------------------
# Fast /status runtime patch.
#
# bot.py already contains /status. We replace only its callback at
# runtime so the first Discord response is a direct ACK rather than
# waiting on Firebase, locks, voice or any other work.
# ------------------------------------------------------------
async def _fast_status_callback(interaction: discord.Interaction):
    try:
        latency_ms = round(bot_module.bot.latency * 1000, 1)
        guild_count = len(bot_module.bot.guilds)
        voice_count = sum(
            1 for guild in bot_module.bot.guilds
            if guild.voice_client and guild.voice_client.is_connected()
        )
        firebase_state = (
            "🟢 initialized"
            if getattr(bot_module.firebase_admin, "_apps", {})
            else "🔴 not initialized"
        )

        embed = discord.Embed(
            title="🟢 SKYNET Status",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🤖 Bot", value=str(bot_module.bot.user), inline=False)
        embed.add_field(name="📡 Gateway", value=f"🟢 Online ({latency_ms} ms)", inline=True)
        embed.add_field(name="🏠 Guilds", value=str(guild_count), inline=True)
        embed.add_field(name="🔊 Voice", value=str(voice_count), inline=True)
        embed.add_field(name="🔥 Firebase", value=firebase_state, inline=True)
        embed.add_field(
            name="📋 Local Commands",
            value=str(len(bot_module.bot.tree.get_commands())),
            inline=True,
        )
        embed.add_field(
            name="⏰ Thailand",
            value=datetime.now(bot_module.TZ_THAI).strftime("%H:%M:%S"),
            inline=True,
        )
        embed.set_footer(text="SKYNET • /status diagnostic")

        # IMPORTANT: direct initial response. No defer/followup dependency.
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(
            "✅ /status handled | "
            f"user={interaction.user} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"latency={latency_ms}ms"
        )
    except Exception as exc:
        print(f"❌ /status handler error: {exc!r}")
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ /status error: `{type(exc).__name__}: {exc}`",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ /status error: `{type(exc).__name__}: {exc}`",
                    ephemeral=True,
                )
        except Exception:
            pass

status_command = bot_module.bot.tree.get_command("status")
if status_command is not None:
    status_command.callback = _fast_status_callback
    print("🩹 /status fast-response patch installed")
else:
    print("⚠️ /status command was not found during startup patch")

_sync_lock = asyncio.Lock()
_sync_complete = False

async def sync_commands_once():
    global _sync_complete
    if _sync_complete:
        return

    async with _sync_lock:
        if _sync_complete:
            return

        await bot_module.bot.wait_until_ready()
        if COMMAND_SYNC_DELAY > 0:
            await asyncio.sleep(COMMAND_SYNC_DELAY)

        print("=" * 60)
        print("🔄 SKYNET DISCORD COMMAND SYNC")
        print(f"🤖 Bot: {bot_module.bot.user}")
        print(f"🏠 Guilds: {len(bot_module.bot.guilds)}")

        local_commands = bot_module.bot.tree.get_commands()
        command_names = sorted(command.qualified_name for command in local_commands)
        print(f"📋 Local commands: {len(command_names)}")
        print("📋 " + ", ".join(command_names))

        required = {"status", "kill", "setvoice"}
        missing_local = sorted(required - set(command_names))
        if missing_local:
            print("❌ Required commands missing locally: " + ", ".join(missing_local))

        guilds = list(bot_module.bot.guilds)
        configured_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if configured_guild_id.isdigit():
            wanted_id = int(configured_guild_id)
            guilds = [g for g in guilds if g.id == wanted_id]
            if not guilds:
                print(f"⚠️ DISCORD_GUILD_ID={wanted_id} not found in Gateway guild cache")

        if not guilds:
            print("❌ ไม่มี Guild สำหรับ sync คำสั่ง")
            return

        successful = 0
        for guild in guilds:
            try:
                bot_module.bot.tree.clear_commands(guild=guild)
                bot_module.bot.tree.copy_global_to(guild=guild)
                synced = await bot_module.bot.tree.sync(guild=guild)
                remote_names = sorted(command.qualified_name for command in synced)
                print(
                    f"✅ Guild Sync: {guild.name} ({guild.id}) -> "
                    f"{len(remote_names)} commands"
                )
                print("🔎 Remote Guild Commands: " + ", ".join(remote_names))

                missing_remote = sorted(required - set(remote_names))
                if missing_remote:
                    print(
                        "❌ Required commands missing on "
                        + guild.name
                        + ": "
                        + ", ".join(missing_remote)
                    )
                else:
                    print("🟢 Required commands verified: /status /kill /setvoice")
                successful += 1
            except Exception as exc:
                print(f"❌ Guild Sync failed: {guild.name} ({guild.id}): {exc!r}")
                traceback.print_exc()

        if successful == len(guilds):
            _sync_complete = True
            print(f"✅ DISCORD GUILD COMMAND SYNC COMPLETE ({successful}/{len(guilds)} guilds)")
        else:
            print(f"⚠️ DISCORD GUILD COMMAND SYNC PARTIAL ({successful}/{len(guilds)} guilds)")
        print("=" * 60)

bot_module.sync_commands_once = sync_commands_once

@bot_module.bot.listen("on_interaction")
async def interaction_diagnostic(interaction: discord.Interaction):
    try:
        if interaction.type != discord.InteractionType.application_command:
            return
        command_name = None
        try:
            if interaction.command is not None:
                command_name = interaction.command.qualified_name
        except Exception:
            pass
        if not command_name:
            try:
                command_name = interaction.data.get("name")
            except Exception:
                command_name = "unknown"
        print(
            "📥 INTERACTION RECEIVED | "
            f"command={command_name!r} user={interaction.user} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"channel={getattr(interaction, 'channel_id', None)}"
        )
    except Exception as exc:
        print(f"⚠️ interaction diagnostic failed: {exc!r}")

@bot_module.bot.listen("on_ready")
async def startup_command_sync():
    print("🟢 on_ready received by start.py")
    try:
        await sync_commands_once()
    except Exception as exc:
        print(f"❌ startup command sync failed: {exc!r}")
        traceback.print_exc()

async def main():
    print("=" * 60)
    print("🚀 SKYNET STARTING")
    print("=" * 60)
    print("🌐 Starting web server...")
    bot_module.keep_alive()

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ไม่พบ DISCORD_TOKEN ใน Environment Variables")

    print("🔑 พบ DISCORD_TOKEN")
    print("🔌 กำลังเริ่ม Discord Bot...")
    print("🔌 กำลังเชื่อมต่อ Discord Gateway...")
    try:
        await bot_module.run_bot_with_backoff(token)
    except KeyboardInterrupt:
        print("🛑 Bot stopped")
    except Exception as exc:
        print(f"❌ Discord Bot หยุดทำงาน: {exc!r}")
        traceback.print_exc()
        raise

if __name__ == "__main__":
    asyncio.run(main())
