"""Boss notification reliability patch for SKYNET.

Keeps bot.py's existing notification pipeline, but adds:
- an immediate post-/kill evaluation (no waiting for the next 10s tick)
- effective noticeMinutes diagnostics
- protection against Firebase race restoring stale notification flags
- a lightweight health probe
"""
import asyncio
from discord.ext import tasks

_health_task = None
_trigger_lock = None


def install(bot_module, log):
    global _health_task, _trigger_lock
    if getattr(bot_module, "_boss_notification_patch_installed", False):
        return
    bot_module._boss_notification_patch_installed = True
    _trigger_lock = asyncio.Lock()

    async def trigger_check(reason="manual"):
        if not getattr(bot_module.bot, "is_ready", lambda: False)():
            return
        async with _trigger_lock:
            await asyncio.sleep(1.5)
            try:
                with bot_module.schedule_lock:
                    snapshot = bot_module.boss_schedule.copy()
                now = bot_module.datetime.now(bot_module.TZ_THAI)
                if not snapshot:
                    log(f"🔎 Boss notification check ({reason}): schedule=0")
                    return
                for boss, data in snapshot.items():
                    try:
                        spawn = bot_module.parse_to_thai_datetime(data.get("spawn_time"))
                        if not spawn:
                            log(f"⚠️ Boss notification check: {boss} has invalid spawn_time")
                            continue
                        left = (spawn - now).total_seconds()
                        configured = data.get("noticeMinutes")
                        fallback = bot_module.get_boss_advance_notice_seconds(boss) / 60
                        notice_min = max(1, int(configured)) if configured is not None else max(1, int(fallback))
                        log(
                            f"🔎 Boss notification check ({reason}): {boss} | "
                            f"spawn={spawn.isoformat()} | left={left:.1f}s | notice={notice_min}m | "
                            f"advance={data.get('notified_advance', False)} | spawn_sent={data.get('notified_spawn', False)}"
                        )
                    except Exception as exc:
                        log(f"⚠️ Boss notification diagnostic failed ({boss}): {exc!r}")

                checker = getattr(bot_module, "check_boss_notifications", None)
                if checker is not None:
                    await checker()
                    log(f"🟢 Boss notification checker executed ({reason})")
            except Exception as exc:
                log(f"❌ Boss notification trigger failed ({reason}): {exc!r}")

    bot_module.trigger_boss_notification_check = trigger_check

    @bot_module.bot.listen("on_interaction")
    async def boss_notification_after_kill(interaction):
        try:
            data = getattr(interaction, "data", None) or {}
            if data.get("type") != 1 or data.get("name") != "kill":
                return
            asyncio.create_task(trigger_check("post-/kill"))
        except Exception as exc:
            log(f"⚠️ post-/kill notification hook failed: {exc!r}")

    @tasks.loop(seconds=30)
    async def health_probe():
        try:
            if not bot_module.bot.is_ready():
                return
            with bot_module.schedule_lock:
                count = len(bot_module.boss_schedule)
            checker = getattr(bot_module, "check_boss_notifications", None)
            running = bool(checker and checker.is_running())
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
