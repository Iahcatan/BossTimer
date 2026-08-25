import asyncio
import os
import traceback

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
# IMPORTANT /status note
# ------------------------------------------------------------
# /status is defined in bot.py and must remain owned by bot.py.
# Do NOT replace Command.callback here: discord.py 2.7 exposes
# Command.callback as a read-only property. The previous runtime
# patch attempted to assign it and caused startup to exit with:
# AttributeError: property 'callback' of 'Command' object has no setter
#
# bot.py's /status only reads the local boss schedule and therefore
# does not need a runtime callback replacement.

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
async def interaction_diagnostic(interaction):
    try:
        # Do not perform any await/defer/response here.
        # This listener is diagnostic only and must never consume the interaction.
        if interaction.type != interaction.InteractionType.application_command:
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
