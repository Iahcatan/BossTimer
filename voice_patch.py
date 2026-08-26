"""Runtime Voice/TTS patch for SKYNET.

Voice is ON-DEMAND:
    /setvoice -> remembers the configured channel only
    notification / notice / tts -> connects when needed
    TTS finishes -> disconnects from voice

Firebase, Dashboard, boss scheduling and slash-command data ownership remain in
bot.py. Discord Gateway lifecycle remains owned by start.py.
"""
import asyncio
import os
import time
import uuid
import logging

import discord
import edge_tts
from discord import app_commands

_voice_locks = {}
_reconnect_locks = {}
_last_voice_connect_attempt = {}
_watchdog_task = None
_notification_watchdog_task = None

VOICE_RECONNECT_COOLDOWN = 12.0
VOICE_CONNECT_TIMEOUT = 20.0
TTS_PLAY_TIMEOUT = 90.0


def install(bot_module, log):
    """Install on-demand Voice/TTS runtime without replacing data/scheduling code."""

    async def ensure_voice(guild, target_channel=None):
        """Connect to the configured Voice channel only when a TTS operation needs it."""
        if guild is None:
            return None

        configured = None
        try:
            configured = bot_module.get_configured_voice_channel(guild)
        except Exception:
            pass

        target = configured or target_channel
        if target is None:
            vc = guild.voice_client
            return vc if vc and vc.is_connected() else None

        lock = _reconnect_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            vc = guild.voice_client
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id == target.id:
                    return vc
                try:
                    await vc.move_to(target)
                    log(f"🔊 Voice ย้ายไปใช้งานชั่วคราว: {guild.name} -> {target.name}")
                    return vc
                except Exception as exc:
                    log(f"⚠️ Voice move failed ({guild.name}): {exc!r}")
                    return None

            now = time.monotonic()
            last_attempt = _last_voice_connect_attempt.get(guild.id, 0.0)
            if now - last_attempt < VOICE_RECONNECT_COOLDOWN:
                log(f"⏳ Voice connect cooldown: {guild.name}")
                return None

            _last_voice_connect_attempt[guild.id] = now
            try:
                vc = await target.connect(reconnect=False, timeout=VOICE_CONNECT_TIMEOUT)
                log(f"🔊 Voice on-demand connect สำเร็จ: {guild.name} -> {target.name}")
                return vc
            except discord.ClientException as exc:
                existing = guild.voice_client
                if existing and existing.is_connected():
                    return existing
                log(f"⚠️ Voice client ยังไม่พร้อม ({guild.name}): {exc!r}")
            except Exception as exc:
                log(f"❌ Voice on-demand connect failed ({guild.name}): {exc!r}")
            return None

    bot_module.ensure_voice_runtime = ensure_voice
    bot_module.ensure_configured_voice = ensure_voice

    async def disconnect_after_tts(guild, reason="TTS finished"):
        """Leave Voice after a TTS operation. Do not leave a persistent connection."""
        if guild is None:
            return
        vc = guild.voice_client
        if not vc:
            return
        try:
            if vc.is_playing():
                vc.stop()
            if vc.is_connected():
                await vc.disconnect(force=False)
                log(f"🔌 Voice disconnected: {guild.name} ({reason})")
        except Exception as exc:
            log(f"⚠️ Voice disconnect failed ({guild.name}): {exc!r}")

    bot_module.disconnect_voice_after_tts = disconnect_after_tts

    async def ensure_notification_tasks_started():
        """Start existing notification loops only after command sync is complete."""
        if hasattr(bot_module, "open_runtime_gate"):
            bot_module.open_runtime_gate()

        task_names = (
            "check_boss_notifications",
            "check_bf_notifications",
            "check_library_boss_notifications",
        )
        started = []
        for name in task_names:
            loop_obj = getattr(bot_module, name, None)
            if loop_obj is None:
                log(f"⚠️ Notification task not found: {name}")
                continue
            try:
                if not loop_obj.is_running():
                    loop_obj.start()
                    if loop_obj.is_running():
                        started.append(name)
            except RuntimeError as exc:
                log(f"⚠️ Notification task start skipped ({name}): {exc!r}")
            except Exception as exc:
                log(f"❌ Notification task start failed ({name}): {exc!r}")

        if started:
            log("🟢 Notification tasks started: " + ", ".join(started))
        return started

    async def notification_watchdog():
        """Watch notification loops only. Never auto-connect Voice."""
        task_names = (
            "check_boss_notifications",
            "check_bf_notifications",
            "check_library_boss_notifications",
        )
        logged_missing = set()
        while True:
            try:
                await asyncio.sleep(30)
                if not bot_module.bot.is_ready():
                    continue
                await ensure_notification_tasks_started()
                for name in task_names:
                    loop_obj = getattr(bot_module, name, None)
                    if loop_obj is None:
                        if name not in logged_missing:
                            log(f"⚠️ Notification task not found: {name}")
                            logged_missing.add(name)
                        continue
                    try:
                        if not loop_obj.is_running():
                            log(f"🔄 Restarting notification task: {name}")
                            loop_obj.start()
                            if loop_obj.is_running():
                                log(f"🟢 Notification task restarted: {name}")
                    except RuntimeError as exc:
                        log(f"⚠️ Notification task restart skipped ({name}): {exc!r}")
                    except Exception as exc:
                        log(f"❌ Notification watchdog failed ({name}): {exc!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"⚠️ Notification watchdog loop error: {exc!r}")

    async def start_voice_watchdog():
        """Compatibility entry point: notification watchdog only; Voice is on-demand."""
        global _notification_watchdog_task
        await ensure_notification_tasks_started()
        if _notification_watchdog_task is None or _notification_watchdog_task.done():
            _notification_watchdog_task = asyncio.create_task(
                notification_watchdog(),
                name="skynet-notification-watchdog"
            )
            log("🟢 Notification watchdog started (Voice is ON-DEMAND)")
        return _notification_watchdog_task

    async def patched_speak_in_guild(
        guild,
        text_th=None,
        text_en=None,
        text_ko=None,
        target_channel=None,
    ):
        """Connect -> generate/play TTS -> disconnect. Returns True when audio played."""
        if guild is None:
            return False

        actual = []

        def language_enabled(attribute, default=True):
            value = getattr(bot_module, attribute, default)
            parser = getattr(bot_module, "parse_bool", None)
            return parser(value, default) if callable(parser) else bool(value)

        if language_enabled("tts_th_enabled", True) and text_th:
            actual.append(("th", text_th, getattr(bot_module, "VOICE_THAI", "th-TH-PremwadeeNeural"), "-20%", "+10Hz"))
        if language_enabled("tts_en_enabled", True) and text_en:
            actual.append(("en", text_en, getattr(bot_module, "VOICE_ENG", "en-US-AriaNeural"), "-10%", "+0Hz"))
        if language_enabled("tts_ko_enabled", True) and text_ko:
            actual.append(("ko", text_ko, getattr(bot_module, "VOICE_KOR", "ko-KR-SunHiNeural"), "-10%", "+0Hz"))

        if not actual:
            log("⚠️ TTS skipped: ไม่มีภาษาเปิดใช้งานหรือไม่มีข้อความ")
            return False

        vc = await ensure_voice(guild, target_channel=target_channel)
        if not vc or not vc.is_connected():
            log(f"❌ TTS skipped: ไม่สามารถเชื่อมต่อ Voice ของ {guild.name}")
            return False

        lock = _voice_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            unique_id = uuid.uuid4().hex
            files = []
            played_any = False
            try:
                # edge-tts 7.x manages its own HTTP session.
                for lang, text, voice, rate, pitch in actual:
                    filename = f"temp_tts_{lang}_{guild.id}_{unique_id}.mp3"
                    try:
                        communicator = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                        await communicator.save(filename)
                        if os.path.exists(filename) and os.path.getsize(filename) > 0:
                            files.append((lang, filename))
                            log(f"🔊 TTS สร้างไฟล์สำเร็จ: {lang} ({guild.name})")
                        else:
                            log(f"❌ TTS ได้ไฟล์ว่าง: {lang} ({guild.name})")
                    except Exception as exc:
                        log(f"❌ สร้าง TTS ไม่สำเร็จ ({lang}): {exc!r}")

                if not files:
                    return False

                for index, (lang, filename) in enumerate(files):
                    if not vc.is_connected():
                        log(f"❌ หยุดเล่น TTS: Voice หลุดก่อนเล่น {lang} ({guild.name})")
                        break

                    if vc.is_playing():
                        vc.stop()
                        await asyncio.sleep(0.2)

                    finished = asyncio.Event()
                    loop = asyncio.get_running_loop()

                    def after_playing(error, event=finished, language=lang):
                        if error:
                            log(f"❌ เล่น TTS {language} ผิดพลาดใน {guild.name}: {error!r}")
                        loop.call_soon_threadsafe(event.set)

                    try:
                        source = discord.FFmpegPCMAudio(
                            filename,
                            executable=bot_module.get_ffmpeg_path(),
                            before_options="-loglevel error",
                            options="-vn",
                        )
                        vc.play(source, after=after_playing)
                        log(f"▶️ กำลังเล่น TTS: {lang} -> {guild.name}")
                        try:
                            await asyncio.wait_for(finished.wait(), timeout=TTS_PLAY_TIMEOUT)
                            played_any = True
                        except asyncio.TimeoutError:
                            log(f"⚠️ TTS timeout ใน {guild.name}")
                            if vc.is_playing():
                                vc.stop()
                    except Exception as exc:
                        log(f"❌ เล่นเสียง TTS ไม่สำเร็จใน {guild.name}: {exc!r}")

                    if index < len(files) - 1:
                        await asyncio.sleep(0.4)
            finally:
                for _, filename in files:
                    try:
                        if os.path.exists(filename):
                            os.remove(filename)
                    except Exception:
                        pass
                await disconnect_after_tts(guild, reason="TTS finished")

            return played_any

    bot_module.ensure_voice_runtime = ensure_voice
    bot_module.start_voice_watchdog = start_voice_watchdog
    bot_module.ensure_notification_tasks_started = ensure_notification_tasks_started
    bot_module.speak_in_guild = patched_speak_in_guild

    # Replace /setvoice with an ON-DEMAND version. The old command connected
    # immediately and therefore kept Render's Voice connection alive 24/7.
    try:
        old_setvoice = bot_module.bot.tree.get_command("setvoice")
        if old_setvoice is not None:
            bot_module.bot.tree.remove_command("setvoice")

        @bot_module.bot.tree.command(
            name="setvoice",
            description="จำห้อง Voice สำหรับ Boss TTS แบบ On-Demand",
        )
        @bot_module.has_allowed_role()
        @app_commands.describe(
            channel="ห้อง Voice ที่ต้องการให้บอทใช้ (เว้นว่าง = ห้องที่คุณอยู่)"
        )
        async def setvoice_on_demand(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception as exc:
                log(f"❌ /setvoice defer failed: {exc!r}")
                return

            try:
                target = channel
                if target is None:
                    user_voice = getattr(interaction.user, "voice", None)
                    target = user_voice.channel if user_voice else None
                if target is None:
                    await interaction.followup.send(
                        "❌ กรุณาเข้าห้อง Voice ก่อน หรือเลือกห้อง Voice ใน /setvoice",
                        ephemeral=True,
                    )
                    return

                guild_id = interaction.guild.id
                bot_module.voice_config[str(guild_id)] = {
                    "guild_id": guild_id,
                    "voice_channel_id": int(target.id),
                    "channel_name": target.name,
                    "enabled": True,
                    "updated_by": str(interaction.user.id),
                    "updated_at": bot_module.datetime.now(bot_module.TZ_THAI).isoformat(),
                }

                await asyncio.wait_for(bot_module.save_voice_config(), timeout=8)
                await interaction.followup.send(
                    f"🔊 บันทึกห้อง Voice **{target.name}** แล้ว\n"
                    f"ID: `{target.id}`\n"
                    "⏸️ บอทจะยังไม่เข้าห้องตอนนี้\n"
                    "▶️ เมื่อมี Notification/TTS จะเข้า Voice → พูด → ออกอัตโนมัติ",
                    ephemeral=True,
                )
                log(f"🔊 /setvoice saved ON-DEMAND: {interaction.guild.name} -> {target.name}")
            except Exception as exc:
                log(f"❌ /setvoice on-demand failed: {exc!r}")
                try:
                    await interaction.followup.send(
                        f"❌ /setvoice เกิดข้อผิดพลาด: `{exc}`",
                        ephemeral=True,
                    )
                except Exception:
                    pass

        log("🟢 Voice mode: ON-DEMAND (setvoice saves only; TTS connects/disconnects automatically)")
    except Exception as exc:
        log(f"⚠️ Could not replace /setvoice with on-demand version: {exc!r}")

    # Additive Admin/Ban + startup gate remains installed after Voice functions.
    try:
        import admin_notification_patch
        admin_notification_patch.install(bot_module, log)
        log("🛡️ Admin/notification patch loaded")
    except Exception as exc:
        log(f"⚠️ Admin/notification patch unavailable: {exc!r}")

    return ensure_voice
