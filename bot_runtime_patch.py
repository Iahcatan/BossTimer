"""SKYNET runtime compatibility patch.

This module patches the already-loaded bot module without replacing bot.py.
It deliberately keeps Firebase, boss commands, TTS, Voice and Dashboard code
in bot.py intact. Its only responsibilities are startup safety and deterministic
per-guild slash-command registration.
"""

import asyncio
import os
import traceback

import discord


_SYNC_LOCK = asyncio.Lock()
_SYNC_DONE = False


def install(bot_module):
    """Install safe startup behavior on bot_module.bot."""
    bot = bot_module.bot

    # Preserve bot.py's existing setup_hook, but do not let its global command
    # sync prevent the bot from reaching READY. QuickActionsView remains intact.
    original_setup_hook = getattr(bot, "setup_hook", None)

    async def safe_setup_hook():
        if original_setup_hook is not None:
            try:
                await original_setup_hook()
            except Exception as exc:
                print(f"⚠️ bot.py setup_hook error (continuing): {exc!r}")
                traceback.print_exc()

        # The original setup_hook already registers QuickActionsView. Only add
        # another instance if the original hook was absent or failed before it.
        try:
            if hasattr(bot_module, "QuickActionsView"):
                bot.add_view(bot_module.QuickActionsView())
                print("✅ Runtime QuickActionsView registered")
        except Exception as exc:
            # Duplicate persistent views are harmless; never block Gateway.
            print(f"⚠️ Runtime QuickActionsView registration skipped: {exc!r}")

    bot.setup_hook = safe_setup_hook

    async def sync_guild_commands():
        global _SYNC_DONE
        if _SYNC_DONE:
            return

        async with _SYNC_LOCK:
            if _SYNC_DONE:
                return

            await bot.wait_until_ready()
            await asyncio.sleep(float(os.environ.get("COMMAND_SYNC_DELAY", "0.5")))

            local = bot.tree.get_commands()
            names = sorted(cmd.qualified_name for cmd in local)
            print("=" * 60)
            print("🔄 SKYNET HARD GUILD COMMAND SYNC")
            print(f"📋 Local commands ({len(names)}): {', '.join(names)}")

            required = {"status", "kill", "setvoice"}
            missing = required.difference(names)
            if missing:
                print(f"❌ Required commands missing locally: {', '.join(sorted(missing))}")

            guilds = list(bot.guilds)
            configured_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
            if configured_id.isdigit():
                wanted = int(configured_id)
                guilds = [g for g in guilds if g.id == wanted]
                if not guilds:
                    print(f"⚠️ DISCORD_GUILD_ID={wanted} not present in Gateway guild cache")

            if not guilds:
                print("❌ No guild available for slash-command sync")
                print("=" * 60)
                return

            success = 0
            for guild in guilds:
                try:
                    # Remove only this guild's local command mirror, then copy
                    # the authoritative global tree into it.
                    bot.tree.clear_commands(guild=guild)
                    bot.tree.copy_global_to(guild=guild)
                    synced = await bot.tree.sync(guild=guild)
                    remote = sorted(cmd.qualified_name for cmd in synced)
                    print(f"✅ Guild Sync: {guild.name} ({guild.id}) -> {len(remote)}")
                    print(f"🔎 Remote: {', '.join(remote)}")
                    missing_remote = required.difference(remote)
                    if missing_remote:
                        print(f"❌ Missing remotely: {', '.join(sorted(missing_remote))}")
                    else:
                        print("🟢 /status /kill /setvoice verified")
                    success += 1
                except Exception as exc:
                    print(f"❌ Guild sync failed: {guild.name} ({guild.id}): {exc!r}")
                    traceback.print_exc()

            if success == len(guilds):
                _SYNC_DONE = True
                print(f"✅ COMMAND SYNC COMPLETE ({success}/{len(guilds)})")
            else:
                print(f"⚠️ COMMAND SYNC PARTIAL ({success}/{len(guilds)})")
            print("=" * 60)

    # Expose a stable callable for start.py and diagnostics.
    bot_module.sync_guild_commands = sync_guild_commands
    return sync_guild_commands
