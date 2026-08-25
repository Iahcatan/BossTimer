import asyncio
import os
import traceback

import bot as bot_module


async def patched_setup_hook():
    """Register persistent views only; slash commands are synced after guild cache is ready."""
    try:
        bot_module.bot.add_view(bot_module.QuickActionsView())
        print("✅ QuickActionsView registered")
    except Exception as e:
        print(f"⚠️ QuickActionsView registration failed: {e!r}")


# The old bot.py setup_hook performs a global sync. Replace it with a view-only
# hook so command synchronization has exactly one owner: this startup module.
bot_module.bot.setup_hook = patched_setup_hook

_command_sync_lock = asyncio.Lock()
_commands_synced = False


@bot_module.bot.listen("on_ready")
async def sync_commands_after_ready():
    global _commands_synced
    if _commands_synced:
        return

    async with _command_sync_lock:
        if _commands_synced:
            return

        print("=" * 60)
        print("🔄 DISCORD COMMAND SYNC")
        print(f"🤖 Bot: {bot_module.bot.user}")
        print(f"🏠 Guilds: {len(bot_module.bot.guilds)}")
        print("=" * 60)

        synced_any = False
        for guild in list(bot_module.bot.guilds):
            try:
                bot_module.bot.tree.copy_global_to(guild=guild)
                synced = await bot_module.bot.tree.sync(guild=guild)
                print(f"✅ Guild Sync: {guild.name} ({guild.id}) -> {len(synced)} commands")
                synced_any = True
            except Exception as e:
                print(f"❌ Guild Sync failed: {guild.name} ({guild.id}): {e!r}")
                traceback.print_exc()

        if not synced_any:
            try:
                synced = await bot_module.bot.tree.sync()
                print(f"🌍 Global Sync fallback: {len(synced)} commands")
            except Exception as e:
                print(f"❌ Global Sync failed: {e!r}")
                traceback.print_exc()

        _commands_synced = True
        print("✅ DISCORD COMMAND SYNC COMPLETE")
        print("=" * 60)


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
    except Exception as e:
        print(f"❌ Discord Bot หยุดทำงาน: {e!r}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    asyncio.run(main())
