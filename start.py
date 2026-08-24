import asyncio
import os
import traceback

from bot import bot, keep_alive, run_bot_with_backoff


@bot.listen("on_ready")
async def force_sync_commands():
    """
    Sync Slash Commands หลัง Bot login
    """

    try:
        print("=" * 60)
        print("🔄 FORCE SYNC: เริ่มซิงค์ Slash Commands")
        print(f"🤖 Bot: {bot.user}")
        print(f"🏠 Guilds: {len(bot.guilds)}")
        print("=" * 60)

        # Global Commands
        global_commands = await bot.tree.sync()

        print(
            f"🌍 Global Sync สำเร็จ: "
            f"{len(global_commands)} commands"
        )

        # Guild Commands
        for guild in bot.guilds:

            try:
                # Copy Global Commands เข้า Guild
                bot.tree.copy_global_to(guild=guild)

                guild_commands = await bot.tree.sync(
                    guild=guild
                )

                print(
                    f"✅ Guild Sync สำเร็จ: "
                    f"{guild.name} "
                    f"({guild.id}) -> "
                    f"{len(guild_commands)} commands"
                )

            except Exception as e:

                print(
                    f"❌ Guild Sync ไม่สำเร็จ: "
                    f"{guild.name} ({guild.id})"
                )

                print(repr(e))
                traceback.print_exc()

        print("=" * 60)
        print("✅ FORCE SYNC เสร็จสมบูรณ์")
        print("=" * 60)

    except Exception as e:

        print("❌ FORCE SYNC ล้มเหลว")
        print(repr(e))

        traceback.print_exc()


async def main():

    print("=" * 60)
    print("🚀 SKYNET STARTING")
    print("=" * 60)

    # ==========================================
    # Render Web Server
    # ==========================================

    print("🌐 Starting web server...")

    keep_alive()

    # ==========================================
    # Discord Token
    # ==========================================

    token = os.environ.get("DISCORD_TOKEN")

    if not token:

        print("❌ ERROR: ไม่พบ DISCORD_TOKEN")

        return

    print("🔑 พบ DISCORD_TOKEN")

    print("🔌 กำลังเริ่ม Discord Bot...")

    try:

        await run_bot_with_backoff(token)

    except Exception as e:

        print("❌ Discord Bot หยุดทำงาน")

        print(repr(e))

        traceback.print_exc()

        raise


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print("🛑 Bot stopped")
