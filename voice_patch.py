"""Runtime Voice/TTS patch for SKYNET.

Voice is ON-DEMAND and supports multiple configured Voice channels per guild:
    /setvoice -> adds/remembers a channel only (does not overwrite old channels)
    notification / notice / tts -> speaks in every configured channel that has humans
    TTS finishes in each channel -> disconnects from that channel

A Discord guild can only have one Voice connection at a time, so multiple
channels inside the same guild are handled sequentially. Different guilds can
still be handled independently by the existing notification tasks.

Firebase, Dashboard, boss scheduling and slash-command data ownership remain in
bot.py. Discord Gateway lifecycle remains owned by start.py.
"""
import asyncio
import os
import time
import uuid

import discord
from discord import app_commands

_voice_locks = {}
_reconnect_locks = {}
_last_voice_connect_attempt = {}
_notification_watchdog_task = None

VOICE_RECONNECT_COOLDOWN = 3.0
VOICE_CONNECT_TIMEOUT = 20.0
TTS_PLAY_TIMEOUT = 90.0


def install(bot_module, log):
    """Install on-demand multi-channel Voice/TTS runtime without replacing
    Firebase, Dashboard, boss scheduling or Gateway lifecycle.
    """

    def _raw_voice_config(guild):
        if guild is None:
            return None
        cfg = getattr(bot_module, "voice_config", {}).get(str(guild.id))
        if isinstance(cfg, dict):
            return cfg
        if isinstance(cfg, list):
            return {"channels": cfg, "enabled": True}
        return None

    def _configured_channel_ids(guild):
        """Return unique configured Voice channel IDs, including legacy format."""
        cfg = _raw_voice_config(guild)
        if not cfg or not bot_module.parse_bool(cfg.get("enabled", True), True):
            return []

        ids = []
        raw_channels = cfg.get("channels", [])
        if isinstance(raw_channels, dict):
            raw_channels = list(raw_channels.values())
        if not isinstance(raw_channels, list):
            raw_channels = []

        for item in raw_channels:
            if isinstance(item, dict):
                channel_id = item.get("voice_channel_id", item.get("channel_id", item.get("id")))
            else:
                channel_id = item
            try:
                channel_id = int(channel_id)
            except (TypeError, ValueError):
                continue
            if channel_id not in ids:
                ids.append(channel_id)

        legacy_id = cfg.get("voice_channel_id")
        try:
            legacy_id = int(legacy_id)
        except (TypeError, ValueError):
            legacy_id = None
        if legacy_id and legacy_id not in ids:
            ids.insert(0, legacy_id)

        return ids

    async def get_configured_voice_channels(guild):
        """Resolve all configured Voice channels for a guild."""
        channels = []
        for channel_id in _configured_channel_ids(guild):
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except Exception as exc:
                    log(f"⚠️ ไม่พบ Voice channel {channel_id} ใน {guild.name}: {exc!r}")
                    continue
            if not isinstance(channel, discord.VoiceChannel):
                log(f"⚠️ ข้าม channel {channel_id} ใน {guild.name}: ไม่ใช่ VoiceChannel")
                continue
            channels.append(channel)
        return channels

    def get_configured_voice_channel(guild):
        """Legacy synchronous helper: return the first configured channel."""
        if guild is None:
            return None
        ids = _configured_channel_ids(guild)
        if not ids:
            return None
        channel = guild.get_channel(ids[0])
        return channel if isinstance(channel, discord.VoiceChannel) else None

    bot_module.get_configured_voice_channels = get_configured_voice_channels
    bot_module.get_configured_voice_channel = get_configured_voice_channel

    async def _restore_multi_voice_config_after_load():
        """Restore the new channels[] field after bot.py's legacy loader.

        Older bot.py versions normalize voice_config to one voice_channel_id.
        The Firebase payload is read once more so multi-channel configuration is
        not lost after a Render restart.
        """
        try:
            data = await asyncio.to_thread(bot_module.db.reference("voice_config").get)
        except Exception as exc:
            log(f"⚠️ Multi-Voice config restore failed: {exc!r}")
            return

        if not isinstance(data, dict):
            return

        for gid, raw_cfg in data.items():
            if not isinstance(raw_cfg, dict):
                continue
            raw_channels = raw_cfg.get("channels")
            if not isinstance(raw_channels, (list, dict)):
                continue

            cfg = getattr(bot_module, "voice_config", {}).get(str(gid), {})
            if not isinstance(cfg, dict):
                cfg = {}

            channels = []
            items = list(raw_channels.values()) if isinstance(raw_channels, dict) else raw_channels
            for item in items:
                if isinstance(item, dict):
                    cid = item.get("voice_channel_id", item.get("channel_id", item.get("id")))
                    name = item.get("channel_name", "")
                    entry = dict(item)
                else:
                    cid = item
                    name = ""
                    entry = {"voice_channel_id": item}
                try:
                    cid = int(cid)
                except (TypeError, ValueError):
                    continue
                entry["voice_channel_id"] = cid
                if name:
                    entry["channel_name"] = name
                if not any(int(e.get("voice_channel_id", 0)) == cid for e in channels):
                    channels.append(entry)

            if channels:
                cfg["channels"] = channels
                cfg["voice_channel_id"] = channels[0]["voice_channel_id"]
                cfg["channel_name"] = channels[0].get("channel_name", cfg.get("channel_name", ""))
                cfg["guild_id"] = int(gid) if str(gid).isdigit() else cfg.get("guild_id", gid)
                cfg["enabled"] = bot_module.parse_bool(raw_cfg.get("enabled", cfg.get("enabled", True)), True)
                bot_module.voice_config[str(gid)] = cfg

        configured_count = sum(
            len(cfg.get("channels", []))
            for cfg in getattr(bot_module, "voice_config", {}).values()
            if isinstance(cfg, dict) and isinstance(cfg.get("channels", []), list)
        )
        log(f"🔊 Multi-Voice config restored: {configured_count} channel(s)")

    original_load_voice_config = getattr(bot_module, "load_voice_config", None)
    if original_load_voice_config is not None and not getattr(original_load_voice_config, "_multi_voice_patched", False):
        async def patched_load_voice_config():
            await original_load_voice_config()
            await _restore_multi_voice_config_after_load()

        patched_load_voice_config._multi_voice_patched = True
        bot_module.load_voice_config = patched_load_voice_config

    async def ensure_voice(guild, target_channel=None):
        """Connect only when a TTS operation needs Voice.

        If target_channel is supplied it is authoritative. Otherwise the first
        configured channel is used for backward-compatible callers.
        """
        if guild is None:
            return None

        target = target_channel or get_configured_voice_channel(guild)
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
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass

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
                if existing and existing.is_connected() and existing.channel and existing.channel.id == target.id:
                    return existing
                log(f"⚠️ Voice client ยังไม่พร้อม ({guild.name}): {exc!r}")
            except Exception as exc:
                log(f"❌ Voice on-demand connect failed ({guild.name} -> {target.name}): {exc!r}")
            return None

    bot_module.ensure_voice_runtime = ensure_voice
    bot_module.ensure_configured_voice = ensure_voice

    async def disconnect_after_tts(guild, reason="TTS finished"):
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
        global _notification_watchdog_task
        await ensure_notification_tasks_started()
        if _notification_watchdog_task is None or _notification_watchdog_task.done():
            _notification_watchdog_task = asyncio.create_task(
                notification_watchdog(),
                name="skynet-notification-watchdog",
            )
            log("🟢 Notification watchdog started (Voice is ON-DEMAND)")
        return _notification_watchdog_task

    async def _play_files_in_channel(guild, channel, files):
        """Connect to one channel, play all enabled languages, then disconnect."""
        vc = await ensure_voice(guild, target_channel=channel)
        if not vc or not vc.is_connected():
            log(f"❌ TTS skipped: ไม่สามารถเชื่อมต่อ {guild.name} -> {channel.name}")
            return False

        played_any = False
        try:
            for index, (lang, filename) in enumerate(files):
                if not vc.is_connected():
                    log(f"❌ Voice หลุดก่อนเล่น {lang}: {guild.name} -> {channel.name}")
                    break

                if vc.is_playing():
                    vc.stop()
                    await asyncio.sleep(0.2)

                finished = asyncio.Event()
                loop = asyncio.get_running_loop()

                def after_playing(error, event=finished, language=lang):
                    if error:
                        log(f"❌ เล่น TTS {language} ผิดพลาดใน {guild.name} -> {channel.name}: {error!r}")
                    loop.call_soon_threadsafe(event.set)

                try:
                    source = discord.FFmpegPCMAudio(
                        filename,
                        executable=bot_module.get_ffmpeg_path(),
                        before_options="-loglevel error",
                        options="-vn",
                    )
                    vc.play(source, after=after_playing)
                    log(f"▶️ กำลังเล่น TTS: {lang} -> {guild.name} -> {channel.name}")
                    try:
                        await asyncio.wait_for(finished.wait(), timeout=TTS_PLAY_TIMEOUT)
                        played_any = True
                    except asyncio.TimeoutError:
                        log(f"⚠️ TTS timeout: {guild.name} -> {channel.name}")
                        if vc.is_playing():
                            vc.stop()
                except Exception as exc:
                    log(f"❌ เล่นเสียง TTS ไม่สำเร็จใน {guild.name} -> {channel.name}: {exc!r}")

                if index < len(files) - 1:
                    await asyncio.sleep(0.4)
        finally:
            await disconnect_after_tts(guild, reason=f"TTS finished: {channel.name}")

        return played_any

    async def patched_speak_in_guild(
        guild,
        text_th=None,
        text_en=None,
        text_ko=None,
        target_channel=None,
    ):
        """Speak in every configured occupied Voice channel, sequentially."""
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

        unique_id = uuid.uuid4().hex
        files = []
        try:
            for lang, text, voice, rate, pitch in actual:
                filename = f"temp_tts_{lang}_{guild.id}_{unique_id}.mp3"
                try:
                    import edge_tts
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

            lock = _voice_locks.setdefault(guild.id, asyncio.Lock())
            async with lock:
                if target_channel is not None:
                    channels = [target_channel]
                else:
                    configured = await get_configured_voice_channels(guild)
                    channels = [
                        ch for ch in configured
                        if any(not getattr(member, "bot", False) for member in getattr(ch, "members", []))
                    ]

                if not channels:
                    log(f"⏭️ ไม่มีห้อง Voice ที่มีสมาชิกสำหรับ TTS: {guild.name}")
                    return False

                log(
                    f"🔊 TTS targets ({guild.name}): "
                    + ", ".join(f"{ch.name}({ch.id})" for ch in channels)
                )

                played_any = False
                for channel in channels:
                    if target_channel is None and not any(
                        not getattr(member, "bot", False) for member in getattr(channel, "members", [])
                    ):
                        log(f"⏭️ ข้ามห้องที่ไม่มีคนแล้ว: {guild.name} -> {channel.name}")
                        continue
                    if await _play_files_in_channel(guild, channel, files):
                        played_any = True

                return played_any
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

    try:
        old_setvoice = bot_module.bot.tree.get_command("setvoice")
        if old_setvoice is not None:
            bot_module.bot.tree.remove_command("setvoice")

        @bot_module.bot.tree.command(
            name="setvoice",
            description="เพิ่มห้อง Voice สำหรับ Boss TTS แบบ On-Demand",
        )
        @bot_module.has_allowed_role()
        @app_commands.describe(
            channel="ห้อง Voice ที่ต้องการเพิ่ม (เว้นว่าง = ห้องที่คุณอยู่)"
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
                cfg = _raw_voice_config(interaction.guild) or {}
                raw_channels = cfg.get("channels", [])
                if isinstance(raw_channels, dict):
                    raw_channels = list(raw_channels.values())
                if not isinstance(raw_channels, list):
                    raw_channels = []

                entries = []
                existing_ids = set()
                for item in raw_channels:
                    if isinstance(item, dict):
                        cid = item.get("voice_channel_id", item.get("channel_id", item.get("id")))
                        try:
                            cid_int = int(cid)
                        except (TypeError, ValueError):
                            continue
                        if cid_int in existing_ids:
                            continue
                        existing_ids.add(cid_int)
                        entries.append(item)
                    else:
                        try:
                            cid_int = int(item)
                        except (TypeError, ValueError):
                            continue
                        if cid_int not in existing_ids:
                            existing_ids.add(cid_int)
                            entries.append({"voice_channel_id": cid_int})

                legacy_id = cfg.get("voice_channel_id")
                try:
                    legacy_id = int(legacy_id)
                except (TypeError, ValueError):
                    legacy_id = None
                if legacy_id and legacy_id not in existing_ids:
                    existing_ids.add(legacy_id)
                    legacy_channel = interaction.guild.get_channel(legacy_id)
                    entries.insert(0, {
                        "voice_channel_id": legacy_id,
                        "channel_name": getattr(legacy_channel, "name", cfg.get("channel_name", "")),
                    })

                if int(target.id) not in existing_ids:
                    entries.append({
                        "voice_channel_id": int(target.id),
                        "channel_name": target.name,
                        "added_by": str(interaction.user.id),
                        "updated_at": bot_module.datetime.now(bot_module.TZ_THAI).isoformat(),
                    })
                    added = True
                else:
                    added = False

                cfg["channels"] = entries
                cfg["voice_channel_id"] = entries[0].get("voice_channel_id") if entries else int(target.id)
                cfg["channel_name"] = entries[0].get("channel_name", target.name) if entries else target.name
                cfg["guild_id"] = guild_id
                cfg["enabled"] = True
                cfg["updated_by"] = str(interaction.user.id)
                cfg["updated_at"] = bot_module.datetime.now(bot_module.TZ_THAI).isoformat()
                bot_module.voice_config[str(guild_id)] = cfg

                await asyncio.wait_for(bot_module.save_voice_config(), timeout=8)

                channel_names = []
                for entry in entries:
                    cid = entry.get("voice_channel_id") if isinstance(entry, dict) else entry
                    ch = interaction.guild.get_channel(int(cid)) if cid else None
                    channel_names.append(getattr(ch, "name", str(cid)))

                action = "เพิ่ม" if added else "มีอยู่แล้ว"
                await interaction.followup.send(
                    f"🔊 {action}ห้อง Voice **{target.name}** แล้ว\n"
                    f"📋 ห้องที่ตั้งค่าไว้ทั้งหมด: **{len(entries)}**\n"
                    + "\n".join(f"• {name}" for name in channel_names)
                    + "\n\n⏸️ บอทยังไม่เข้า Voice ตอนนี้\n"
                      "▶️ เมื่อมี Notification/TTS จะพูดในทุกห้องที่มีคน แล้วออกอัตโนมัติ",
                    ephemeral=True,
                )
                log(
                    f"🔊 /setvoice saved ON-DEMAND MULTI: {interaction.guild.name} -> {target.name} "
                    f"(configured={len(entries)})"
                )
            except Exception as exc:
                log(f"❌ /setvoice on-demand failed: {exc!r}")
                try:
                    await interaction.followup.send(
                        f"❌ /setvoice เกิดข้อผิดพลาด: `{exc}`",
                        ephemeral=True,
                    )
                except Exception:
                    pass

        log("🟢 Voice mode: ON-DEMAND MULTI-CHANNEL (setvoice adds channels; TTS connects/speaks/disconnects)")
    except Exception as exc:
        log(f"⚠️ Could not replace /setvoice with on-demand multi-channel version: {exc!r}")

    try:
        import admin_notification_patch
        admin_notification_patch.install(bot_module, log)
        log("🛡️ Admin/notification patch loaded")
    except Exception as exc:
        log(f"⚠️ Admin/notification patch unavailable: {exc!r}")

    return ensure_voice
