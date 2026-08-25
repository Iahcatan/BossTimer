import asyncio
import os
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands

import bot as bot_module

# ============================================================
# SKYNET runtime bootstrap
# ============================================================
# start.py is the single owner of Discord application-command sync.
# bot.py continues to own Firebase, TTS, Voice, Dashboard and the
# actual command implementations.

if bot_module.bot.tree.get_command("status") is None:
    @bot_module.bot.tree.command(
        name="status",
        description="ตรวจสอบสถานะ SKYNET Bot, Discord Gateway และ Firebase",
    )
    async def status_command(interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as exc:
            print(f"❌ /status defer failed: {exc!r}")
            return
        try:
            latency_ms = round(bot_module.bot.latency * 1000, 1)
            guild_count = len(bot_module.bot.guilds)
            voice_count = sum(
                1 for guild in bot_module.bot.guilds
                if guild.voice_client and guild.voice_client.is_connected()
            )
            firebase_state = (
                "🟢 initialized"
                if getattr(bot_module.firebase_admin, "_apps", {}) else
                "🔴 not initialized"
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
            embed.add_field(name="📋 Local Commands", value=str(len(bot_module.bot.tree.get_commands())), inline=True)
            embed.add_field(name="⏰ Thailand", value=datetime.now(bot_module.TZ_THAI).strftime("%H:%M:%S"), inline=True)
            embed.set_footer(text="SKYNET • /status diagnostic")
            await interaction.followup.send(embed=embed, ephemeral=True)
            print(f"✅ /status handled | user={interaction.user} guild={getattr(interaction.guild, 'id', None)} latency={latency_ms}ms")
        except Exception as exc:
            print(f"❌ /status handler error: {exc!r}")
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /status error: `{type(exc).__name__}: {exc}`",
                    ephemeral=True,
                )
            except Exception:
                pass

# Protect startup from old/malformed custom_bosses values in Firebase.
_original_load_custom_bosses = getattr(bot_module, "load_custom_bosses", None)
if _original_load_custom_bosses is not None:
    async def safe_load_custom_bosses():
        try:
            await _original_load_custom_bosses()
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            print(
                "⚠️ custom_bosses มีข้อมูลเก่าหรือรูปแบบไม่ถูกต้อง "
                f"({type(exc).__name__}: {exc}) — ข้ามข้อมูลที่ผิดรูปแบบ"
            )
        except Exception as exc:
            print(f"⚠️ load_custom_bosses failed safely: {exc!r}")
            traceback.print_exc()
    bot_module.load_custom_bosses = safe_load_custom_bosses

# Do not let bot.py's older setup_hook perform a second global sync.
async def patched_setup_hook():
    try:
        bot_module.bot.add_view(bot_module.QuickActionsView())
        print("✅ QuickActionsView registered")
    except Exception as exc:
        print(f"⚠️ QuickActionsView registration failed: {exc!r}")

bot_module.bot.setup_hook = patched_setup_hook

_command_sync_lock = asyncio.Lock()
_commands_synced = False

async def sync_commands_once():
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
        print(f"📋 Local commands: {len(local_commands)}")
        print("📋 " + ", ".join(sorted(c.qualified_name for c in local_commands)))

        try:
            global_synced = await bot_module.bot.tree.sync()
            print(f"🌍 Global Sync: {len(global_synced)} commands")
        except Exception as exc:
            print(f"⚠️ Global Sync failed: {exc!r}")
            traceback.print_exc()

        for guild in list(bot_module.bot.guilds):
            try:
                bot_module.bot.tree.clear_commands(guild=guild)
                bot_module.bot.tree.copy_global_to(guild=guild)
                guild_synced = await bot_module.bot.tree.sync(guild=guild)
                print(f"✅ Guild Sync: {guild.name} ({guild.id}) -> {len(guild_synced)} commands")
                try:
                    remote = await bot_module.bot.tree.fetch_commands(guild=guild)
                    print("🔎 Verified Guild Commands: " + ", ".join(sorted(c.name for c in remote)))
                except Exception as verify_exc:
                    print(f"⚠️ Guild command verification failed for {guild.name}: {verify_exc!r}")
            except Exception as exc:
                print(f"❌ Guild Sync failed: {guild.name} ({guild.id}): {exc!r}")
                traceback.print_exc()

        _commands_synced = True
        print("✅ DISCORD COMMAND SYNC COMPLETE")
        print("=" * 60)

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
    await sync_commands_once()

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
