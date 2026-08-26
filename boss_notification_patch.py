"""Boss notification reliability patch for SKYNET.

This patch owns the boss notification state machine after start.py imports it.
It deliberately does NOT own the Discord Gateway lifecycle or Voice connection
lifecycle. voice_patch.py remains responsible for on-demand multi-channel TTS.

Pipeline:
    /kill -> local schedule -> Firebase -> checker -> advance notice -> spawn
    notice -> TTS in every configured occupied voice channel.

Important fixes:
- noticeMinutes stored on the schedule is the source of truth for each boss.
- The checker runs every 5 seconds, so it cannot miss a narrow notice window.
- Notification flags are updated with Firebase child updates, not a full-root
  overwrite, preventing the Firebase listener from erasing other schedules.
- Text notification and Voice notification have separate state flags, so a
  temporary Voice failure is retried without duplicating the text notification.
- /kill gets an immediate diagnostic check.
- A small retry cooldown prevents repeated Voice connection attempts.
"""

import asyncio
import time
from discord.ext import tasks

_health_task = None
_trigger_lock = None
_voice_attempts = {}

CHECK_INTERVAL = 5.0
VOICE_RETRY_COOLDOWN = 30.0


def _notice_minutes(bot_module, boss_name, data):
    raw = data.get("noticeMinutes")
    if raw is None:
        raw = data.get("notice_minutes")
    if raw is None:
        raw = bot_module.get_boss_advance_notice_seconds(boss_name) / 60
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return max(1, int(bot_module.get_boss_advance_notice_seconds(boss_name) / 60))


def _event_flag(data, snake_name, firebase_name):
    value = data.get(snake_name)
    if value is None:
        value = data.get(firebase_name, False)
    return bool(value)


def _channel_list(bot_module, guild, preferred_text_channel_id=None):
    channels = []
    if preferred_text_channel_id:
        try:
            channel = bot_module.bot.get_channel(int(preferred_text_channel_id))
            if channel is not None:
                channels.append(channel)
        except (TypeError, ValueError):
            pass

    if channels:
        return channels

    live_name = getattr(bot_module, "LIVE_CHANNEL_NAME", "boss-schedule")
    for g in bot_module.bot.guilds:
        channel = bot_module.discord.utils.get(g.text_channels, name=live_name)
        if channel is None:
            channel = g.system_channel or (g.text_channels[0] if g.text_channels else None)
        if channel is not None:
            channels.append(channel)
    return channels


async def _firebase_flag_update(bot_module, boss_name, **flags):
    """Update only notification flags for one boss; never overwrite the root."""
    clean = {}
    for key, value in flags.items():
        clean[key] = bool(value)
    try:
        await asyncio.to_thread(
            bot_module.db.reference(f"boss_schedule/{boss_name}").update,
            clean,
        )
        return True
    except Exception as exc:
        print(f"⚠️ Firebase notification flag update failed ({boss_name}): {exc!r}")
        return False


async def _send_text(bot_module, boss_name, channels, stage, notice_text, spawn_time):
    import discord

    if stage == "advance":
        embed = discord.Embed(
            title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
            description=(
                f"บอส **{boss_name}** จะเกิดในอีก **{notice_text}**!\n"
                f"เวลาเกิด: **{spawn_time.strftime('%H:%M:%S น.')}**"
            ),
            color=discord.Color.gold(),
        )
    else:
        embed = discord.Embed(
            title="⚔️ บอสเกิดแล้ว!",
            description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
            color=discord.Color.green(),
        )

    sent = False
    for channel in channels:
        try:
            guild = getattr(channel, "guild", None)
            mentions = []
            if guild:
                for role_id in getattr(bot_module, "TARGET_ROLE_IDS", []):
                    role = guild.get_role(role_id)
                    if role:
                        mentions.append(role.mention)
            content = " ".join(mentions) or None
            await channel.send(content=content, embed=embed)
            sent = True
        except Exception as exc:
            print(f"❌ ส่งข้อความแจ้งเตือน {stage} ไม่สำเร็จ ({boss_name}): {exc!r}")
    return sent


async def _voice_notify(bot_module, boss_name, stage, notice_text, notice_text_en, notice_text_ko):
    """Speak through the installed on-demand multi-channel Voice patch."""
    now = time.monotonic()
    key = f"{boss_name}:{stage}"
    last = _voice_attempts.get(key, 0.0)
    if now - last < VOICE_RETRY_COOLDOWN:
        return False
    _voice_attempts[key] = now

    spoken_name = bot_module.get_boss_pronunciation(boss_name)
    if stage == "advance":
        th = f"บอส {spoken_name} จะเกิดในอีก {notice_text} ค่ะ"
        en = f"Boss {boss_name} will spawn in {notice_text_en}."
        ko = f"보스 {boss_name}가 {notice_text_ko} 후에 나타납니다."
    else:
        th = f"บอส {spoken_name} เกิดแล้วค่ะ"
        en = f"Boss {boss_name} has spawned."
        ko = f"보스 {boss_name}가 나타났습니다."

    speaker = getattr(bot_module, "speak_in_guild", None)
    if speaker is None:
        print("❌ Voice notification skipped: speak_in_guild is unavailable")
        return False

    success = False
    for guild in bot_module.bot.guilds:
        try:
            result = await asyncio.wait_for(
                speaker(guild, text_th=th, text_en=en, text_ko=ko),
                timeout=180,
            )
            if result is not False:
                success = True
        except asyncio.TimeoutError:
            print(f"⚠️ Voice notification timeout ({stage}): {boss_name} / {guild.name}")
        except Exception as exc:
            print(f"❌ Voice notification failed ({stage}): {boss_name} / {guild.name}: {exc!r}")
    return success


async def _run_checker(bot_module, log, reason="loop"):
    if not bot_module.bot.is_ready():
        return

    now = bot_module.datetime.now(bot_module.TZ_THAI)
    with bot_module.schedule_lock:
        snapshot = {
            boss: dict(data)
            for boss, data in bot_module.boss_schedule.items()
            if isinstance(data, dict)
        }

    if not snapshot:
        if reason != "loop":
            log(f"🔎 Boss notification check ({reason}): schedule=0")
        return

    for boss_name, data in snapshot.items():
        try:
            spawn = bot_module.parse_to_thai_datetime(
                data.get("spawn_time") or data.get("spawnTimeMs")
            )
            if not spawn:
                log(f"⚠️ Boss notification check: {boss_name} has invalid spawn_time")
                continue

            left = (spawn - now).total_seconds()
            notice_min = _notice_minutes(bot_module, boss_name, data)
            notice_seconds = notice_min * 60
            advance_sent = _event_flag(data, "notified_advance", "notifiedNotice")
            spawn_sent = _event_flag(data, "notified_spawn", "notifiedSpawn")
            voice_advance_sent = _event_flag(data, "voice_notice_sent", "voiceNoticeSent")
            voice_spawn_sent = _event_flag(data, "voice_spawn_sent", "voiceSpawnSent")

            if reason != "loop" or 0 < left <= notice_seconds + 15 or left <= 15:
                log(
                    f"🔎 Boss notification check ({reason}): {boss_name} | "
                    f"spawn={spawn.isoformat()} | left={left:.1f}s | "
                    f"notice={notice_min}m | advance={advance_sent} | "
                    f"spawn_sent={spawn_sent} | voice_advance={voice_advance_sent} | "
                    f"voice_spawn={voice_spawn_sent}"
                )

            channels = _channel_list(bot_module, bot_module.bot.guilds[0] if bot_module.bot.guilds else None, data.get("channel_id"))
            notice_text = f"{notice_min} นาที"
            notice_text_en = "1 hour" if notice_min == 60 else f"{notice_min} minutes"
            notice_text_ko = "1시간" if notice_min == 60 else f"{notice_min}분"

            # Advance notification: use the schedule's noticeMinutes, not a hard-coded boss table.
            if 0 < left <= notice_seconds and not advance_sent:
                text_ok = await _send_text(
                    bot_module, boss_name, channels, "advance", notice_text, spawn
                )
                if text_ok:
                    with bot_module.schedule_lock:
                        if boss_name in bot_module.boss_schedule:
                            bot_module.boss_schedule[boss_name]["notified_advance"] = True
                    await _firebase_flag_update(bot_module, boss_name, notifiedNotice=True)
                    advance_sent = True
                    log(f"🟢 Advance notice sent: {boss_name} ({notice_min}m)")

            if 0 < left <= notice_seconds and not voice_advance_sent:
                voice_ok = await _voice_notify(
                    bot_module, boss_name, "advance", notice_text, notice_text_en, notice_text_ko
                )
                if voice_ok:
                    with bot_module.schedule_lock:
                        if boss_name in bot_module.boss_schedule:
                            bot_module.boss_schedule[boss_name]["voice_notice_sent"] = True
                    await _firebase_flag_update(bot_module, boss_name, voiceNoticeSent=True)
                    log(f"🔊 Advance TTS sent: {boss_name}")

            # Spawn notification: never require advance notification to have succeeded.
            if left <= 0 and not spawn_sent:
                text_ok = await _send_text(
                    bot_module, boss_name, channels, "spawn", notice_text, spawn
                )
                if text_ok:
                    with bot_module.schedule_lock:
                        if boss_name in bot_module.boss_schedule:
                            bot_module.boss_schedule[boss_name]["notified_spawn"] = True
                    await _firebase_flag_update(bot_module, boss_name, notifiedSpawn=True)
                    spawn_sent = True
                    log(f"🟢 Spawn notice sent: {boss_name}")

            if left <= 0 and not voice_spawn_sent:
                voice_ok = await _voice_notify(
                    bot_module, boss_name, "spawn", notice_text, notice_text_en, notice_text_ko
                )
                if voice_ok:
                    with bot_module.schedule_lock:
                        if boss_name in bot_module.boss_schedule:
                            bot_module.boss_schedule[boss_name]["voice_spawn_sent"] = True
                    await _firebase_flag_update(bot_module, boss_name, voiceSpawnSent=True)
                    log(f"🔊 Spawn TTS sent: {boss_name}")

        except Exception as exc:
            log(f"❌ Boss notification processing failed ({boss_name}): {exc!r}")


def install(bot_module, log):
    global _health_task, _trigger_lock
    if getattr(bot_module, "_boss_notification_patch_installed", False):
        return
    bot_module._boss_notification_patch_installed = True
    _trigger_lock = asyncio.Lock()

    # Replace the legacy 10-second checker with this 5-second checker before
    # start.py opens the runtime gate. This is safe because the patch is loaded
    # before the Discord Gateway starts.
    old_checker = getattr(bot_module, "check_boss_notifications", None)
    try:
        if old_checker is not None and old_checker.is_running():
            old_checker.cancel()
    except Exception:
        pass

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def reliable_checker():
        await _run_checker(bot_module, log, "loop")

    bot_module.check_boss_notifications = reliable_checker

    async def trigger_check(reason="manual"):
        if not bot_module.bot.is_ready():
            return
        async with _trigger_lock:
            try:
                await _run_checker(bot_module, log, reason)
            except Exception as exc:
                log(f"❌ Boss notification trigger failed ({reason}): {exc!r}")

    bot_module.trigger_boss_notification_check = trigger_check

    @bot_module.bot.listen("on_interaction")
    async def boss_notification_after_kill(interaction):
        try:
            data = getattr(interaction, "data", None) or {}
            if data.get("type") != 2 or data.get("name") != "kill":
                return
            await asyncio.sleep(0.5)
            await trigger_check("post-/kill")
        except Exception as exc:
            log(f"⚠️ post-/kill notification hook failed: {exc!r}")

    @tasks.loop(seconds=30)
    async def health_probe():
        try:
            if not bot_module.bot.is_ready():
                return
            with bot_module.schedule_lock:
                count = len(bot_module.boss_schedule)
            running = reliable_checker.is_running()
            log(f"💓 NOTIFY HEALTH | schedules={count} checker_running={running}")
        except Exception as exc:
            log(f"⚠️ NOTIFY HEALTH failed: {exc!r}")

    async def start_health_probe():
        global _health_task
        if not health_probe.is_running():
            health_probe.start()
        _health_task = health_probe
        return health_probe

    bot_module.start_notification_health_probe = start_health_probe
    log("🛡️ Boss notification reliability patch installed")
