"""Runtime Voice/TTS patch for SKYNET.

This module is intentionally kept outside bot.py so the existing Firebase,
Dashboard, slash commands and boss scheduler remain unchanged. start.py
installs the patch after importing bot.py.

Important compatibility point:
edge-tts 7.2.x Communicate does NOT accept a ``session=`` keyword. The
original bot passed an aiohttp ClientSession, which caused every TTS request
to fail before an MP3 file was created.
"""

import asyncio
import os
import time
import uuid

import discord
import edge_tts


_voice_locks = {}
_reconnect_locks = {}


def install(bot_module, log):
    """Install the voice/TTS runtime patch into the existing bot module."""

    async def ensure_voice(guild, target_channel=None):
        if guild is None:
            return None

        configured = None
        try:
            configured = bot_module.get_configured_voice_channel(guild)
        except Exception:
            configured = None

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
                    log(f"🔊 Voice ย้ายกลับ: {guild.name} -> {target.name}")
                    return vc
                except Exception as exc:
                    log(f"⚠️ Voice move failed ({guild.name}): {exc!r}")

            # A stale voice client can remain attached after a 1006 close.
            # Remove only the stale voice connection; do not touch Gateway.
            stale = guild.voice_client
            if stale:
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    pass

            try:
                vc = await target.connect(reconnect=True, timeout=20)
                log(f"🔊 Voice reconnect สำเร็จ: {guild.name} -> {target.name}")
                return vc
            except discord.ClientException as exc:
                # discord.py may already be reconnecting internally.
                vc = guild.voice_client
                if vc and vc.is_connected():
                    return vc
                log(f"⚠️ Voice client ยังไม่พร้อม ({guild.name}): {exc!r}")
            except Exception as exc:
                log(f"❌ Voice connect failed ({guild.name}): {exc!r}")
            return None

    async def patched_speak_in_guild(
        guild,
        text_th=None,
        text_en=None,
        text_ko=None,
        target_channel=None,
    ):
        """Generate and play enabled TTS languages in the configured Voice channel."""
        if guild is None:
            return

        actual = []
        if getattr(bot_module, "tts_th_enabled", True) and text_th:
            actual.append(("th", text_th, getattr(bot_module, "VOICE_THAI", "th-TH-PremwadeeNeural"), "-20%", "+10Hz"))
        if getattr(bot_module, "tts_en_enabled", True) and text_en:
            actual.append(("en", text_en, getattr(bot_module, "VOICE_ENG", "en-US-AriaNeural"), "-10%", "+0Hz"))
        if getattr(bot_module, "tts_ko_enabled", True) and text_ko:
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
                # edge-tts 7.2.x manages its own aiohttp session.
                # Do NOT pass session=... to Communicate.
                for lang, text, voice, rate, pitch in actual:
                    filename = f"temp_tts_{lang}_{guild.id}_{unique_id}.mp3"
                    try:
                        communicator = edge_tts.Communicate(
                            text,
                            voice,
                            rate=rate,
                            pitch=pitch,
                        )
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
                    # Re-check the Voice connection before every language.
                    vc = await ensure_voice(guild, target_channel=target_channel)
                    if not vc or not vc.is_connected():
                        log(f"❌ หยุดเล่น TTS: Voice หลุด ({guild.name})")
                        break

                    if vc.is_playing():
                        vc.stop()
                        await asyncio.sleep(0.15)

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
    bot_module.speak_in_guild = patched_speak_in_guild

    return ensure_voice
