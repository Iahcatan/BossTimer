"""SKYNET Admin + startup/notification reliability patch.

Additive runtime patch. It does not replace Firebase, Dashboard, /kill, TTS,
Voice, or the existing boss-notification implementation.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from flask import jsonify, request, render_template_string

BANNED_PATH = "banned_users"
_banned_users = {}
_banned_lock = asyncio.Lock()
_installed = False
_diag_task = None
_runtime_gate_open = False
_original_loop_starts = {}


def _log(log, message):
    try:
        log(message)
    except Exception:
        print(message, flush=True)


def open_runtime_gate(log=None):
    global _runtime_gate_open
    _runtime_gate_open = True
    if log:
        _log(log, "🟢 Runtime gate OPEN: Voice + notification loops may start")


async def _firebase_get(bot_module, path):
    return await asyncio.to_thread(bot_module.db.reference(path).get)


async def _firebase_set(bot_module, path, value):
    return await asyncio.to_thread(bot_module.db.reference(path).set, value)


async def _load_bans(bot_module, log):
    global _banned_users
    try:
        data = await _firebase_get(bot_module, BANNED_PATH)
        _banned_users = {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        _log(log, f"🛡️ Ban database loaded: {len(_banned_users)} users")
    except Exception as exc:
        _log(log, f"⚠️ Ban database load failed safely: {exc!r}")


async def _save_bans(bot_module):
    await _firebase_set(bot_module, BANNED_PATH, _banned_users)


async def _ban_user(bot_module, user_id: int, user_name: str, moderator, reason: str):
    async with _banned_lock:
        _banned_users[str(user_id)] = {
            "user_id": int(user_id),
            "user_name": str(user_name),
            "reason": reason or "ไม่ระบุเหตุผล",
            "banned_by": str(getattr(moderator, "id", moderator)),
            "banned_by_name": str(getattr(moderator, "display_name", moderator)),
            "banned_at": datetime.now(timezone.utc).isoformat(),
        }
        await _save_bans(bot_module)


async def _unban_user(bot_module, user_id: int):
    async with _banned_lock:
        existed = str(user_id) in _banned_users
        if existed:
            _banned_users.pop(str(user_id), None)
            await _save_bans(bot_module)
        return existed


async def _is_banned(user_id: int) -> bool:
    async with _banned_lock:
        return str(user_id) in _banned_users


def _install_global_ban_check(bot_module, log):
    tree = bot_module.bot.tree
    original = getattr(tree, "interaction_check", None)

    async def global_ban_check(interaction: discord.Interaction) -> bool:
        try:
            if await _is_banned(interaction.user.id):
                msg = "🚫 คุณถูก Ban จากการใช้งาน SKYNET Bot\nหากคิดว่าเป็นความผิดพลาด กรุณาติดต่อ Administrator"
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(msg, ephemeral=True)
                    else:
                        await interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    pass
                return False
            if original is not None:
                result = original(interaction)
                return await result if asyncio.iscoroutine(result) else bool(result)
            return True
        except Exception as exc:
            _log(log, f"⚠️ global ban check failed safely: {exc!r}")
            return True

    tree.interaction_check = global_ban_check


def _register_discord_commands(bot_module, log):
    bot = bot_module.bot
    existing = {cmd.name for cmd in bot.tree.get_commands()}

    if "ban" not in existing:
        @bot.tree.command(name="ban", description="[Admin Only] Ban ผู้ใช้จากการใช้งาน SKYNET Bot")
        @app_commands.describe(user="สมาชิกที่ต้องการ Ban", reason="เหตุผลในการ Ban")
        @app_commands.checks.has_permissions(administrator=True)
        async def ban_command(interaction: discord.Interaction, user: discord.Member, reason: str = "ไม่ระบุเหตุผล"):
            await interaction.response.defer(ephemeral=True)
            if user.id == interaction.user.id:
                await interaction.followup.send("❌ ไม่สามารถ Ban ตัวเองได้", ephemeral=True)
                return
            await _ban_user(bot_module, user.id, user.display_name, interaction.user, reason)
            await interaction.followup.send(f"🔨 Ban **{user.display_name}** (`{user.id}`) สำเร็จ\nเหตุผล: {reason}", ephemeral=True)
            try:
                await bot_module.send_audit_log(interaction.guild, interaction.user, "Ban ผู้ใช้", f"ผู้ใช้: `{user.display_name}` (`{user.id}`)\nเหตุผล: {reason}", discord.Color.red())
            except Exception:
                pass

    if "unban" not in existing:
        @bot.tree.command(name="unban", description="[Admin Only] ปลด Ban ผู้ใช้จาก SKYNET Bot")
        @app_commands.describe(user_id="Discord User ID ที่ต้องการปลด Ban")
        @app_commands.checks.has_permissions(administrator=True)
        async def unban_command(interaction: discord.Interaction, user_id: str):
            await interaction.response.defer(ephemeral=True)
            try:
                uid = int(user_id.strip())
            except (TypeError, ValueError):
                await interaction.followup.send("❌ User ID ต้องเป็นตัวเลข", ephemeral=True)
                return
            existed = await _unban_user(bot_module, uid)
            await interaction.followup.send(f"🟢 ปลด Ban User ID `{uid}` สำเร็จ" if existed else f"ℹ️ User ID `{uid}` ไม่ได้อยู่ในรายการ Ban", ephemeral=True)

    _log(log, "🛡️ Admin Ban/Unban commands installed")


ADMIN_HTML = """
<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SKYNET Admin Panel</title><style>body{font-family:Arial;background:#0f172a;color:#e2e8f0;max-width:1000px;margin:30px auto;padding:0 16px}.card{background:#1e293b;padding:20px;border-radius:12px;margin-bottom:20px}input,button{padding:10px;border-radius:8px;border:1px solid #475569;margin:4px}input{background:#0f172a;color:#fff}button{cursor:pointer}.ban{background:#7f1d1d;color:#fff}.unban{background:#166534;color:#fff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #334155;text-align:left}.muted{color:#94a3b8}</style></head>
<body><h1>🛡️ SKYNET Admin Panel</h1><div class="card"><h2>Ban User</h2><input id="uid" placeholder="Discord User ID"><input id="uname" placeholder="ชื่อผู้ใช้"><input id="reason" placeholder="เหตุผล"><button class="ban" onclick="banUser()">Ban</button><button class="unban" onclick="unbanUser()">Unban</button><p class="muted">System Ban ของ SKYNET Bot ไม่ใช่ Discord Server Ban</p></div><div class="card"><h2>รายการ Ban</h2><table><thead><tr><th>User</th><th>Reason</th><th>Time</th><th>Action</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>const key={{ key|tojson }};const H={'Content-Type':'application/json','X-Admin-Key':key};async function load(){const r=await fetch('/api/admin/banned',{headers:H});const d=await r.json();if(!d.success)return;document.getElementById('rows').innerHTML=d.users.map(u=>`<tr><td>${u.user_name||''}<br>${u.user_id}</td><td>${u.reason||''}</td><td>${u.banned_at||''}</td><td><button class="unban" onclick="unbanId('${u.user_id}')">Unban</button></td></tr>`).join('')}async function banUser(){const r=await fetch('/api/admin/ban',{method:'POST',headers:H,body:JSON.stringify({user_id:uid.value,user_name:uname.value,reason:reason.value})});const d=await r.json();alert(d.success?'Ban สำเร็จ':d.error);load()}async function unbanId(id){const r=await fetch('/api/admin/unban',{method:'POST',headers:H,body:JSON.stringify({user_id:id})});const d=await r.json();alert(d.success?'Unban สำเร็จ':d.error);load()}function unbanUser(){unbanId(uid.value)}load();</script></body></html>
"""


def _admin_key_ok():
    expected = os.environ.get("ADMIN_PANEL_KEY", "").strip()
    if not expected:
        return False
    supplied = request.headers.get("X-Admin-Key", "").strip() or request.args.get("key", "").strip()
    return bool(supplied) and supplied == expected


def _install_admin_routes(bot_module, log):
    app = bot_module.app

    @app.route("/admin")
    def admin_panel():
        if not _admin_key_ok():
            return ("ADMIN_PANEL_KEY is not configured", 503) if not os.environ.get("ADMIN_PANEL_KEY", "").strip() else ("Unauthorized", 401)
        return render_template_string(ADMIN_HTML, key=request.args.get("key", ""), banned_users=list(_banned_users.values()))

    @app.route("/api/admin/banned")
    def admin_banned_api():
        if not _admin_key_ok():
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return jsonify({"success": True, "users": list(_banned_users.values())})

    @app.route("/api/admin/ban", methods=["POST"])
    def admin_ban_api():
        if not _admin_key_ok():
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try: uid = int(str(data.get("user_id", "")).strip())
        except (TypeError, ValueError): return jsonify({"success": False, "error": "Invalid user_id"}), 400
        try:
            fut = asyncio.run_coroutine_threadsafe(_ban_user(bot_module, uid, data.get("user_name", uid), "ADMIN_PANEL", data.get("reason", "ไม่ระบุเหตุผล")), bot_module.bot.loop)
            fut.result(timeout=10)
            return jsonify({"success": True, "user_id": uid})
        except Exception as exc: return jsonify({"success": False, "error": repr(exc)}), 500

    @app.route("/api/admin/unban", methods=["POST"])
    def admin_unban_api():
        if not _admin_key_ok():
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try: uid = int(str(data.get("user_id", "")).strip())
        except (TypeError, ValueError): return jsonify({"success": False, "error": "Invalid user_id"}), 400
        try:
            fut = asyncio.run_coroutine_threadsafe(_unban_user(bot_module, uid), bot_module.bot.loop)
            existed = fut.result(timeout=10)
            return jsonify({"success": True, "user_id": uid, "existed": existed})
        except Exception as exc: return jsonify({"success": False, "error": repr(exc)}), 500

    _log(log, "🛡️ Admin web panel installed at /admin")


def _gate_existing_task_starts(bot_module, log):
    names = ("check_boss_notifications", "check_bf_notifications", "check_library_boss_notifications", "update_live_embed", "check_auto_disconnect")
    for name in names:
        loop_obj = getattr(bot_module, name, None)
        if loop_obj is None or name in _original_loop_starts:
            continue
        original = loop_obj.start
        _original_loop_starts[name] = original
        def gated_start(*args, _name=name, _original=original, **kwargs):
            if not _runtime_gate_open:
                _log(log, f"⏸️ Deferred task start until command sync: {_name}")
                return None
            return _original(*args, **kwargs)
        loop_obj.start = gated_start


def _gate_voice_until_sync(bot_module, log):
    original = bot_module.ensure_configured_voice
    async def gated_ensure(guild, *args, **kwargs):
        if not _runtime_gate_open:
            _log(log, f"⏸️ Deferred Voice connect until command sync: {getattr(guild, 'name', guild)}")
            return None
        return await original(guild, *args, **kwargs)
    bot_module.ensure_configured_voice = gated_ensure
    bot_module._original_ensure_configured_voice = original


async def _notification_diagnostic_loop(bot_module, log):
    last_logged = {}
    while True:
        try:
            await asyncio.sleep(30)
            if not bot_module.bot.is_ready():
                continue
            now = bot_module.datetime.now(bot_module.TZ_THAI)
            for boss_name, data in list(getattr(bot_module, "boss_schedule", {}).items()):
                spawn = bot_module.parse_to_thai_datetime(data.get("spawn_time") or data.get("spawnTimeMs"))
                if not spawn:
                    continue
                left = (spawn - now).total_seconds()
                notice = bot_module.get_boss_advance_notice_seconds(boss_name)
                adv = bot_module.parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
                spawned = bot_module.parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))
                if 0 < left <= notice + 15 or -15 <= left <= 0:
                    stage = "advance-pending" if left > 0 else "spawn-pending"
                    key = (stage, adv, spawned)
                    if last_logged.get(boss_name) != key:
                        last_logged[boss_name] = key
                        _log(log, f"🧪 NOTIFY PIPELINE | {boss_name} | left={left:.1f}s | advance={adv} | spawn={spawned} | TTS={'ready' if callable(getattr(bot_module, 'speak_in_guild', None)) else 'missing'}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(log, f"⚠️ Notification diagnostic failed safely: {exc!r}")


def install(bot_module, log):
    global _installed, _diag_task
    if _installed:
        return
    _installed = True

    class _Voice4006Filter(logging.Filter):
        def filter(self, record):
            text = str(record.getMessage())
            return not ("Disconnected from voice" in text and "4006" in text)
    logging.getLogger("discord.voice_state").addFilter(_Voice4006Filter())

    _gate_existing_task_starts(bot_module, log)
    _gate_voice_until_sync(bot_module, log)
    _install_global_ban_check(bot_module, log)
    _register_discord_commands(bot_module, log)
    _install_admin_routes(bot_module, log)

    async def diagnostics_start():
        global _diag_task
        await bot_module.bot.wait_until_ready()
        await asyncio.sleep(2)
        if _diag_task is None or _diag_task.done():
            _diag_task = asyncio.create_task(_notification_diagnostic_loop(bot_module, log), name="skynet-notification-diagnostics")
            _log(log, "🧪 Notification pipeline diagnostics started")

    bot_module.start_notification_diagnostics = diagnostics_start
    bot_module.open_runtime_gate = lambda: open_runtime_gate(log)
    _log(log, "🛡️ Admin + startup/notification reliability patch installed")
