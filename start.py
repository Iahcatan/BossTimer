import asyncio
import discord
import os
import sys
import traceback
from discord import app_commands

# Render normally runs Python with stdout connected to a log pipe.
# Reconfigure BEFORE importing bot.py so even Firebase/import/on_ready logs
# are visible immediately in Render instead of waiting for the buffer to fill.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import bot as bot_module

SKYNET_RUNTIME_ROLE = os.environ.get("SKYNET_RUNTIME_ROLE", "web").strip().lower()
if SKYNET_RUNTIME_ROLE not in {"web", "bot"}:
    raise RuntimeError("SKYNET_RUNTIME_ROLE must be exactly 'web' or 'bot'")

# ============================================================
# SKYNET STARTUP / DISCORD COMMAND BOOTSTRAP
# ============================================================
# start.py owns command synchronization only.
# bot.py remains the owner of Firebase, Boss Timer, /kill,
# /setvoice, /status, TTS, Voice, Dashboard and background tasks.

COMMAND_SYNC_DELAY = float(os.environ.get("COMMAND_SYNC_DELAY", "1.0"))


def log(message: str):
    print(message, flush=True)


# ------------------------------------------------------------
# Preserve persistent bot setup without doing any Gateway work.
# discord.py runs setup_hook BEFORE READY, so never wait_until_ready()
# or sync guild commands from setup_hook.
# ------------------------------------------------------------
async def patched_setup_hook():
    try:
        if hasattr(bot_module, "QuickActionsView"):
            bot_module.bot.add_view(bot_module.QuickActionsView())
            log("✅ QuickActionsView registered")
    except Exception as exc:
        log(f"⚠️ QuickActionsView registration skipped: {exc!r}")


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
            log(
                "⚠️ custom_bosses invalid/legacy data skipped: "
                f"{type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            log(f"⚠️ load_custom_bosses failed safely: {exc!r}")
            traceback.print_exc()

    bot_module.load_custom_bosses = safe_load_custom_bosses

# ------------------------------------------------------------
# /status is defined in bot.py. Never replace Command.callback.
# discord.py 2.7 exposes Command.callback as read-only.
# ------------------------------------------------------------
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

        log("=" * 60)
        log("🔄 SKYNET DISCORD COMMAND SYNC | V142 verify-first Guild Commands (REST diagnostics + voice/attendance safeguards)")
        log(f"🤖 Bot: {bot_module.bot.user}")
        log(f"🆔 Bot ID: {getattr(bot_module.bot.user, 'id', None)}")
        log(f"🏠 Guilds: {len(bot_module.bot.guilds)}")

        local_commands = bot_module.bot.tree.get_commands()
        command_names = sorted(command.qualified_name for command in local_commands)
        log(f"📋 Local commands: {len(command_names)}")
        log("📋 " + ", ".join(command_names))

        required = {"status", "kill", "setvoice"}
        missing_local = sorted(required - set(command_names))
        if missing_local:
            log("❌ Required commands missing locally: " + ", ".join(missing_local))

        guilds = list(bot_module.bot.guilds)
        configured_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if configured_guild_id.isdigit():
            wanted_id = int(configured_guild_id)
            guilds = [g for g in guilds if g.id == wanted_id]
            if not guilds:
                log(f"⚠️ DISCORD_GUILD_ID={wanted_id} not found in Gateway guild cache")

        if not guilds:
            log("❌ ไม่มี Guild สำหรับ sync คำสั่ง")
            return

        # V137: do not clear/rewrite Global Commands on every startup. The current
        # deployment is Guild-authoritative and the previous cleanup already removed
        # stale global commands. Repeating that bulk REST mutation on every Render
        # restart creates unnecessary Discord API traffic.
        log("🛡️ Global command cleanup skipped | Guild Commands are authoritative")

        successful = 0
        command_sync_performed = False
        local_name_set = set(command_names)
        for guild in guilds:
            try:
                await bot_module.wait_for_discord_rest_startup_gate(context="startup:command-verify")
                try:
                    remote_commands = await bot_module.bot.tree.fetch_commands(guild=guild)
                    remote_names = sorted(
                        getattr(command, "qualified_name", getattr(command, "name", ""))
                        for command in remote_commands
                        if getattr(command, "name", None)
                    )
                    remote_name_set = set(remote_names)
                    log(
                        f"🔎 Guild command verification: {guild.name} ({guild.id}) -> "
                        f"remote={len(remote_names)} local={len(local_name_set)}"
                    )

                    if remote_name_set == local_name_set and len(remote_names) == len(command_names):
                        log(
                            f"✅ Guild command set already current | {guild.name} ({guild.id}) -> "
                            f"{len(remote_names)} commands | sync skipped"
                        )
                    else:
                        log(
                            f"🔄 Guild command set differs | {guild.name} ({guild.id}) | "
                            f"remote={', '.join(remote_names)} | local={', '.join(command_names)}"
                        )
                        bot_module.bot.tree.clear_commands(guild=guild)
                        bot_module.bot.tree.copy_global_to(guild=guild)
                        command_sync_performed = True
                        synced = await bot_module.bot.tree.sync(guild=guild)
                        remote_names = sorted(
                            getattr(command, "qualified_name", getattr(command, "name", ""))
                            for command in synced
                            if getattr(command, "name", None)
                        )
                        log(f"✅ Guild Sync: {guild.name} ({guild.id}) -> {len(remote_names)} commands")

                    log("🔎 Remote Guild Commands: " + ", ".join(remote_names))
                    missing_remote = sorted(required - set(remote_names))
                    if missing_remote:
                        log("❌ Required commands missing on " + guild.name + ": " + ", ".join(missing_remote))
                    else:
                        log("🟢 Required commands verified: /status /kill /setvoice")
                    successful += 1
                except discord.HTTPException as exc:
                    if getattr(exc, "status", None) == 429:
                        try:
                            bot_module._apply_discord_rest_429(exc, context="startup:command-verify")
                        except Exception:
                            pass
                    raise
            except Exception as exc:
                log(f"❌ Guild command verification/sync failed: {guild.name} ({guild.id}): {exc!r}")
                traceback.print_exc()

        if successful == len(guilds):
            # V138: verification can legitimately finish with zero HTTP writes when the
            # remote Guild command set already matches the local 20-command tree. V137
            # only released the background startup hold from tree.sync(), so the no-sync
            # path stayed permanently blocked and logged startup-command-sync-hold forever.
            # Release the hold here after successful verification. Existing probation remains
            # reserved for the case where an actual command sync was performed.
            try:
                bot_module.release_discord_rest_startup_hold(
                    reason="command-verification-complete",
                    arm_probation=False if not command_sync_performed else True,
                )
            except Exception as exc:
                log(f"⚠️ Startup REST hold release failed safely: {exc!r}")
            _sync_complete = True
            log(f"✅ DISCORD GUILD COMMAND SYNC COMPLETE ({successful}/{len(guilds)} guilds)")
        else:
            log(f"⚠️ DISCORD GUILD COMMAND SYNC PARTIAL ({successful}/{len(guilds)} guilds)")
        log("=" * 60)


bot_module.sync_commands_once = sync_commands_once


@bot_module.bot.listen("on_interaction")
async def interaction_diagnostic(interaction):
    """Diagnostic only: NEVER acknowledge/defer the interaction here."""
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
        log(
            "📥 INTERACTION RECEIVED | "
            f"command={command_name!r} user={interaction.user} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"channel={getattr(interaction, 'channel_id', None)}"
        )
    except Exception as exc:
        log(f"⚠️ interaction diagnostic failed: {exc!r}")


@bot_module.bot.listen("on_ready")
async def startup_command_sync():
    if SKYNET_RUNTIME_ROLE != "bot":
        return
    log("🟢 on_ready received by start.py")
    try:
        await sync_commands_once()
    except Exception as exc:
        log(f"❌ startup command sync failed: {exc!r}")
        traceback.print_exc()


async def startup_heartbeat():
    while True:
        await asyncio.sleep(30)
        try:
            log(
                "💓 SKYNET HEARTBEAT | "
                f"ready={bot_module.bot.is_ready()} "
                f"closed={bot_module.bot.is_closed()} "
                f"user={bot_module.bot.user} "
                f"guilds={len(bot_module.bot.guilds)}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"⚠️ heartbeat failed: {exc!r}")


async def main():
    log("=" * 60)
    log("🚀 SKYNET STARTING")
    log(f"🧭 Runtime role: {SKYNET_RUNTIME_ROLE}")
    log("=" * 60)

    if SKYNET_RUNTIME_ROLE == "web":
        log("🌐 Starting web server...")
        bot_module.keep_alive()
        log("🌐 Web server startup requested")
        log("🛡️ Render web runtime: Discord Gateway/REST DISABLED")
        heartbeat_task = asyncio.create_task(startup_heartbeat())
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            log("🛑 Web runtime stopped")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        return

    # Single Render Web Service mode: serve Dashboard/API and run the Discord bot
    # in the same process when SKYNET_RUNTIME_ROLE=bot.
    # This keeps the existing one-service deployment model working while
    # preventing accidental Gateway startup when role=web.
    bot_module.keep_alive()
    log("🌐 Web server startup requested (bot runtime shares this service)")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SKYNET_RUNTIME_ROLE=bot requires DISCORD_TOKEN")

    log("🔑 พบ DISCORD_TOKEN")
    log("🔌 กำลังเริ่ม Discord Bot...")
    log("🔌 กำลังเชื่อมต่อ Discord Gateway...")
    log("🛡️ Bot runtime: Discord Gateway/REST ENABLED on external runtime")

    heartbeat_task = asyncio.create_task(startup_heartbeat())
    try:
        await bot_module.run_bot_with_backoff(token)
    except KeyboardInterrupt:
        log("🛑 Bot stopped")
    except Exception as exc:
        log(f"❌ Discord Bot หยุดทำงาน: {exc!r}")
        traceback.print_exc()
        raise
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 SKYNET stopped by KeyboardInterrupt")
    except Exception as exc:
        log(f"💥 FATAL STARTUP ERROR: {exc!r}")
        traceback.print_exc()
        raise
