"""Runtime Voice/TTS patch for SKYNET.

Keeps Firebase, Dashboard, slash commands and boss scheduling in bot.py.
start.py installs this module after importing bot.py.

This patch gives the configured Voice connection a single runtime owner and
leaves Discord Gateway lifecycle ownership in start.py. It also starts the
existing notification loops after command sync so notification scheduling is
not lost during the on_ready/sync race.

Important: a Discord Voice WebSocket close (for example 4006) is not the same
thing as the main Discord Gateway connection. discord.py's VoiceClient owns
its reconnect=True lifecycle. The watchdog therefore does NOT force
 disconnect/reconnect on every failed poll. It waits for the native voice
reconnect first and only resets a stale client after a longer grace period.
"""

import asyncio
import os
import time
import uuid

import discord
import edge_tts

_voice_locks = {}
_reconnect_locks = {}
_last_voice_connect_attempt = {}
_voice_disconnected_since = {}
_watchdog_task = None
_notification_watchdog_task = None

VOICE_RECONNECT_COOLDOWN = 45.0
VOICE_STALE_RESET_AFTER = 120.0


def install(bot_module, log):
    """Install Voice/TTS runtime functions without changing data ownership."""

    async def ensure_voice(guild, target_channel=None):
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

            # Healthy connection: do not reconnect or recreate it.
            if vc and vc.is_connected():
                _voice_disconnected_since.pop(guild.id, None)
                if vc.channel and vc.channel.id == target.id:
                    return vc
                try:
                    await vc.move_to(target)
                    log(f"🔊 Voice ย้ายกลับ: {guild.name} -> {target.name}")
                    return vc
                except Exception as exc:
                    log(f"⚠️ Voice move failed ({guild.name}): {exc!r}")
                    return None

            now = time.monotonic()

            # A VoiceClient that exists but is temporarily disconnected is
            # normally already reconnecting internally because connect(...,
            # reconnect=True) was used. Do NOT force-disconnect it immediately;
            # doing so creates the 4006 -> reconnect -> disconnect loop seen in
            # the Render logs.
            if vc is not None:
                disconnected_at = _voice_disconnected_since.setdefault(guild.id, now)
                age = now - disconnected_at

                if age < VOICE_STALE_RESET_AFTER:
                    return None

                last_attempt = _last_voice_connect_attempt.get(guild.id, 0.0)
                if now - last_attempt < VOICE_RECONNECT_COOLDOWN:
                    return None

                # Only after a long grace period do we reset a genuinely stale
                # VoiceClient. This is separate from the Discord Gateway.
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None
                _voice_disconnected_since.pop(guild.id, None)

            last_attempt = _last_voice_connect_attempt.get(guild.id, 0.0)
            if now - last_attempt < VOICE_RECONNECT_COOLDOWN:
                return None

            _last_voice_connect_attempt[guild.id] = now
            try:
                vc = await target.connect(reconnect=True, timeout=20)
                _voice_disconnected_since.pop(guild.id, None)
                log(f"🔊 Voice reconnect สำเร็จ: {guild.name} -> {target.name}")
                return vc
            except discord.ClientException as exc:
                existing = guild.voice_client
                if existing and existing.is_connected():
                    _voice_disconnected_since.pop(guild.id, None)
                    return existing
                _voice_disconnected_since.setdefault(guild.id, now)
                log(f"⚠️ Voice client ยังไม่พร้อม ({guild.name}): {exc!r}")
            except Exception as exc:
                _voice_disconnected_since.setdefault(guild.id, now)
                log(f"❌ Voice connect failed ({guild.name}): {exc!r}")
            return None

    # Make every existing bot.py caller use the same Voice lifecycle.
    bot_module.ensure_voice_runtime = ensure_voice
    bot_module.ensure_configured_voice = ensure_voice

    async def ensure_notification_tasks_started():
        """Start the existing notification loops exactly once.

        bot.py owns the loop implementations. This helper only makes sure the
        already-defined loops are running after Gateway/command sync.
        """
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
                    started.append(name)
            except RuntimeError as exc:
                log(f"⚠️ Notification task start skipped ({name}): {exc!r}")
            except Exception as exc:
                log(f"❌ Notification task start failed ({name}): {exc!r}")
        if started:
            log("🟢 Notification tasks started: " + ", ".join(started))
        return started

    async def voice_watchdog():
        """Keep /setvoice channels connected without touching Discord Gateway."""
        while True:
            try:
                await asyncio.sleep(20)
                if not bot_module.bot.is_ready():
                    continue
                for guild in list(bot_module.bot.guilds):
                    try:
                        configured = bot_module.get_configured_voice_channel(guild)
                        if configured:
                            vc = guild.voice_client
                            if not vc or not vc.is_connected():
                                await ensure_voice(guild, configured)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log(f"⚠️ Voice watchdog failed ({guild.name}): {exc!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"⚠️ Voice watchdog loop error: {exc!r}")

    async def notification_watchdog():
        """Keep the existing bot.py notification loops alive.

        This watchdog never creates a second scheduler. It only restarts a
        Loop object that already exists in bot.py and has stopped unexpectedly.
        """
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
                        running = loop_obj.is_running() if hasattr(loop_obj, "is_running") else False
                        if not running:
                            log(f"🔄 Restarting notification task: {name}")
                            loop_obj.start()
                            log(f"🟢 Notification task restarted: {name}")
                        else:
                            logged_missing.discard(name)
                    except RuntimeError as exc:
                        log(f"⚠️ Notification task restart skipped ({name}): {exc!r}")
                    except Exception as exc:
                        log(f"❌ Notification task watchdog failed ({name}): {exc!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"⚠️ Notification watchdog loop error: {exc!r}")

    async def start_voice_watchdog():
        global _watchdog_task, _notification_watchdog_task
        await ensure_notification_tasks_started()
        if _watchdog_task is None or _watchdog_task.done():
            _watchdog_task = asyncio.create_task(voice_watchdog(), name="skynet-voice-watchdog")
            log("🟢 Voice watchdog started")
        if _notification_watchdog_task is None or _notification_watchdog_task.done():
            _notification_watchdog_task = asyncio.create_task(
                notification_watchdog(), name="skynet-notification-watchdog"
            )
            log("🟢 Notification watchdog started")
        return _watchdog_task

    async def patched_speak_in_guild(guild, text_th=None, text_en=None, text_ko=None, target_channel=None):
        """Generate and play enabled TTS languages in the configured Voice channel."""
        if guild is None:
            return

        actual = []

        def language_enabled(attribute, default=True):
            value = getattr(bot_module, attribute, default)
            parser = getattr(bot_module, "parse_bool", None)
            if callable(parser):
                return parser(value, default)
            return bool(value)

        if language_enabled("tts_th_enabled", True) and text_th:
            actual.append(("th", text_th, getattr(bot_module, "VOICE_THAI", "th-TH-PremwadeeNeural"), "-20%", "+10Hz"))
        if language_enabled("tts_en_enabled", True) and text_en:
            actual.append(("en", text_en, getattr(bot_module, "VOICE_ENG", "en-US-AriaNeural"), "-10%", "+0Hz"))
        if language_enabled("tts_ko_enabled", True) and text_ko:
            actual.append(("ko", text_ko, getattr(bot_module, "VOICE_KOR", "ko-KR-SunHiNeural"), "-10%", "+0Hz"))

        if not actual:
            log("⚠️ TTS skipped: ไม่มีภาษาเปิดใช้งานหรือไม่มีข้อความ")
            return

        vc = await ensure_voice(guild, target_channel=target_channel)
        if not vc or not vc.is_connected():
            log(f"❌ TTS skipped: ไม่สามารถเชื่อมต่อ Voice ของ {guild.name}")
            return

        lock = _voice_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            unique_id = uuid.uuid4().hex
            files = []
            try:
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
                    return

                for index, (lang, filename) in enumerate(files):
                    # Re-use the healthy connection. If Discord is in the
                    # middle of a native voice reconnect, give it a few seconds
                    # instead of creating another VoiceClient.
                    if not vc.is_connected():
                        for _ in range(10):
                            await asyncio.sleep(1)
                            if vc.is_connected():
                                break
                    if not vc.is_connected():
                        log(f"❌ หยุดเล่น TTS: Voice ยังไม่กลับมา ({guild.name})")
                        break

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
                            await asyncio.wait_for(finished.wait(), timeout=90)
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

    bot_module.ensure_voice_runtime = ensure_voice
    bot_module.start_voice_watchdog = start_voice_watchdog
    bot_module.ensure_notification_tasks_started = ensure_notification_tasks_started
    bot_module.speak_in_guild = patched_speak_in_guild
    return ensure_voice
