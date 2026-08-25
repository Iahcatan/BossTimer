import asyncio
import os
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands

import bot as bot_module


# ============================================================
# Emergency /status command
# ============================================================
# The current bot.py does not define /status. If Discord still has
# an old /status command cached/registered, Discord can invoke it but
# this application has no callback, resulting in "The application did
# not respond". Define it here so the command always has a handler.
if bot_module.bot.tree.get_command("status") is None:

    @bot_module.bot.tree.command(
        name="status",
        description="ตรวจสอบสถานะ SKYNET Bot, Discord Gateway และ Firebase"
    )
    async def status_command(interaction: discord.Interaction):
        # ACK immediately. Do not wait for Firebase, Voice or TTS.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            print(f"❌ /status defer failed: {e!r}")
            return

        try:
            latency_ms = round(bot_module.bot.latency * 1000, 1)
            guild_count = len(bot_module.bot.guilds)
            voice_count = sum(
                1 for g in bot_module.bot.guilds
                if g.voice_client and g.voice_client.is_connected()
            )

            firebase_state = "🟢 initialized" if getattr(bot_module.firebase_admin, "_apps", {}) else "🔴 not initialized"

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
            embed.add_field(name="📋 Commands", value=str(len(bot_module.bot.tree.get_commands())), inline=True)
            embed.add_field(name="⏰ Thailand", value=datetime.now(bot_module.TZ_THAI).strftime("%H:%M:%S"), inline=True)
            embed.set_footer(text="SKYNET • /status diagnostic")

            await interaction.followup.send(embed=embed, ephemeral=True)
            print(
                f"✅ /status handled | user={interaction.user} "
                f"guild={getattr(interaction.guild, 'id', None)} latency={latency_ms}ms"
            )
        except Exception as e:
            print(f"❌ /status handler error: {e!r}")
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /status error: `{type(e).__name__}: {e}`",
                    ephemeral=True,
                )
            except Exception:
                pass


# ============================================================
# Startup hook
# ============================================================
async def patched_setup_hook():
    """Register persistent views only; slash commands sync after login."""
    try:
        bot_module.bot.add_view(bot_module.QuickActionsView())
        print("✅ QuickActionsView registered")
    except Exception as e:
        print(f"⚠️ QuickActionsView registration failed: {e!r}")


# bot.py has its own setup_hook which performs a global sync. Replace it
# so there is exactly one command-sync owner: this file.
bot_module.bot.setup_hook = patched_setup_hook

_command_sync_lock = asyncio.Lock()
_commands_synced = False


@bot_module.bot.listen("on_interaction")
async def interaction_diagnostic(interaction: discord.Interaction):
    """Log every application command reaching the bot before the callback."""
    try:
        if interaction.type == discord.InteractionType.application_command:
            command_name = getattr(interaction.command, "qualified_name", None)
            if not command_name:
                command_name = getattr(interaction.data, "get", lambda *_: None)("name")
            print(
                f"📥 INTERACTION RECEIVED | command={command_name!r} "
                f"user={interaction.user} "
                f"guild={getattr(interaction.guild, 'id', None)} "
                f"channel={getattr(interaction, 'channel_id', None)}"
            )
    except Exception as e:
        print(f"⚠️ interaction diagnostic failed: {e!r}")


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
        local_commands = bot_module.bot.tree.get_commands()
        print(f"📋 Local commands before sync: {len(local_commands)}")
        print("📋 " + ", ".join(sorted(c.qualified_name for c in local_commands)))
        print("=" * 60)

        synced_any = False

        for guild in list(bot_module.bot.guilds):
            try:
                # Copy all local/global tree commands to this guild.
                bot_module.bot.tree.copy_global_to(guild=guild)
                synced = await bot_module.bot.tree.sync(guild=guild)
                print(
                    f"✅ Guild Sync: {guild.name} ({guild.id}) -> "
                    f"{len(synced)} commands"
                )
                print(
                    "📋 Guild commands: "
                    + ", ".join(sorted(c.qualified_name for c in synced))
                )
                synced_any = True
            except Exception as e:
                print(
                    f"❌ Guild Sync failed: {guild.name} ({guild.id}): {e!r}"
                )
                traceback.print_exc()

        if not synced_any:
            try:
                synced = await bot_module.bot.tree.sync()
                print(f"🌍 Global Sync fallback: {len(synced)} commands")
                print(
                    "📋 Global commands: "
                    + ", ".join(sorted(c.qualified_name for c in synced))
                )
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
