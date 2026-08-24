import asyncio
import traceback

from bot import bot, keep_alive, run_bot_with_backoff


@bot.listen("on_ready")
async def force_sync_commands():
    """
    Sync slash commands แบบ Guild หลัง Bot login
    เพื่อให้คำสั่งใช้งานได้ทันทีใน Server
    """
    try:
        print("🔄 FORCE SYNC: เริ่มซิงค์ Slash Commands...")

        # Global sync
        global_commands = await bot.tree.sync()
        print(
            f"🌍 Global Slash Commands Sync สำเร็จ: "
            f"{len(global_commands)} คำสั่ง"
        )

        # Guild sync
        for guild in bot.guilds:
            try:
                guild_commands = await bot.tree.sync(guild=guild)

                print(
                    f"🏠 Guild Sync สำเร็จ: "
                    f"{guild.name} ({guild.id}) = "
                    f"{len(guild_commands)} คำสั่ง"
                )

            except Exception as guild_error:
                print(
                    f"❌ Guild Sync ไม่สำเร็จ: "
                    f"{guild.name} ({guild.id})"
                )
                print(guild_error)

        print("✅ FORCE SYNC Slash Commands เสร็จสมบูรณ์")

    except Exception as e:
        print("❌ FORCE SYNC Slash Commands ล้มเหลว")
        print(e)
        traceback.print_exc()


async def main():
    keep_alive()

    token = __import__("os").environ.get("DISCORD_TOKEN")

    if not token:
        print("❌ ไม่พบ DISCORD_TOKEN")
        return

    await run_bot_with_backoff(token)


if __name__ == "__main__":
    asyncio.run(main())