import asyncio
import discord
import os
import sys
import traceback
from discord import app_commands
import signal
import time
import uuid

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
# start.py owns process startup/hand-over control and command synchronization only.
# bot.py remains the owner of Firebase, Boss Timer, /kill,
# /setvoice, /status, TTS, Voice, Dashboard and background tasks.

COMMAND_SYNC_DELAY = float(os.environ.get("COMMAND_SYNC_DELAY", "1.0"))


def log(message: str):
    print(message, flush=True)


# ============================================================
# 🛡️ V149: DISCORD REST-AWARE GATEWAY STARTUP + SINGLE RUNTIME HANDOVER
#
# Render Web Services use zero-downtime deploys: a new instance can start
# while the previous instance is still alive. The old V142 in-process guard
# cannot see a different Python process, so it cannot prevent two Gateway
# sessions from using the same Discord token during the handover window.
#
# This lock lives in Firebase (not Discord) and is therefore outside Discord's
# rate-limit system. Only one process may hold the Gateway lease at a time.
# The first V143 bootstrap also waits long enough for a legacy pre-V143 process
# to receive Render's SIGTERM before taking the lease.
# ============================================================
GATEWAY_LEASE_PATH = os.environ.get(
    "DISCORD_GATEWAY_LEASE_PATH",
    "app_settings/discord_gateway_lease_v143",
).strip() or "app_settings/discord_gateway_lease_v143"
GATEWAY_LEASE_META_PATH = os.environ.get(
    "DISCORD_GATEWAY_LEASE_META_PATH",
    "app_settings/discord_gateway_lease_v143_meta",
).strip() or "app_settings/discord_gateway_lease_v143_meta"
GATEWAY_LEASE_TTL_SECONDS = max(
    90.0,
    float(os.environ.get("DISCORD_GATEWAY_LEASE_TTL", "120")),
)
GATEWAY_LEASE_RENEW_SECONDS = max(
    15.0,
    min(GATEWAY_LEASE_TTL_SECONDS / 3.0, float(os.environ.get("DISCORD_GATEWAY_LEASE_RENEW", "20"))),
)
GATEWAY_LEASE_POLL_SECONDS = max(
    5.0,
    float(os.environ.get("DISCORD_GATEWAY_LEASE_POLL", "10")),
)
# Render sends SIGTERM to the old instance 60 seconds after the new instance
# becomes ready, then waits for the configured shutdown delay (30 seconds by
# default). This one-time bootstrap window covers the legacy V144 -> V149 handover.
GATEWAY_LEGACY_HANDOVER_SECONDS = max(
    60.0,
    float(os.environ.get("DISCORD_GATEWAY_LEGACY_HANDOVER", "75")),
)
GATEWAY_LEASE_INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
gateway_lease_lost_event = asyncio.Event()
gateway_shutdown_event = asyncio.Event()


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


async def _gateway_lease_meta_exists() -> bool:
    """Read the one-time V143 guard marker from Firebase without touching Discord."""
    try:
        raw = await asyncio.to_thread(
            lambda: bot_module.db.reference(GATEWAY_LEASE_META_PATH).get()
        )
        return isinstance(raw, dict) and bool(raw.get("initialized"))
    except Exception as exc:
        log(f"⚠️ Gateway handover meta read failed safely: {exc!r}")
        # Do not assume the guard is initialized when Firebase cannot be read.
        # A bootstrap hold is the safer choice because it prevents an overlap.
        return False


def _gateway_lease_transaction(current):
    """Atomic Firebase transaction callback for acquiring our single Gateway lease."""
    now = time.time()
    current = dict(current) if isinstance(current, dict) else {}
    holder = str(current.get("holder_id") or "")
    expires_at = _safe_float(current.get("expires_at"), 0.0)
    if not holder or holder == GATEWAY_LEASE_INSTANCE_ID or expires_at <= now:
        acquired_at = _safe_float(current.get("acquired_at"), 0.0)
        if not acquired_at or holder != GATEWAY_LEASE_INSTANCE_ID:
            acquired_at = now
        return {
            "version": 1,
            "holder_id": GATEWAY_LEASE_INSTANCE_ID,
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "renewed_at": now,
            "expires_at": now + GATEWAY_LEASE_TTL_SECONDS,
            "runtime_role": SKYNET_RUNTIME_ROLE,
        }
    return current


async def acquire_gateway_lease() -> bool:
    """Attempt one atomic lease claim; return True only if this process owns it."""
    try:
        result = await asyncio.to_thread(
            lambda: bot_module.db.reference(GATEWAY_LEASE_PATH).transaction(_gateway_lease_transaction)
        )
        if isinstance(result, dict) and str(result.get("holder_id") or "") == GATEWAY_LEASE_INSTANCE_ID:
            return True
        return False
    except Exception as exc:
        log(f"⚠️ Gateway lease acquire failed safely: {exc!r}")
        return False


def _gateway_lease_renew_transaction(current):
    now = time.time()
    if not isinstance(current, dict):
        return current
    if str(current.get("holder_id") or "") != GATEWAY_LEASE_INSTANCE_ID:
        return current
    updated = dict(current)
    updated["renewed_at"] = now
    updated["expires_at"] = now + GATEWAY_LEASE_TTL_SECONDS
    updated["pid"] = os.getpid()
    return updated


async def renew_gateway_lease_once() -> bool:
    """Renew only when the Firebase record still belongs to this process."""
    try:
        result = await asyncio.to_thread(
            lambda: bot_module.db.reference(GATEWAY_LEASE_PATH).transaction(_gateway_lease_renew_transaction)
        )
        owned = isinstance(result, dict) and str(result.get("holder_id") or "") == GATEWAY_LEASE_INSTANCE_ID
        if not owned:
            bot_module.discord_rest_runtime_lease_owned = False
            gateway_lease_lost_event.set()
            log("🚨 Gateway lease lost to another runtime; Discord REST ownership disabled and Gateway shutdown requested")
        return owned
    except Exception as exc:
        # If ownership cannot be renewed, stop the Gateway rather than risking
        # split-brain operation after the lease expires and another runtime claims it.
        bot_module.discord_rest_runtime_lease_owned = False
        gateway_lease_lost_event.set()
        log(f"🚨 Gateway lease renewal could not be confirmed; Discord REST ownership disabled; stopping Gateway safely: {exc!r}")
        return False


def _gateway_lease_release_transaction(current):
    # Firebase Admin transaction callbacks must return a concrete value; returning
    # None causes ValueError("Value must not be none."). Use an empty object to
    # clear our holder while preserving the transaction contract.
    if isinstance(current, dict) and str(current.get("holder_id") or "") == GATEWAY_LEASE_INSTANCE_ID:
        return {}
    return current


async def release_gateway_lease(reason: str = "shutdown"):
    """Release our lease only; never remove another process's lease."""
    try:
        result = await asyncio.to_thread(
            lambda: bot_module.db.reference(GATEWAY_LEASE_PATH).transaction(_gateway_lease_release_transaction)
        )
        released = not isinstance(result, dict) or str(result.get("holder_id") or "") != GATEWAY_LEASE_INSTANCE_ID
        if released:
            log(f"🔓 Discord Gateway lease released | reason={reason}")
    except Exception as exc:
        log(f"⚠️ Discord Gateway lease release failed safely | reason={reason} | {exc!r}")


async def gateway_lease_worker():
    """Keep the single Gateway lease alive and detect ownership loss."""
    while not gateway_shutdown_event.is_set():
        try:
            try:
                await asyncio.wait_for(
                    gateway_shutdown_event.wait(),
                    timeout=GATEWAY_LEASE_RENEW_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                pass

            if gateway_shutdown_event.is_set():
                break
            await renew_gateway_lease_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"⚠️ Gateway lease worker failed safely: {exc!r}")


async def wait_for_gateway_lease() -> bool:
    """Wait until this process exclusively owns the Discord Gateway lease."""
    initialized = await _gateway_lease_meta_exists()
    if not initialized:
        log(
            "🛡️ V144 first-runtime handover guard | "
            f"waiting {GATEWAY_LEGACY_HANDOVER_SECONDS:.0f}s before claiming Gateway lease "
            "so a legacy V143 Render instance can terminate first"
        )
        try:
            await asyncio.wait_for(
                gateway_shutdown_event.wait(),
                timeout=GATEWAY_LEGACY_HANDOVER_SECONDS,
            )
            return False
        except asyncio.TimeoutError:
            pass

    while not gateway_shutdown_event.is_set():
        if await acquire_gateway_lease():
            try:
                await asyncio.to_thread(
                    lambda: bot_module.db.reference(GATEWAY_LEASE_META_PATH).set({
                        "version": 1,
                        "initialized": True,
                        "initialized_at": time.time(),
                        "last_holder_id": GATEWAY_LEASE_INSTANCE_ID,
                    })
                )
            except Exception as exc:
                log(f"⚠️ Gateway handover meta write failed safely: {exc!r}")
            log(
                "🔐 Discord Gateway lease ACQUIRED | "
                f"instance={GATEWAY_LEASE_INSTANCE_ID} | ttl={GATEWAY_LEASE_TTL_SECONDS:.0f}s"
            )
            return True

        # The transaction returns the currently-held lease, so avoid another
        # Firebase read just to calculate a wait time. This keeps the control plane small.
        log(
            "⏳ Discord Gateway lease busy | "
            f"another runtime owns the lease; retry in {GATEWAY_LEASE_POLL_SECONDS:.0f}s | no Discord request sent"
        )
        try:
            await asyncio.wait_for(
                gateway_shutdown_event.wait(),
                timeout=GATEWAY_LEASE_POLL_SECONDS,
            )
            return False
        except asyncio.TimeoutError:
            pass
    return False


def install_gateway_signal_handlers(loop):
    """Convert Render SIGTERM/SIGINT into a graceful async shutdown signal."""
    def _signal_received(signum):
        signal_name = getattr(signal.Signals(signum), "name", str(signum))
        log(f"🛑 {signal_name} received | preparing graceful Discord Gateway handover")
        gateway_shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_received, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows / non-main-loop environments may not support add_signal_handler.
            pass


async def run_gateway_until_shutdown(token: str, lease_worker_task):
    """Run the existing Gateway controller until shutdown or lease loss."""
    gateway_task = asyncio.create_task(
        bot_module.run_bot_with_backoff(token),
        name="skynet-gateway-runner",
    )
    shutdown_wait_task = asyncio.create_task(
        gateway_shutdown_event.wait(),
        name="skynet-gateway-shutdown-wait",
    )
    lease_loss_wait_task = asyncio.create_task(
        gateway_lease_lost_event.wait(),
        name="skynet-gateway-lease-loss-wait",
    )

    try:
        done, _pending = await asyncio.wait(
            {gateway_task, shutdown_wait_task, lease_loss_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_wait_task in done or lease_loss_wait_task in done:
            if lease_loss_wait_task in done and gateway_lease_lost_event.is_set():
                log("🛑 Gateway lease ownership is no longer exclusive; stopping Discord Gateway safely")
            else:
                log("🛑 Graceful shutdown requested; stopping Discord Gateway safely")
            try:
                await bot_module.bot.close()
            except Exception as exc:
                log(f"⚠️ Discord bot close failed safely: {exc!r}")
            if not gateway_task.done():
                gateway_task.cancel()
            await asyncio.gather(gateway_task, return_exceptions=True)
            return

        # The existing gateway controller should normally be long-lived. If it ever
        # terminates or raises unexpectedly, preserve that behavior instead of hiding it.
        result = await gateway_task
        return result
    finally:
        for task in (shutdown_wait_task, lease_loss_wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(shutdown_wait_task, lease_loss_wait_task, return_exceptions=True)
        if lease_worker_task is not None and not lease_worker_task.done():
            lease_worker_task.cancel()
            await asyncio.gather(lease_worker_task, return_exceptions=True)


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
        log("🔄 SKYNET DISCORD COMMAND SYNC | V144 verify-first Guild Commands (Gateway handover + REST diagnostics + voice/attendance safeguards)")
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
                await bot_module.wait_for_discord_rest_clear_confirmed(context="startup:command-verify")
                try:
                    remote_commands = await bot_module.guarded_discord_call(
                        lambda: bot_module.bot.tree.fetch_commands(guild=guild),
                        context="startup:command-verify",
                        background=False,
                        wait_for_cooldown=False,
                    )
                    if remote_commands is None:
                        log(
                            f"⏭️ Guild command verification deferred by Discord REST guard | "
                            f"{guild.name} ({guild.id}) | no HTTP sent"
                        )
                        continue
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
    log("🛡️ Bot runtime: Discord Gateway/REST ENABLED on external runtime")
    log("🛡️ Gateway startup is gated by Firebase handover lease; no Discord request is sent while another runtime owns it")

    # V152: retain V151 Discord REST recovery-only gating; Boss Voice/queue behavior changes are isolated to bot.py.
    # V149: restore any durable Discord temporary-API restriction BEFORE the first
    # Gateway request. The previous flow restored this state only inside on_ready(),
    # which is too late: a new Render process could hit Discord Gateway first and
    # receive another 429 during an already-active server restriction. This restore
    # performs only Firebase/SQLite reads and local waiting; it sends no Discord HTTP.
    try:
        restored = await bot_module.restore_persisted_discord_block_state()
        if restored:
            await bot_module.wait_for_discord_rest_startup_gate(context="startup:gateway")
            log("🟢 Persisted Discord REST restriction gate cleared for Gateway startup; no early HTTP probe was sent")
    except Exception as exc:
        # Preserve the existing startup path if persistence is temporarily unavailable.
        # A real Discord 429 is still handled by bot.py and persisted for the next retry.
        log(f"⚠️ Pre-Gateway Discord REST restriction restore failed safely: {exc!r}")

    install_gateway_signal_handlers(asyncio.get_running_loop())
    heartbeat_task = asyncio.create_task(startup_heartbeat())
    lease_worker_task = None
    lease_acquired = False
    try:
        # IMPORTANT: do not touch Discord Gateway until this process exclusively
        # owns the handover lease. This prevents Render's old/new overlap from
        # producing two simultaneous Gateway sessions with the same token.
        lease_acquired = await wait_for_gateway_lease()
        if not lease_acquired:
            log("🛑 Gateway startup cancelled before lease acquisition")
            return

        # V150: use the same single-runtime Firebase handover lease for normal
        # Discord REST. This prevents old/new Render deploy overlap from creating
        # two concurrent REST writers even when Gateway ownership is exclusive.
        bot_module.discord_rest_runtime_lease_owned = True
        log("🛡️ Discord REST runtime lease ownership ENABLED with Gateway lease")

        lease_worker_task = asyncio.create_task(
            gateway_lease_worker(),
            name="skynet-gateway-lease-worker",
        )
        log("🔌 กำลังเชื่อมต่อ Discord Gateway... (single-runtime lease confirmed)")
        await run_gateway_until_shutdown(token, lease_worker_task)
    except KeyboardInterrupt:
        log("🛑 Bot stopped")
    except Exception as exc:
        log(f"❌ Discord Bot หยุดทำงาน: {exc!r}")
        traceback.print_exc()
        raise
    finally:
        gateway_shutdown_event.set()
        if lease_worker_task is not None and not lease_worker_task.done():
            lease_worker_task.cancel()
            await asyncio.gather(lease_worker_task, return_exceptions=True)
        if lease_acquired:
            bot_module.discord_rest_runtime_lease_owned = False
            log("🛡️ Discord REST runtime lease ownership DISABLED before Gateway lease release")
            await release_gateway_lease(reason="runtime-exit")
        try:
            await bot_module.bot.close()
        except Exception as exc:
            log(f"⚠️ Discord bot close during runtime-exit failed safely: {exc!r}")
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
