import asyncio
import os
import sys
import traceback
import aiohttp
import discord

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import bot as bot_module
import voice_patch

COMMAND_SYNC_DELAY = float(os.environ.get("COMMAND_SYNC_DELAY", "1.0"))


def log(message: str):
    print(message, flush=True)


# Install only the Voice/TTS runtime layer. Firebase, Dashboard, commands,
# boss scheduler and persistent voice_config remain owned by bot.py.
voice_patch.install(bot_module, log)


async def patched_setup_hook():
    """Keep setup_hook lightweight; start.py owns Gateway lifecycle and command sync."""
    try:
        if hasattr(bot_module, "QuickActionsView"):
            bot_module.bot.add_view(bot_module.QuickActionsView())
            log("✅ QuickActionsView registered")
    except Exception as exc:
        log(f"⚠️ QuickActionsView registration skipped: {exc!r}")


bot_module.bot.setup_hook = patched_setup_hook


_original_load_custom_bosses = getattr(bot_module, "load_custom_bosses", None)
if _original_load_custom_bosses is not None:
    async def safe_load_custom_bosses():
        try:
            await _original_load_custom_bosses()
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            log("⚠️ custom_bosses invalid/legacy data skipped: " f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log(f"⚠️ load_custom_bosses failed safely: {exc!r}")
            traceback.print_exc()
    bot_module.load_custom_bosses = safe_load_custom_bosses


_sync_lock = asyncio.Lock()
_sync_complete = False


async def sync_commands_once():
    """Sync guild commands after Gateway READY; compatible with discord.py 2.7 AppCommand."""
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
        log("🔄 SKYNET DISCORD COMMAND SYNC")
        log(f"🤖 Bot: {bot_module.bot.user}")
        log(f"🆔 Bot ID: {getattr(bot_module.bot.user, 'id', None)}")
        log(f"🏠 Guilds: {len(bot_module.bot.guilds)}")

        local_commands = bot_module.bot.tree.get_commands()
        command_names = sorted(
            getattr(command, "qualified_name", getattr(command, "name", "unknown"))
            for command in local_commands
        )
        log(f"📋 Local commands: {len(command_names)}")
        log("📋 " + ", ".join(command_names))

        required = {"status", "kill", "setvoice", "notice"}
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

        successful = 0
        for guild in guilds:
            try:
                bot_module.bot.tree.clear_commands(guild=guild)
                bot_module.bot.tree.copy_global_to(guild=guild)
                synced = await bot_module.bot.tree.sync(guild=guild)
                remote_names = sorted(getattr(command, "name", str(command)) for command in synced)
                log(f"✅ Guild Sync: {guild.name} ({guild.id}) -> {len(remote_names)} commands")
                log("🔎 Remote Guild Commands: " + ", ".join(remote_names))
                missing_remote = sorted(required - set(remote_names))
                if missing_remote:
                    log(f"❌ Required commands missing on {guild.name}: {', '.join(missing_remote)}")
                else:
                    log("🟢 Required commands verified: /status /kill /setvoice /notice")
                successful += 1
            except Exception as exc:
                log(f"❌ Guild Sync failed: {guild.name} ({guild.id}): {exc!r}")
                traceback.print_exc()

        if successful == len(guilds):
            _sync_complete = True
            log(f"✅ DISCORD GUILD COMMAND SYNC COMPLETE ({successful}/{len(guilds)} guilds)")
        else:
            log(f"⚠️ DISCORD GUILD COMMAND SYNC PARTIAL ({successful}/{len(guilds)} guilds)")
        log("=" * 60)


bot_module.sync_commands_once = sync_commands_once


@bot_module.bot.listen("on_ready")
async def startup_command_sync():
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
            log("💓 SKYNET HEARTBEAT | " f"ready={bot_module.bot.is_ready()} " f"closed={bot_module.bot.is_closed()} " f"user={bot_module.bot.user} " f"guilds={len(bot_module.bot.guilds)}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"⚠️ heartbeat failed: {exc!r}")


async def run_bot_with_backoff(token: str):
    """SINGLE Discord Gateway lifecycle owner. bot.py never starts/retries Gateway."""
    backoff = 30.0
    max_backoff = 300.0
    while True:
        try:
            log("🔌 กำลังเชื่อมต่อ Discord Gateway...")
            await bot_module.bot.start(token, reconnect=True)
            log("🛑 Discord bot stopped normally.")
            return
        except discord.LoginFailure:
            log("❌ Discord LoginFailure: ตรวจสอบ DISCORD_TOKEN")
            raise
        except discord.HTTPException as exc:
            retry_after = getattr(exc, "retry_after", None)
            delay = min(max(float(retry_after), backoff) if retry_after is not None else backoff, max_backoff)
            log(f"🛑 Discord HTTP error — รอ {delay:.0f} วินาทีก่อนสร้าง Gateway session ใหม่: {exc!r}")
            try:
                await bot_module.bot.close()
            except Exception:
                pass
            try:
                bot_module.bot.clear()
            except Exception:
                pass
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, max_backoff)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            delay = min(backoff, max_backoff)
            log(f"🛑 Discord network error — รอ {delay:.0f} วินาทีก่อนสร้าง Gateway session ใหม่: {exc!r}")
            try:
                await bot_module.bot.close()
            except Exception:
                pass
            try:
                bot_module.bot.clear()
            except Exception:
                pass
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, max_backoff)
        except RuntimeError as exc:
            if "Session is closed" in str(exc):
                delay = min(backoff, max_backoff)
                log(f"⚠️ Discord session ถูกปิดก่อนเริ่มใหม่ — รอ {delay:.0f} วินาทีแล้ว reset client")
                try:
                    await bot_module.bot.close()
                except Exception:
                    pass
                try:
                    bot_module.bot.clear()
                except Exception:
                    pass
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, max_backoff)
                continue
            raise
        except Exception as exc:
            log(f"❌ Discord Gateway fatal error: {exc!r}")
            traceback.print_exc()
            raise


async def main():
    print("=" * 60)
    print("🚀 SKYNET STARTING")
    print("=" * 60)
    print("🌐 Starting web server...")
    try:
        from threading import Thread
        from waitress import serve
        def run_web():
            port = int(os.environ.get("PORT", "5000"))
            log(f"🌐 Starting Flask/Waitress on 0.0.0.0:{port}")
            serve(bot_module.app, host="0.0.0.0", port=port, threads=4)
        Thread(target=run_web, daemon=True).start()
        print("🌐 Web server startup requested")
    except Exception as exc:
        print(f"❌ Web server startup failed: {exc!r}")
        traceback.print_exc()

    token = os.environ.get("DISCORD_TOKEN", "").strip() or os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ไม่พบ DISCORD_TOKEN หรือ DISCORD_BOT_TOKEN")
    print("🔑 พบ DISCORD_TOKEN")
    print("🔌 กำลังเริ่ม Discord Bot...")
    heartbeat_task = asyncio.create_task(startup_heartbeat())
    try:
        await run_bot_with_backoff(token)
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
        print("🛑 SKYNET stopped by KeyboardInterrupt")
    except Exception as exc:
        print(f"❌ SKYNET stopped: {exc!r}")
        traceback.print_exc()
        raise
