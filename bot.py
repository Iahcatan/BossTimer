import os
import json
import threading
import base64
import asyncio
import time
import shutil
import traceback
import logging
import re
import uuid
import sqlite3
import aiohttp
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, render_template_string, request, jsonify
from waitress import serve
import edge_tts
import imageio_ffmpeg

# 🔥 Firebase Admin SDK Setup
import firebase_admin
from firebase_admin import credentials, db

# ==========================================
# 🔥 0. เชื่อมต่อ Firebase Realtime Database
# ==========================================
# อ่านค่าการเชื่อมต่อจาก Environment Variable แทนการเก็บ Service Account Key
# รองรับทั้ง FIREBASE_SERVICE_ACCOUNT_JSON (JSON string) และ
# FIREBASE_SERVICE_ACCOUNT_BASE64 (Base64 ของ JSON)
# DATABASE_URL ใช้ค่าจาก Environment Variable เช่นกัน
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_BASE64 = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64", "").strip()
DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL",
    "https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app"
).strip()

if not firebase_admin._apps:
    try:
        firebase_service_account = None

        if FIREBASE_SERVICE_ACCOUNT_JSON:
            firebase_service_account = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
        elif FIREBASE_SERVICE_ACCOUNT_BASE64:
            decoded_key = base64.b64decode(FIREBASE_SERVICE_ACCOUNT_BASE64).decode("utf-8")
            firebase_service_account = json.loads(decoded_key)

        if firebase_service_account:
            cred = credentials.Certificate(firebase_service_account)
            firebase_admin.initialize_app(cred, {
                'databaseURL': DATABASE_URL
            })
            print("✅ เชื่อมต่อ Firebase Realtime Database สำเร็จ!")
        else:
            raise ValueError(
                "ไม่พบ FIREBASE_SERVICE_ACCOUNT_JSON หรือ FIREBASE_SERVICE_ACCOUNT_BASE64 ใน Environment Variable"
            )
    except Exception as e:
        print(f"❌ ไม่สามารถเชื่อมต่อ Firebase Realtime Database ได้: {e}")

# ==========================================
# ⚙️ ซ่อน Log แจ้งเตือนที่ไม่จำเป็นจาก Discord.py
# ==========================================
logging.getLogger('discord.player').setLevel(logging.WARNING)
logging.getLogger('discord.voice_state').setLevel(logging.WARNING)

# ==========================================
# 🔒 Thread Safety Lock สำหรับแชร์ข้อมูล & Flag ป้องกัน Loop
# ==========================================
schedule_lock = threading.Lock()
is_bot_ready = False
is_updating_from_bot = False

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone ไทย & Helper Functions
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))

def parse_bool(val, default=False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ('true', '1', 'yes'):
            return True
        if cleaned in ('false', '0', 'no'):
            return False
    return default

def parse_to_thai_datetime(data_val):
    if not data_val:
        return None
    if isinstance(data_val, (int, float)):
        return datetime.fromtimestamp(data_val / 1000.0, tz=TZ_THAI)
    elif isinstance(data_val, str):
        cleaned_val = data_val.replace(" น.", "").strip()
        try:
            if cleaned_val.endswith('Z'):
                cleaned_val = cleaned_val[:-1] + '+00:00'
            st = datetime.fromisoformat(cleaned_val)
            if st.tzinfo is None:
                return st.replace(tzinfo=TZ_THAI)
            return st.astimezone(TZ_THAI)
        except ValueError:
            now = datetime.now(TZ_THAI)
            try:
                time_obj = datetime.strptime(cleaned_val, "%H:%M:%S").time()
                st = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=time_obj.second, microsecond=0)
                if (st - now).total_seconds() > 600:
                    st -= timedelta(days=1)
                elif st < now - timedelta(hours=18):
                    st += timedelta(days=1)
                return st
            except ValueError:
                try:
                    time_obj = datetime.strptime(cleaned_val, "%H:%M").time()
                    st = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
                    if (st - now).total_seconds() > 600:
                        st -= timedelta(days=1)
                    elif st < now - timedelta(hours=18):
                        st += timedelta(days=1)
                    return st
                except ValueError:
                    pass
            return None
    elif isinstance(data_val, datetime):
        if data_val.tzinfo is None:
            return data_val.replace(tzinfo=TZ_THAI)
        return data_val.astimezone(TZ_THAI)
    return None

def parse_time_input(time_str: str, now: datetime) -> datetime:
    if not time_str or not time_str.strip():
        return now
    cleaned = time_str.strip().replace(".", ":")
    if re.fullmatch(r'\d{3,6}', cleaned):
        if len(cleaned) == 3:
            hh, mm, ss = int(cleaned[0]), int(cleaned[1:]), 0
        elif len(cleaned) == 4:
            hh, mm, ss = int(cleaned[:2]), int(cleaned[2:]), 0
        elif len(cleaned) == 5:
            hh, mm, ss = int(cleaned[0]), int(cleaned[1:3]), int(cleaned[3:])
        elif len(cleaned) == 6:
            hh, mm, ss = int(cleaned[:2]), int(cleaned[2:4]), int(cleaned[4:])
    elif ":" in cleaned:
        parts = [int(p) for p in cleaned.split(":") if p.isdigit()]
        if len(parts) == 2:
            hh, mm, ss = parts[0], parts[1], 0
        elif len(parts) == 3:
            hh, mm, ss = parts[0], parts[1], parts[2]
        else:
            raise ValueError("Invalid time format")
    else:
        raise ValueError("Invalid time format")
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("Invalid time range")
    boss_died_at = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if (boss_died_at - now).total_seconds() > 600:
        boss_died_at -= timedelta(days=1)
    return boss_died_at

def get_boss_respawn_time(boss_name: str) -> timedelta:
    if not boss_name: return timedelta(minutes=30)
    cleaned = boss_name.strip().lower()
    for key, val in BOSS_RESPAWN_TIMES.items():
        if key.lower() == cleaned: return val
    return timedelta(minutes=30)

def get_boss_canonical_name(boss_name: str) -> str:
    if not boss_name: return boss_name
    cleaned = boss_name.strip().lower()
    for key in BOSS_RESPAWN_TIMES.keys():
        if key.lower() == cleaned: return key
    return boss_name

def get_boss_advance_notice_seconds(boss_name: str) -> int:
    cleaned = boss_name.strip().lower() if boss_name else ""
    if "wadangka" in cleaned or "วาดังการ์" in cleaned: return 1800 
    for key, val in ADVANCE_NOTICE_SECONDS.items():
        if key.lower() == cleaned: return val
    return 300

def get_boss_advance_notice_text(boss_name: str) -> str:
    cleaned = boss_name.strip().lower() if boss_name else ""
    if "wadangka" in cleaned or "วาดังการ์" in cleaned: return "30 นาที" 
    for key, val in ADVANCE_NOTICE_TEXT.items():
        if key.lower() == cleaned: return val
    return "5 นาที"

def get_boss_advance_notice_text_en(boss_name: str) -> str:
    seconds = get_boss_advance_notice_seconds(boss_name)
    if seconds == 3600: return "1 hour"
    return f"{int(seconds / 60)} minutes"

def get_boss_advance_notice_text_ko(boss_name: str) -> str:
    seconds = get_boss_advance_notice_seconds(boss_name)
    if seconds == 3600: return "1시간"
    return f"{int(seconds / 60)}분"

def get_boss_cd_text(boss_name: str) -> str:
    cleaned = boss_name.strip().lower() if boss_name else ""
    for key, val in BOSS_CD_TEXT.items():
        if key.lower() == cleaned: return val
    return "30 นาที"

def get_boss_pronunciation(boss_name: str) -> str:
    cleaned = boss_name.strip().lower() if boss_name else ""
    for key, val in BOSS_PRONUNCIATION.items():
        if key.lower() == cleaned: return val
    return boss_name

# ==========================================
# 🗄️ Database Utility (SQLite Persistent Storage)
# ==========================================
# Render filesystem is ephemeral unless a persistent disk is mounted.
# SQLite is only a local fallback; Firebase is the canonical database.
DATA_DIR = os.environ.get("SKYNET_DATA_DIR", os.getcwd())
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_database.db")

def init_db():
    """Create/repair the local SQLite fallback database before any save can run."""
    try:
        os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        print(f"✅ SQLite Database พร้อมใช้งาน: {os.path.abspath(DB_FILE)}")
        return True
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการตั้งค่า SQLite Database: {e}")
        return False

def set_db_value(key: str, value):
    """Self-healing local fallback: recreate the table if an old DB is missing it."""
    try:
        init_db()
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        val_str = json.dumps(value, ensure_ascii=False)
        cursor.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val_str)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ บันทึกข้อมูลลง SQLite ไม่สำเร็จ ({key}): {e}")
        return False

def get_db_value(key: str, default=None):
    try:
        init_db()
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        print(f"❌ ดึงข้อมูลจาก SQLite ไม่สำเร็จ ({key}): {e}")
    return default

# Initialize immediately, not only from on_ready(). This prevents the
# post-deploy race where a Discord interaction arrives before SQLite exists.
init_db()

# ==========================================
# 🌐 1. Web Dashboard & Server สำหรับ Render
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Boss Timer Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <meta http-equiv="refresh" content="10">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: 'Kanit', sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; }
        .status-badge { font-size: 0.9rem; padding: 6px 12px; border-radius: 20px; }
        .table { color: #f8fafc; }
        .table-dark { --bs-table-bg: #1e293b; }
        .form-check-input:checked { background-color: #0d6efd; border-color: #0d6efd; }
    </style>
</head>
<body>
    <div class="container py-5">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h2>⚔️ Boss Timer Dashboard</h2>
            <span class="badge bg-success status-badge">🟢 Bot Online</span>
        </div>

        <!-- 🔊 สวิตช์ตั้งค่าเปิด-ปิดการแจ้งเตือนเสียง TTS แยกภาษา -->
        <div class="card p-4 shadow-sm mb-4">
            <h4 class="card-title text-info mb-3">🔊 ตั้งค่าการแจ้งเตือนด้วยเสียง (TTS แยกตามภาษา)</h4>
            <div class="row g-3">
                <div class="col-md-4">
                    <div class="form-check form-switch fs-5">
                        <input class="form-check-input" type="checkbox" id="tts_th" {% if tts_th %}checked{% endif %} onchange="updateTTS('th', this.checked)">
                        <label class="form-check-label" for="tts_th">🇹🇭 เสียงภาษาไทย (TH)</label>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="form-check form-switch fs-5">
                        <input class="form-check-input" type="checkbox" id="tts_en" {% if tts_en %}checked{% endif %} onchange="updateTTS('en', this.checked)">
                        <label class="form-check-label" for="tts_en">🇺🇸 เสียงภาษาอังกฤษ (EN)</label>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="form-check form-switch fs-5">
                        <input class="form-check-input" type="checkbox" id="tts_ko" {% if tts_ko %}checked{% endif %} onchange="updateTTS('ko', this.checked)">
                        <label class="form-check-label" for="tts_ko">🇰🇷 เสียงภาษาเกาหลี (KO)</label>
                    </div>
                </div>
            </div>
        </div>

        <div class="card p-4 shadow-sm mb-4">
            <h4 class="card-title text-warning mb-3">📜 ตารางเวลาบอสล่าสุด</h4>
            {% if bosses %}
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle">
                    <thead>
                        <tr>
                            <th>ชื่อบอส</th>
                            <th>เวลาเกิด (เวลาไทย)</th>
                            <th>สถานะ/นับถอยหลัง</th>
                            <th>แจ้งเตือนล่วงหน้า</th>
                            <th>ผู้บันทึก (Recorded by)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for boss in bosses %}
                        <tr>
                            <td class="fw-bold text-info">{{ boss.name }}</td>
                            <td>{{ boss.spawn_time }} น.</td>
                            <td>
                                {% if boss.is_spawned %}
                                    <span class="badge bg-danger">⚔️ เกิดแล้ว!</span>
                                {% else %}
                                    <span class="badge bg-primary">⏳ เหลือ {{ boss.time_left }}</span>
                                {% endif %}
                            </td>
                            <td><small class="text-muted">{{ boss.notice_text }}</small></td>
                            <td><small class="text-warning">{{ boss.recorded_by }}</small></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <p class="text-muted mb-0">📌 ยังไม่มีการบันทึกเวลาบอสใดๆ ในขณะนี้</p>
            {% endif %}
        </div>
        <footer class="text-center text-muted">
            <small>อัปเดตข้อมูลอัตโนมัติทุกๆ 10 วินาที • Boss Timer Bot 24/7</small>
        </footer>
    </div>

    <script>
        function updateTTS(lang, enabled) {
            fetch('/api/toggle_tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lang: lang, enabled: enabled })
            })
            .then(res => res.json())
            .then(data => {
                if (!data.success) {
                    alert('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า TTS');
                }
            })
            .catch(err => {
                console.error('Error:', err);
                alert('ไม่สามารถเชื่อมต่อกับเซิร์ฟเวอร์ได้');
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    now = datetime.now(TZ_THAI)
    boss_list = []
    
    with schedule_lock:
        schedule_copy = boss_schedule.copy()
        
    sorted_bosses = sorted(
        schedule_copy.items(), 
        key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now
    )
    
    for boss_name, data in sorted_bosses:
        spawn_time = parse_to_thai_datetime(data["spawn_time"])
        if not spawn_time:
            continue
        time_left_sec = (spawn_time - now).total_seconds()
        
        if time_left_sec <= 0:
            time_left_str = "เกิดแล้ว!"
            is_spawned = True
        else:
            m, s = divmod(int(time_left_sec), 60)
            h, m = divmod(m, 60)
            time_left_str = f"{h:02d}:{m:02d}:{s:02d} ชม."
            is_spawned = False

        boss_list.append({
            "name": boss_name,
            "spawn_time": spawn_time.strftime("%H:%M:%S"),
            "time_left": time_left_str,
            "is_spawned": is_spawned,
            "notice_text": get_boss_advance_notice_text(boss_name),
            "recorded_by": data.get("recorded_by", "-")
        })

    return render_template_string(
        HTML_TEMPLATE, 
        bosses=boss_list,
        tts_th=tts_th_enabled,
        tts_en=tts_en_enabled,
        tts_ko=tts_ko_enabled
    )

@app.route('/api/toggle_tts', methods=['POST'])
def toggle_tts_api():
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    data = request.get_json() or {}
    lang = data.get('lang')
    enabled = parse_bool(data.get('enabled'), True)
    
    if lang == 'th':
        tts_th_enabled = enabled
    elif lang == 'en':
        tts_en_enabled = enabled
    elif lang == 'ko':
        tts_ko_enabled = enabled
    else:
        return jsonify({"success": False, "error": "Invalid language"}), 400

    if is_bot_ready and bot.loop and bot.loop.is_running():
        asyncio.run_coroutine_threadsafe(save_bot_settings(), bot.loop)
    return jsonify({"success": True, "lang": lang, "enabled": enabled})

_web_server_started = False
_web_server_lock = threading.Lock()

def run_web():
    global _web_server_started
    port = int(os.environ.get("PORT", 5000))
    with _web_server_lock:
        if _web_server_started:
            return
        _web_server_started = True
    print(f"🌐 Starting Flask/Waitress on 0.0.0.0:{port}")
    try:
        serve(app, host="0.0.0.0", port=port, threads=4, expose_tracebacks=False)
    except OSError as e:
        # Render can briefly restart/rebind a worker. Do not crash the Discord bot thread.
        if getattr(e, "errno", None) == 98:
            print(f"⚠️ PORT {port} ถูกใช้งานอยู่แล้ว — ไม่เปิด Web Server ซ้ำ")
        else:
            print(f"❌ Web Server หยุดทำงาน: {e}")
    except Exception as e:
        print(f"❌ Web Server error: {e}")

def keep_alive():
    global _web_server_started
    with _web_server_lock:
        if _web_server_started:
            return
    t = threading.Thread(target=run_web, name="render-web", daemon=True)
    t.start()

# ==========================================
# ⚙️ Config & Global Variables
# ==========================================
DATA_FILE = "boss_data.json"
CUSTOM_BOSSES_FILE = "custom_bosses.json"
LIVE_CONFIG_FILE = "live_config.json"
VIP_CONFIG_FILE = "vip_config.json"
VOICE_CONFIG_FILE = "voice_config.json"
SETTINGS_FILE = "bot_settings.json"

DEFAULT_TARGET_ROLE_IDS = []
env_target_roles = os.environ.get("TARGET_ROLE_IDS", "")
TARGET_ROLE_IDS = [int(r.strip()) for r in env_target_roles.split(",") if r.strip().isdigit()] if env_target_roles else DEFAULT_TARGET_ROLE_IDS

DEFAULT_BF_ROLE_IDS = []
env_bf_roles = os.environ.get("BF_ROLE_IDS", "")
BF_ROLE_IDS = [int(r.strip()) for r in env_bf_roles.split(",") if r.strip().isdigit()] if env_bf_roles else DEFAULT_BF_ROLE_IDS

LOG_CHANNEL_NAME = "boss-logs"
LIVE_CHANNEL_NAME = "boss-schedule"

voice_empty_start = {}
voice_locks = {}
voice_connect_locks = {}
disconnect_tasks = {}
voice_config = {}
last_voice_connect_attempt = {}
last_channel_fetch_attempt = {}

bf_notify_enabled = True
lib_notify_enabled = True
ppl_notify_enabled = True
tts_th_enabled = True
tts_en_enabled = True
tts_ko_enabled = True

vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
last_bf_notified_hour = -1
last_lib_notified_key = ""

cached_live_message = None
VOICE_THAI = "th-TH-PremwadeeNeural"
VOICE_ENG = "en-US-AriaNeural"
VOICE_KOR = "ko-KR-SunHiNeural"

BOSS_RESPAWN_TIMES = {
    "Wadangka": timedelta(hours=2, minutes=30),
    "Elemental Queen": timedelta(hours=2, minutes=30),
    "Tank": timedelta(minutes=58, seconds=20),
    "Swirl Flame": timedelta(minutes=58, seconds=20),
    "Maelstrom": timedelta(minutes=58, seconds=20),
    "Twister": timedelta(minutes=58, seconds=20),
    "Bigmama": timedelta(hours=48),
    "Chief Magief": timedelta(minutes=30),
    "Faith": timedelta(hours=5, minutes=53),
    "Apapa": timedelta(minutes=15),
    "Corrupt Forest Keeper": timedelta(minutes=58),
    "Recluse": timedelta(hours=11, minutes=23),
    "Blackskull": timedelta(minutes=56, seconds=50),
    "Sleepy Kooii": timedelta(minutes=20),
    "Awaken Kooii": timedelta(hours=1, minutes=3),
    "Eeheehee": timedelta(hours=1, minutes=6, seconds=48),
    "Ooheeheek": timedelta(hours=1, minutes=8, seconds=3),
    "Oohehe": timedelta(hours=1, minutes=5, seconds=8),
    "Guardian Imp": timedelta(hours=1, minutes=3),
    "Devilang": timedelta(hours=5, minutes=33),
    "Blackjuno": timedelta(minutes=35),
    "Blacksky": timedelta(minutes=35),
    "Red Fox": timedelta(minutes=20),
    "7tailfox": timedelta(minutes=20),
    "777Tailfox": timedelta(minutes=30),
    "Sunrise Flower": timedelta(minutes=20),
    "Magma Senior Thief": timedelta(minutes=20),
    "Bbinikjoe": timedelta(minutes=20),
    "Bigmouse": timedelta(minutes=20),
    "Caligo": timedelta(days=7),
    "Poison Root Flower": timedelta(minutes=28, seconds=10),
    "Contaminated Queen Bee": timedelta(minutes=28),
    "Rotten Pudding": timedelta(minutes=30),
    "Swamp Flower Monster": timedelta(minutes=30),
    "Ukpana": timedelta(hours=48),
    "Darlene the Witch": timedelta(hours=72),
    "Illust": timedelta(hours=72),
    "Actaemon": timedelta(hours=6),
    "Aiyo's Protector": timedelta(hours=72),
    "Glucose": timedelta(minutes=30),
    "Overload": timedelta(minutes=29, seconds=52),
    "Soul Lich": timedelta(hours=24, minutes=15),
    "Platanista": timedelta(hours=168),
    "Barslaf": timedelta(hours=48),
    "Billiard": timedelta(hours=7, minutes=55, seconds=3),
    "Shaaack": timedelta(minutes=30),
    "Suuuk": timedelta(minutes=20),
    "Sususuk": timedelta(minutes=20),
    "sandgrave": timedelta(minutes=20),
    "Elder Beholder": timedelta(minutes=20)
}

DEFAULT_BOSS_NAMES = set(BOSS_RESPAWN_TIMES.keys())

BOSS_CD_TEXT = {
    "Wadangka": "2 ชั่วโมง 30 นาที",
    "Elemental Queen": "2 ชั่วโมง 30 นาที",
    "Tank": "58 นาที 20 วินาที",
    "Swirl Flame": "58 นาที 20 วินาที",
    "Maelstrom": "58 นาที 20 วินาที",
    "Twister": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง",
    "Chief Magief": "30 นาที",
    "Faith": "5 ชั่วโมง 53 นาที",
    "Apapa": "15 นาที",
    "Corrupt Forest Keeper": "58 นาที",
    "Recluse": "11 ชั่วโมง 23 นาที",
    "Blackskull": "56 นาที 50 วินาที",
    "Sleepy Kooii": "20 นาที",
    "Awaken Kooii": "1 ชั่วโมง 3 นาที",
    "Eeheehee": "1 ชั่วโมง 6 นาที 48 วินาที",
    "Ooheeheek": "1 ชั่วโมง 8 นาที 3 วินาที",
    "Oohehe": "1 ชั่วโมง 5 นาที 8 วินาที",
    "Guardian Imp": "1 ชั่วโมง 3 นาที",
    "Devilang": "5 ชั่วโมง 33 นาที",
    "Blackjuno": "35 นาที",
    "Blacksky": "35 นาที",
    "Red Fox": "20 นาที",
    "7tailfox": "20 นาที",
    "777Tailfox": "30 นาที",
    "Sunrise Flower": "20 นาที",
    "Magma Senior Thief": "20 นาที",
    "Bbinikjoe": "20 นาที",
    "Bigmouse": "20 นาที",
    "Caligo": "7 วัน",
    "Poison Root Flower": "28 นาที 10 วินาที",
    "Contaminated Queen Bee": "28 นาที",
    "Rotten Pudding": "30 นาที",
    "Swamp Flower Monster": "30 นาที",
    "Ukpana": "48 ชั่วโมง",
    "Darlene the Witch": "72 ชั่วโมง",
    "Illust": "72 ชั่วโมง",
    "Actaemon": "6 ชั่วโมง",
    "Aiyo's Protector": "72 ชั่วโมง",
    "Glucose": "30 นาที",
    "Overload": "29 นาที 52 วินาที",
    "Soul Lich": "24 ชั่วโมง 15 นาที",
    "Platanista": "168 ชั่วโมง (7 วัน)",
    "Barslaf": "48 ชั่วโมง",
    "Billiard": "7 ชั่วโมง 55 นาที 3 วินาที",
    "Shaaack": "30 นาที",
    "Suuuk": "20 นาที",
    "Sususuk": "20 นาที",
    "sandgrave": "20 นาที",
    "Elder Beholder": "20 นาที"
}

ADVANCE_NOTICE_SECONDS = {
    "Wadangka": 1800, "Elemental Queen": 1800, "Tank": 300, "Swirl Flame": 300,
    "Maelstrom": 300, "Twister": 300, "Bigmama": 1800, "Chief Magief": 300,
    "Faith": 1800, "Apapa": 300, "Corrupt Forest Keeper": 300, "Recluse": 1800,
    "Blackskull": 300, "Sleepy Kooii": 300, "Awaken Kooii": 300, "Eeheehee": 300,
    "Ooheeheek": 300, "Oohehe": 300, "Guardian Imp": 300, "Devilang": 1800,
    "Blackjuno": 300, "Blacksky": 300, "Red Fox": 300, "7tailfox": 300,
    "777Tailfox": 300, "Sunrise Flower": 300, "Magma Senior Thief": 300, "Bbinikjoe": 300,
    "Bigmouse": 300, "Caligo": 3600, "Poison Root Flower": 300, "Contaminated Queen Bee": 300,
    "Rotten Pudding": 300, "Swamp Flower Monster": 300, "Ukpana": 1800, "Darlene the Witch": 1800,
    "Illust": 1800, "Actaemon": 1800, "Aiyo's Protector": 1800, "Glucose": 300,
    "Overload": 300, "Soul Lich": 1800, "Platanista": 3600, "Barslaf": 1800,
    "Billiard": 1800, "Shaaack": 300, "Suuuk": 300, "Sususuk": 300,
    "sandgrave": 300, "Elder Beholder": 300
}

ADVANCE_NOTICE_TEXT = {
    "Wadangka": "30 นาที", "Elemental Queen": "30 นาที", "Tank": "5 นาที", "Swirl Flame": "5 นาที",
    "Maelstrom": "5 นาที", "Twister": "5 นาที", "Bigmama": "30 นาที", "Chief Magief": "5 นาที",
    "Faith": "30 นาที", "Apapa": "5 นาที", "Corrupt Forest Keeper": "5 นาที", "Recluse": "30 นาที",
    "Blackskull": "5 นาที", "Sleepy Kooii": "5 นาที", "Awaken Kooii": "5 นาที", "Eeheehee": "5 นาที",
    "Ooheeheek": "5 นาที", "Oohehe": "5 นาที", "Guardian Imp": "5 นาที", "Devilang": "30 นาที",
    "Blackjuno": "5 นาที", "Blacksky": "5 นาที", "Red Fox": "5 นาที", "7tailfox": "5 นาที",
    "777Tailfox": "5 นาที", "Sunrise Flower": "5 นาที", "Magma Senior Thief": "5 นาที", "Bbinikjoe": "5 นาที",
    "Bigmouse": "5 นาที", "Caligo": "1 ชั่วโมง", "Poison Root Flower": "5 นาที", "Contaminated Queen Bee": "5 นาที",
    "Rotten Pudding": "5 นาที", "Swamp Flower Monster": "5 นาที", "Ukpana": "30 นาที", "Darlene the Witch": "30 นาที",
    "Illust": "30 นาที", "Actaemon": "30 นาที", "Aiyo's Protector": "30 นาที", "Glucose": "5 นาที",
    "Overload": "5 นาที", "Soul Lich": "30 นาที", "Platanista": "1 ชั่วโมง", "Barslaf": "30 นาที",
    "Billiard": "30 นาที", "Shaaack": "5 นาที", "Suuuk": "5 นาที", "Sususuk": "5 นาที",
    "sandgrave": "5 นาที", "Elder Beholder": "5 นาที"
}

BOSS_PRONUNCIATION = {
    "Wadangka": "วาดังการ์", "Elemental Queen": "เอเลเมนทัล ควีน", "Tank": "แท้งก์", "Swirl Flame": "สเวิร์ล เฟลม",
    "Maelstrom": "เมลสตรอม", "Twister": "ทวิสเตอร์", "Bigmama": "บิ๊กมาม่า", "Chief Magief": "ชีฟ มาเกียฟ",
    "Faith": "เฟธ", "Apapa": "อาปาป้า", "Corrupt Forest Keeper": "คอร์รัปต์ ฟอเรสต์ คีปเปอร์", "Recluse": "เรคลูซ",
    "Blackskull": "แบล็กสกัลป์", "Sleepy Kooii": "สลีปปี้ คูอี", "Awaken Kooii": "อเวเคน คูอี", "Eeheehee": "อีฮีฮี",
    "Ooheeheek": "โอฮีฮีก", "Oohehe": "โอเฮเฮ้", "Guardian Imp": "การ์เดียน อิมป์", "Devilang": "เดวิลแลง",
    "Blackjuno": "แบล็กจูโน่", "Blacksky": "แบล็กสกาย", "Red Fox": "เรดฟ็อกซ์", "7tailfox": "เซเว่นเทลฟ็อกซ์",
    "777Tailfox": "ทริปเปิลเซเว่นเทลฟ็อกซ์", "Sunrise Flower": "ซันไรส์ ฟลาวเวอร์", "Magma Senior Thief": "แมกม่า ซีเนียร์ ธีฟ",
    "Bbinikjoe": "บีนิกโจ", "Bigmouse": "บิ๊กเมาส์", "Caligo": "คาลิโก้", "Poison Root Flower": "พอยซัน รูท ฟลาวเวอร์",
    "Contaminated Queen Bee": "คอนทามิเนตเต็ด ควีนบี", "Rotten Pudding": "รอตเทน พุดดิ้ง", "Swamp Flower Monster": "สแวมป์ ฟลาวเวอร์ มอนสเตอร์",
    "Ukpana": "อุคปาน่า", "Darlene the Witch": "ดาร์ลีน เดอะ วิทช์", "Illust": "อิลลัสต์", "Actaemon": "แอคธีมอน",
    "Aiyo's Protector": "ไอโย โปรเตกเตอร์", "Glucose": "กลูโคส", "Overload": "โอเวอร์โหลด", "โซล ลิช": "โซล ลิช",
    "Platanista": "พลานิสต้า", "Barslaf": "บาร์สลาฟ", "Billiard": "บิลเลียด", "Shaaack": "ชาค",
    "Suuuk": "ซุก", "Sususuk": "ซูซูซุก", "sandgrave": "แซนด์เกรฟ", "Elder Beholder": "เอลเดอร์ บีโฮลเดอร์"
}

boss_schedule = {}
live_message_config = {}

# ==========================================
# 📝 3. ระบบ Audit Log
# ==========================================
async def send_audit_log(guild: discord.Guild, user: discord.User, action: str, details: str, color: discord.Color):
    if not guild: return
    log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if not log_channel: return

    now = datetime.now(TZ_THAI)
    embed = discord.Embed(
        title=f"📝 Audit Log: {action}",
        color=color,
        timestamp=now
    )
    embed.add_field(name="👤 ผู้ดำเนินการ", value=f"{user.mention} (`{user.name}`)", inline=True)
    embed.add_field(name="📋 รายละเอียด", value=details, inline=False)
    embed.set_footer(text=f"User ID: {user.id}")

    try: await log_channel.send(embed=embed)
    except Exception as e: print(f"❌ ส่ง Audit Log ไม่สำเร็จ: {e}")

# ==========================================
# 🛡️ 4. Check สำหรับตรวจสอบสิทธิ์ผู้ใช้งาน
# ==========================================
def check_user_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator: return True
    if not TARGET_ROLE_IDS: return True
    user_role_ids = [role.id for role in member.roles]
    return any(role_id in TARGET_ROLE_IDS for role_id in user_role_ids)

def has_allowed_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member): return False
        return check_user_permission(interaction.user)
    return app_commands.check(predicate)

# ==========================================
# 💾 5. ระบบบันทึก/โหลดไฟล์ Firebase & Local Storage
# ==========================================
def save_json_local(filename: str, data: dict):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ เซฟ {filename} ลง local ไม่สำเร็จ: {e}")

def _schedule_record_to_firebase(boss_name: str, data: dict) -> dict:
    """Single canonical boss_schedule schema used by Discord and Dashboard."""
    spawn_dt = parse_to_thai_datetime(data.get("spawnTimeMs") or data.get("spawn_time"))
    kill_dt = parse_to_thai_datetime(data.get("killTimeMs") or data.get("kill_time_ms") or data.get("kill_time"))
    if not spawn_dt:
        raise ValueError(f"ไม่มี spawnTimeMs ที่ถูกต้องสำหรับ {boss_name}")
    spawn_ms = int(spawn_dt.timestamp() * 1000)
    kill_ms = int(kill_dt.timestamp() * 1000) if kill_dt else None
    notice = data.get("noticeMinutes")
    if notice is None:
        notice = int(get_boss_advance_notice_seconds(boss_name) / 60)
    try:
        notice = max(1, int(notice))
    except (TypeError, ValueError):
        notice = int(get_boss_advance_notice_seconds(boss_name) / 60)
    record = {
        "spawnTimeMs": spawn_ms,
        # Keep both fields because the Dashboard/Firebase Rules use spawn_time
        # while the bot/UI use spawnTimeMs.
        "spawn_time": datetime.fromtimestamp(spawn_ms / 1000, tz=TZ_THAI).isoformat(),
        "noticeMinutes": notice,
        "recordedBy": data.get("recordedBy") or data.get("recorded_by") or "-",
        "notifiedNotice": parse_bool(data.get("notifiedNotice", data.get("notified_advance", False))),
        "notifiedSpawn": parse_bool(data.get("notifiedSpawn", data.get("notified_spawn", False))),
    }
    if kill_ms is not None:
        record["killTimeMs"] = kill_ms
    channel_id = data.get("channelId") or data.get("channel_id")
    if channel_id is not None:
        try: record["channelId"] = int(channel_id)
        except (TypeError, ValueError): pass
    voice_channel_id = data.get("voiceChannelId") or data.get("voice_channel_id")
    if voice_channel_id is not None:
        try: record["voiceChannelId"] = int(voice_channel_id)
        except (TypeError, ValueError): pass
    return record

def _firebase_to_internal(boss_name: str, data: dict) -> dict | None:
    try:
        record = _schedule_record_to_firebase(boss_name, data)
    except Exception:
        return None
    # Keep the existing task/UI code stable while Firebase remains canonical.
    return {
        "spawn_time": parse_to_thai_datetime(record["spawnTimeMs"]),
        "killTimeMs": record.get("killTimeMs"),
        "channel_id": record.get("channelId"),
        "voice_channel_id": record.get("voiceChannelId"),
        "notified_advance": record.get("notifiedNotice", False),
        "notified_spawn": record.get("notifiedSpawn", False),
        "noticeMinutes": record.get("noticeMinutes", 5),
        "recorded_by": record.get("recordedBy", "-"),
    }

async def save_boss_data():
    """Persist boss_schedule to Firebase (canonical) and local fallback."""
    global is_updating_from_bot
    with schedule_lock:
        firebase_data = {}
        for boss_name, data in boss_schedule.items():
            try:
                firebase_data[boss_name] = _schedule_record_to_firebase(boss_name, data)
            except Exception as e:
                print(f"⚠️ ข้ามข้อมูลบอส {boss_name}: {e}")

    firebase_ok = False
    try:
        is_updating_from_bot = True
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("boss_schedule").set, firebase_data),
            timeout=12
        )
        firebase_ok = True
        print(f"☁️ Firebase boss_schedule บันทึกสำเร็จ: {len(firebase_data)} รายการ")
    except Exception as e:
        print(f"❌ บันทึก boss_schedule ลง Firebase ไม่สำเร็จ: {e}")
    finally:
        is_updating_from_bot = False

    sqlite_ok = await asyncio.to_thread(set_db_value, "boss_schedule", firebase_data)
    await asyncio.to_thread(save_json_local, DATA_FILE, firebase_data)
    if not firebase_ok:
        print("⚠️ boss_schedule ถูกเก็บไว้ใน local fallback เท่านั้น — ตรวจ FIREBASE_SERVICE_ACCOUNT_JSON / FIREBASE_DATABASE_URL")
    return firebase_ok

async def load_boss_data():
    global boss_schedule
    saved_data = None
    try:
        saved_data = await asyncio.to_thread(db.reference("boss_schedule").get)
    except Exception as e:
        print(f"⚠️ ดึง boss_schedule จาก Firebase ไม่สำเร็จ: {e}")
    if not saved_data:
        saved_data = get_db_value("boss_schedule", None)
    if not isinstance(saved_data, dict):
        saved_data = {}
    with schedule_lock:
        boss_schedule.clear()
        for boss_name, data in saved_data.items():
            if not isinstance(data, dict):
                continue
            canonical = get_boss_canonical_name(boss_name)
            internal = _firebase_to_internal(canonical, data)
            if internal:
                boss_schedule[canonical] = internal
    print(f"✅ โหลด boss_schedule จาก Firebase สำเร็จ {len(boss_schedule)} รายการ")

def start_firebase_listener(loop):
    """Safe listener: always read the boss_schedule root, never trust event.data as the full tree."""
    def listener(event):
        global is_updating_from_bot
        if not is_bot_ready or is_updating_from_bot:
            return
        try:
            snapshot = db.reference("boss_schedule").get()
            if not isinstance(snapshot, dict):
                snapshot = {}
            new_schedule = {}
            for boss_name, data in snapshot.items():
                if not isinstance(data, dict):
                    continue
                canonical = get_boss_canonical_name(boss_name)
                internal = _firebase_to_internal(canonical, data)
                if internal:
                    new_schedule[canonical] = internal
            with schedule_lock:
                boss_schedule.clear()
                boss_schedule.update(new_schedule)
            print(f"🔄 Firebase boss_schedule sync: {len(new_schedule)} รายการ")
        except Exception as e:
            print(f"❌ Firebase Listener boss_schedule ผิดพลาด: {e}")
    try:
        db.reference("boss_schedule").listen(listener)
        print("🟢 Firebase Listener พร้อมทำงานแบบ safe root-sync")
    except Exception as e:
        print(f"❌ ไม่สามารถเปิด Firebase Listener ได้: {e}")

async def save_voice_config():
    """Persist Voice configuration to Firebase and local fallback."""
    with schedule_lock:
        data = {str(gid): dict(cfg) for gid, cfg in voice_config.items()}
    firebase_ok = False
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("voice_config").set, data),
            timeout=12
        )
        firebase_ok = True
        print(f"☁️ Firebase voice_config บันทึกสำเร็จ: {len(data)} เซิร์ฟเวอร์")
    except Exception as e:
        print(f"❌ บันทึก voice_config ลง Firebase ไม่สำเร็จ: {e}")
    sqlite_ok = await asyncio.to_thread(set_db_value, "voice_config", data)
    await asyncio.to_thread(save_json_local, VOICE_CONFIG_FILE, data)
    return firebase_ok or sqlite_ok

async def load_voice_config():
    """โหลดห้อง Voice ที่ตั้งไว้จาก Firebase เมื่อบอทเริ่มทำงาน"""
    global voice_config
    data = None
    try:
        data = await asyncio.to_thread(db.reference("voice_config").get)
    except Exception as e:
        print(f"⚠️ โหลด voice_config จาก Firebase ไม่สำเร็จ: {e}")
    if not data:
        data = get_db_value("voice_config", None)
    if isinstance(data, dict):
        normalized = {}
        for gid, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            channel_id = cfg.get("voice_channel_id") or cfg.get("voiceChannelId")
            try:
                channel_id = int(channel_id) if channel_id is not None else None
            except (TypeError, ValueError):
                channel_id = None
            if channel_id:
                normalized[str(gid)] = {
                    "voice_channel_id": channel_id,
                    "guild_id": int(gid) if str(gid).isdigit() else gid,
                    "channel_name": cfg.get("channel_name", ""),
                    "enabled": parse_bool(cfg.get("enabled", True), True),
                    "updated_by": cfg.get("updated_by", ""),
                    "updated_at": cfg.get("updated_at", "")
                }
        voice_config = normalized
    print(f"🔊 โหลด voice_config สำเร็จ {len(voice_config)} เซิร์ฟเวอร์")


def get_configured_voice_channel(guild: discord.Guild):
    if not guild:
        return None
    cfg = voice_config.get(str(guild.id))
    if not cfg or not parse_bool(cfg.get("enabled", True), True):
        return None
    channel_id = cfg.get("voice_channel_id")
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.VoiceChannel):
        return channel
    return None

async def ensure_configured_voice(guild: discord.Guild):
    """เชื่อมต่อ/ย้ายไปห้องที่ตั้งไว้ โดยใช้ lock + cooldown ป้องกัน connect/disconnect loop"""
    if not guild:
        return None
    target = get_configured_voice_channel(guild)
    if not target:
        return guild.voice_client if guild.voice_client and guild.voice_client.is_connected() else None

    if guild.id not in voice_connect_locks:
        voice_connect_locks[guild.id] = asyncio.Lock()

    async with voice_connect_locks[guild.id]:
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id == target.id:
                return vc
            # ย้ายครั้งเดียว ไม่ disconnect/connect ใหม่
            try:
                await vc.move_to(target)
                return vc
            except Exception as e:
                print(f"⚠️ ย้าย Voice ไป {target.name} ไม่สำเร็จ: {e}")
                return None

        now = time.monotonic()
        last_attempt = last_voice_connect_attempt.get(guild.id, 0.0)
        if now - last_attempt < 30:
            return None
        last_voice_connect_attempt[guild.id] = now
        try:
            vc = await target.connect(reconnect=True, timeout=20)
            print(f"🔊 เชื่อมต่อ Voice ถาวร: {guild.name} -> {target.name}")
            return vc
        except discord.ClientException as e:
            # ถ้ามี client เก่าค้างอยู่ ให้รอ reconnect event แทนการยิง connect ซ้ำ
            print(f"⚠️ Discord Voice Client ยังไม่พร้อมใน {guild.name}: {e}")
        except Exception as e:
            print(f"❌ เชื่อมต่อ Voice {guild.name}/{target.name} ไม่สำเร็จ: {e}")
        return None

async def save_bot_settings():
    settings_data = {
        "bf_notify_enabled": bf_notify_enabled,
        "lib_notify_enabled": lib_notify_enabled,
        "ppl_notify_enabled": ppl_notify_enabled,
        "tts_th_enabled": tts_th_enabled,
        "tts_en_enabled": tts_en_enabled,
        "tts_ko_enabled": tts_ko_enabled
    }
    firebase_ok = False
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("bot_settings").set, settings_data),
            timeout=12
        )
        firebase_ok = True
        print("☁️ Firebase bot_settings บันทึกสำเร็จ")
    except Exception as e:
        print(f"❌ บันทึก bot_settings ลง Firebase ไม่สำเร็จ: {e}")
    for key, value in settings_data.items():
        await asyncio.to_thread(set_db_value, key, value)
    await asyncio.to_thread(save_json_local, SETTINGS_FILE, settings_data)
    return firebase_ok

async def load_bot_settings():
    global bf_notify_enabled, lib_notify_enabled, ppl_notify_enabled
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    data = None
    try: data = await asyncio.to_thread(db.reference('bot_settings').get)
    except Exception: pass

    if not data:
        db_bf = get_db_value("bf_notify_enabled", None)
        db_lib = get_db_value("lib_notify_enabled", None)
        db_ppl = get_db_value("ppl_notify_enabled", None)
        db_th = get_db_value("tts_th_enabled", None)
        db_en = get_db_value("tts_en_enabled", None)
        db_ko = get_db_value("tts_ko_enabled", None)
        if db_bf is not None: bf_notify_enabled = parse_bool(db_bf, True)
        if db_lib is not None: lib_notify_enabled = parse_bool(db_lib, True)
        if db_ppl is not None: ppl_notify_enabled = parse_bool(db_ppl, True)
        if db_th is not None: tts_th_enabled = parse_bool(db_th, True)
        if db_en is not None: tts_en_enabled = parse_bool(db_en, True)
        if db_ko is not None: tts_ko_enabled = parse_bool(db_ko, True)
        return

    if data:
        bf_notify_enabled = parse_bool(data.get("bf_notify_enabled", True), True)
        lib_notify_enabled = parse_bool(data.get("lib_notify_enabled", True), True)
        ppl_notify_enabled = parse_bool(data.get("ppl_notify_enabled", True), True)
        tts_th_enabled = parse_bool(data.get("tts_th_enabled", True), True)
        tts_en_enabled = parse_bool(data.get("tts_en_enabled", True), True)
        tts_ko_enabled = parse_bool(data.get("tts_ko_enabled", True), True)

async def save_vip_config():
    try: await asyncio.to_thread(db.reference('vip_config').set, vip_config)
    except Exception: pass
    await asyncio.to_thread(set_db_value, "vip_config", vip_config)
    await asyncio.to_thread(save_json_local, VIP_CONFIG_FILE, vip_config)

async def load_vip_config():
    global vip_config
    data = None
    try: data = await asyncio.to_thread(db.reference('vip_config').get)
    except Exception: pass
    if not data: data = get_db_value("vip_config", None)
    if data: vip_config = data

async def save_live_config():
    try: await asyncio.to_thread(db.reference('live_message_config').set, live_message_config)
    except Exception: pass
    await asyncio.to_thread(set_db_value, "live_message_config", live_message_config)
    await asyncio.to_thread(save_json_local, LIVE_CONFIG_FILE, live_message_config)

async def load_live_config():
    global live_message_config
    data = None
    try: data = await asyncio.to_thread(db.reference('live_message_config').get)
    except Exception: pass
    if not data: data = get_db_value("live_message_config", None)
    if data: live_message_config = data

async def save_custom_bosses_to_github():
    custom_data = {}
    for name in list(BOSS_RESPAWN_TIMES.keys()):
        if name not in DEFAULT_BOSS_NAMES:
            custom_data[name] = {
                "total_seconds": int(BOSS_RESPAWN_TIMES[name].total_seconds()),
                "cd_text": BOSS_CD_TEXT[name],
                "notice_seconds": ADVANCE_NOTICE_SECONDS[name],
                "notice_text": ADVANCE_NOTICE_TEXT[name]
            }
    try: await asyncio.to_thread(db.reference('custom_bosses').set, custom_data)
    except Exception: pass
    await asyncio.to_thread(set_db_value, "custom_bosses", custom_data)
    await asyncio.to_thread(save_json_local, CUSTOM_BOSSES_FILE, custom_data)

async def load_custom_bosses():
    custom_data = None
    try:
        custom_data = await asyncio.to_thread(db.reference("custom_bosses").get)
    except Exception as e:
        print(f"⚠️ อ่าน custom_bosses จาก Firebase ไม่สำเร็จ: {e}")

    if not custom_data:
        custom_data = get_db_value("custom_bosses", None)

    if not isinstance(custom_data, dict):
        if custom_data is not None:
            print("⚠️ custom_bosses ไม่ใช่ object — ข้ามข้อมูลเก่า/placeholder")
        return

    for boss_name, data in custom_data.items():
        if not isinstance(data, dict):
            print(f"⚠️ ข้าม custom_bosses[{boss_name!r}] เพราะไม่ใช่ object")
            continue

        total_seconds = data.get("total_seconds")
        if not isinstance(total_seconds, (int, float)) or total_seconds <= 0:
            print(f"⚠️ ข้าม custom boss {boss_name!r}: total_seconds ไม่ถูกต้อง")
            continue

        notice_seconds = data.get("notice_seconds", 300)
        if not isinstance(notice_seconds, (int, float)) or notice_seconds < 0:
            notice_seconds = 300

        cd_text = data.get("cd_text")
        if not isinstance(cd_text, str) or not cd_text.strip():
            cd_text = f"{int(total_seconds // 60)} นาที"

        notice_text = data.get("notice_text")
        if not isinstance(notice_text, str) or not notice_text.strip():
            notice_text = f"{int(notice_seconds // 60)} นาที"

        boss_name = str(boss_name)
        BOSS_RESPAWN_TIMES[boss_name] = timedelta(seconds=float(total_seconds))
        BOSS_CD_TEXT[boss_name] = cd_text
        ADVANCE_NOTICE_SECONDS[boss_name] = int(notice_seconds)
        ADVANCE_NOTICE_TEXT[boss_name] = notice_text
        if boss_name not in BOSS_PRONUNCIATION:
            BOSS_PRONUNCIATION[boss_name] = boss_name

# ==========================================
# 🤖 6. Discord Bot Setup & Voice Helper
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents)

async def setup_hook():
    bot.add_view(QuickActionsView())
    try:
        synced = await bot.tree.sync()
        print(f"✅ ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง (จาก setup_hook)")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")

bot.setup_hook = setup_hook

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(title="🚫 ปฏิเสธการเข้าถึง", description="คำสั่งนี้อนุญาตเฉพาะ **Administrator (ผู้ดูแลระบบ)**เท่านั้นครับ!", color=discord.Color.red())
        if interaction.response.is_done(): await interaction.followup.send(embed=embed, ephemeral=True)
        else: await interaction.response.send_message(embed=embed, ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        embed = discord.Embed(title="🚫 ปฏิเสธการเข้าถึง", description="คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้!\nอนุญาตเฉพาะผู้ได้รับสิทธิ์หรือมีบทบาทที่กำหนดเท่านั้นครับ", color=discord.Color.red())
        if interaction.response.is_done(): await interaction.followup.send(embed=embed, ephemeral=True)
        else: await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        print(f"❌ เกิดข้อผิดพลาดของระบบ: {error}")
        traceback.print_exc()
        try:
            message = "❌ คำสั่งเกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception as handler_error:
            print(f"❌ ไม่สามารถส่ง error response ให้ Discord ได้: {handler_error}")

def get_ffmpeg_path():
    try: return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e: print(f"⚠️ ไม่สามารถโหลด FFmpeg จาก imageio-ffmpeg ได้: {e}")
    cwd = os.getcwd()
    for filename in ["ffmpeg.exe", "ffmpeg"]:
        local_path = os.path.join(cwd, filename)
        if os.path.exists(local_path): return local_path
    for filename in ["ffmpeg.exe", "ffmpeg"]:
        bin_path = os.path.join(cwd, "ffmpeg", "bin", filename)
        if os.path.exists(bin_path): return bin_path
    system_path = shutil.which("ffmpeg")
    if system_path: return system_path
    return "ffmpeg"

def clean_display_name(name: str) -> str:
    if not name: return "สมาชิก"
    cleaned = re.sub(r'[^\w\s\u0E00-\u0E7F]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else "สมาชิก"

# 🔥 ฟังก์ชันแจ้งเตือนด้วยเสียง (ตรวจสอบสถานะเปิด-ปิด TTS แต่ละภาษาก่อนเล่น)
async def speak_in_guild(guild: discord.Guild, text_th: str = None, text_en: str = None, text_ko: str = None, target_channel: discord.VoiceChannel = None):
    """พูด TTS ในห้องที่ตั้งค่าไว้ โดยคง Voice connection ไว้และไม่ disconnect หลังพูด"""
    if not guild:
        return

    actual_text_th = text_th if (tts_th_enabled and text_th) else None
    actual_text_en = text_en if (tts_en_enabled and text_en) else None
    actual_text_ko = text_ko if (tts_ko_enabled and text_ko) else None
    if not (actual_text_th or actual_text_en or actual_text_ko):
        print(f"⚠️ TTS ถูกปิดทุกภาษาใน {guild.name}: TH={tts_th_enabled} EN={tts_en_enabled} KO={tts_ko_enabled}")
        return

    if guild.id not in voice_locks:
        voice_locks[guild.id] = asyncio.Lock()

    async with voice_locks[guild.id]:
        configured = get_configured_voice_channel(guild)
        channel = target_channel or configured
        if channel is None:
            vc_existing = guild.voice_client
            if vc_existing and vc_existing.is_connected():
                channel = vc_existing.channel
        if channel is None:
            cfg = voice_config.get(str(guild.id))
            print(f"⚠️ ไม่พบห้อง Voice สำหรับ {guild.name}: config={cfg!r}; กรุณาใช้ /setvoice อีกครั้ง")
            return

        # ถ้าเป็นห้องที่ตั้งค่าไว้ ให้ ensure connection; ถ้า target_channel เป็นห้องชั่วคราวก็ใช้ connection เดิม/ย้ายได้
        if configured is not None:
            vc = await ensure_configured_voice(guild)
        else:
            vc = guild.voice_client
            if vc and vc.is_connected() and vc.channel.id != channel.id:
                try:
                    await vc.move_to(channel)
                except Exception as e:
                    print(f"⚠️ ย้าย Voice ไป {channel.name} ไม่สำเร็จ: {e}")
                    return
            elif vc is None:
                # ไม่สร้าง connection ชั่วคราวถ้ายังไม่มี /setvoice
                print(f"⚠️ ห้อง {channel.name} ยังไม่มี connection และยังไม่ได้ตั้ง /setvoice")
                return

        if not vc or not vc.is_connected():
            return

        unique_id = uuid.uuid4().hex
        tts_files = []
        if actual_text_th: tts_files.append(("th", actual_text_th, VOICE_THAI, f"temp_tts_th_{guild.id}_{unique_id}.mp3", "-20%", "+10Hz"))
        if actual_text_en: tts_files.append(("en", actual_text_en, VOICE_ENG, f"temp_tts_en_{guild.id}_{unique_id}.mp3", "-10%", "+0Hz"))
        if actual_text_ko: tts_files.append(("ko", actual_text_ko, VOICE_KOR, f"temp_tts_ko_{guild.id}_{unique_id}.mp3", "-10%", "+0Hz"))

        try:
            # Current edge-tts manages its own HTTP session. Passing session=
            # breaks on newer Render installations, so keep the constructor minimal.
            for _, text, voice, filename, rate, pitch in tts_files:
                try:
                    communicator = edge_tts.Communicate(
                        text, voice, rate=rate, pitch=pitch
                    )
                    await communicator.save(filename)
                    if not os.path.exists(filename) or os.path.getsize(filename) <= 0:
                        print(f"❌ TTS ได้ไฟล์ว่าง ({_})")
                except Exception as e:
                    print(f"❌ สร้าง TTS ไม่สำเร็จ ({_}): {e}")

            ffmpeg_executable = get_ffmpeg_path()
            loop = asyncio.get_running_loop()
            for idx, (_, _, _, filename, _, _) in enumerate(tts_files):
                if not os.path.exists(filename) or os.path.getsize(filename) <= 0:
                    continue
                if not vc.is_connected():
                    break
                if vc.is_playing():
                    vc.stop()
                    await asyncio.sleep(0.2)

                finished = asyncio.Event()
                def after_playing(error, event=finished, lang=tts_files[idx][0]):
                    if error:
                        print(f"❌ เล่น TTS {lang} ผิดพลาดใน {guild.name}: {error}")
                    loop.call_soon_threadsafe(event.set)

                source = discord.FFmpegPCMAudio(filename, executable=ffmpeg_executable, before_options="-loglevel error", options="-vn")
                try:
                    vc.play(source, after=after_playing)
                    await asyncio.wait_for(finished.wait(), timeout=45)
                except asyncio.TimeoutError:
                    print(f"⚠️ TTS timeout ใน {guild.name}")
                    if vc.is_playing():
                        vc.stop()
                except Exception as e:
                    print(f"❌ เล่นเสียง TTS ไม่สำเร็จใน {guild.name}: {e}")
                if idx < len(tts_files) - 1:
                    await asyncio.sleep(0.4)
        finally:
            for _, _, _, filename, _, _ in tts_files:
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                except Exception:
                    pass

# ==========================================
# 🔊 Event แจ้งเตือน + ทักทายเมื่อมีคนเข้าห้องเสียง
# ==========================================
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    global ppl_notify_enabled, vip_config
    if member.bot: return

    if before.channel != after.channel and after.channel is not None:
        if vip_config.get("enabled", False) and member.id == vip_config.get("user_id"):
            greeting_text = vip_config.get("message", "")
            if greeting_text: asyncio.create_task(speak_in_guild(member.guild, text_th=greeting_text, target_channel=after.channel))
        elif ppl_notify_enabled:
            user_name = clean_display_name(member.display_name)
            channel_name = clean_display_name(after.channel.name)
            greeting_text_th = f"ยินดีต้อนรับคุณ {user_name} เข้าสู่ห้อง{channel_name}"
            greeting_text_en = f"Welcome {user_name} to {channel_name}."
            greeting_text_ko = f"{user_name}님, {channel_name} 방에 오신 것을 환영합니다."
            asyncio.create_task(speak_in_guild(member.guild, text_th=greeting_text_th, text_en=greeting_text_en, text_ko=greeting_text_ko, target_channel=after.channel))

@bot.event
async def on_ready():
    global is_bot_ready
    if is_bot_ready:
        print("🔄 บอท Reconnect สำเร็จ (ข้ามการโหลดข้อมูลซ้ำ)")
        return
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🔊 ใช้ FFmpeg จากตำแหน่ง: {get_ffmpeg_path()}")
    print(f"☁️ Firebase URL: {DATABASE_URL}")
    print(f"☁️ Firebase Admin credential loaded: {bool(FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_BASE64)}")

    init_db()

    # ==========================================
    # 🔥 โหลด Firebase แต่ละส่วนแยกกัน
    # เพื่อไม่ให้ Firebase จุดหนึ่งผิดพลาด
    # แล้วทำให้ Discord Bot ทั้งหมดหยุด
    # ==========================================

    try:
        await load_bot_settings()
        print("✅ load_bot_settings สำเร็จ")
    except Exception as e:
        print(f"❌ load_bot_settings ERROR: {e}")
        traceback.print_exc()

    try:
        await load_custom_bosses()
        print("✅ load_custom_bosses สำเร็จ")
    except Exception as e:
        print(f"❌ load_custom_bosses ERROR: {e}")
        traceback.print_exc()

    try:
        await load_boss_data()
        print("✅ load_boss_data สำเร็จ")
    except Exception as e:
        print(f"❌ load_boss_data ERROR: {e}")
        traceback.print_exc()

    try:
        await load_live_config()
        print("✅ load_live_config สำเร็จ")
    except Exception as e:
        print(f"❌ load_live_config ERROR: {e}")
        traceback.print_exc()

    try:
        await load_vip_config()
        print("✅ load_vip_config สำเร็จ")
    except Exception as e:
        print(f"❌ load_vip_config ERROR: {e}")
        traceback.print_exc()

    try:
        await load_voice_config()
        print("✅ load_voice_config สำเร็จ")
    except Exception as e:
        print(f"❌ load_voice_config ERROR: {e}")
        traceback.print_exc()

    # ==========================================
    # 🔊 เชื่อมต่อ Voice Channel ที่บันทึกไว้
    # ==========================================

    for guild in bot.guilds:
        try:
            if get_configured_voice_channel(guild):
                asyncio.create_task(
                    ensure_configured_voice(guild)
                )
        except Exception as e:
            print(
                f"❌ เชื่อมต่อ Voice ของ "
                f"{guild.name} ไม่สำเร็จ: {e}"
            )

    # ==========================================
    # ⏳ รอให้ข้อมูล Firebase โหลดเสร็จ
    # ==========================================

    await asyncio.sleep(3)

    # ==========================================
    # ⏰ เริ่ม Background Tasks
    # ==========================================

    if not check_boss_notifications.is_running():
        check_boss_notifications.start()

    if not check_bf_notifications.is_running():
        check_bf_notifications.start()

    if not check_library_boss_notifications.is_running():
        check_library_boss_notifications.start()

    if not update_live_embed.is_running():
        update_live_embed.start()

    if not check_auto_disconnect.is_running():
        check_auto_disconnect.start()

    # ==========================================
    # 🟢 Bot พร้อมใช้งาน
    # ==========================================

    is_bot_ready = True

    loop = asyncio.get_running_loop()

    threading.Thread(
        target=start_firebase_listener,
        args=(loop,),
        daemon=True
    ).start()

    print("=" * 60)
    print("🟢 DISCORD BOT READY")
    print(f"🤖 Bot: {bot.user}")
    print(f"🏠 Guilds: {len(bot.guilds)}")
    print("=" * 60)
# ==========================================
# ⏰ 7. Tasks เช็กเวลาเตือน + BF + Library Boss + Live Embed + Auto-Disconnect
# ==========================================
@tasks.loop(seconds=30)
async def check_bf_notifications():
    global last_bf_notified_hour, bf_notify_enabled
    if not bf_notify_enabled: return

    try:
        now = datetime.now(TZ_THAI)
        if now.hour % 2 == 1 and now.minute == 57:
            if last_bf_notified_hour != now.hour:
                last_bf_notified_hour = now.hour
                next_bf_hour = (now.hour + 1) % 24
                next_bf_time = f"{next_bf_hour:02d}:00"
                
                for guild in bot.guilds:
                    mentions = []
                    for role_id in BF_ROLE_IDS:
                        role = guild.get_role(role_id)
                        if role: mentions.append(role.mention)
                    mention_target = " ".join(mentions) if mentions else ""

                    channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                    if not channel:
                        channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                    
                    if channel:
                        embed = discord.Embed(
                            title="⚔️ แจ้งเตือนสงคราม Battlefield (BF)!",
                            description=f"สนามรบ **BF** กำลังจะเริ่มในอีก **3 นาที** (เวลา **{next_bf_time} น.**)!\nเตรียมตัวเข้าประจำที่ได้เลยครับ!",
                            color=discord.Color.red()
                        )
                        try:
                            send_content = mention_target if mention_target.strip() else None
                            await channel.send(content=send_content, embed=embed)
                        except Exception as e: print(f"❌ ส่งข้อความเตือน BF ไม่สำเร็จ: {e}")
                    
                    spoken_text_th = "Battlefield กำลังจะเริ่มในอีก 3 นาทีค่ะ"
                    spoken_text_en = "Battlefield will start in 3 minutes."
                    spoken_text_ko = "배틀필드가 3분 후에 시작됩니다."
                    asyncio.create_task(speak_in_guild(guild, text_th=spoken_text_th, text_en=spoken_text_en, text_ko=spoken_text_ko))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_bf_notifications': {e}")

@tasks.loop(seconds=30)
async def check_library_boss_notifications():
    global last_lib_notified_key, lib_notify_enabled
    if not lib_notify_enabled: return

    try:
        now = datetime.now(TZ_THAI)
        if (now.hour == 8 and now.minute == 50) or (now.hour == 20 and now.minute == 50):
            current_key = f"{now.strftime('%Y-%m-%d')}_{now.hour}:{now.minute}"
            if last_lib_notified_key != current_key:
                last_lib_notified_key = current_key
                time_str = "08:50 น." if now.hour == 8 else "20:50 น."

                for guild in bot.guilds:
                    mentions = []
                    for role_id in BF_ROLE_IDS:
                        role = guild.get_role(role_id)
                        if role: mentions.append(role.mention)
                    mention_target = " ".join(mentions) if mentions else ""

                    channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                    if not channel:
                        channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)

                    if channel:
                        embed = discord.Embed(
                            title="⚔️ แจ้งเตือน Library Boss!",
                            description=f"บอส **Library Boss** ถึงเวลาเตรียมตัวแล้ว! (เวลา **{time_str}**)\nเตรียมตัวเข้าประจำที่ได้เลยครับ!",
                            color=discord.Color.purple()
                        )
                        try:
                            send_content = mention_target if mention_target.strip() else None
                            await channel.send(content=send_content, embed=embed)
                        except Exception as e: print(f"❌ ส่งข้อความเตือน Library Boss ไม่สำเร็จ: {e}")

                    spoken_text_th = "Library Boss ถึงเวลาเตรียมตัวแล้วค่ะ"
                    spoken_text_en = "It's time to prepare for Library Boss."
                    spoken_text_ko = "도서관 보스 준비 시간입니다."
                    asyncio.create_task(speak_in_guild(guild, text_th=spoken_text_th, text_en=spoken_text_en, text_ko=spoken_text_ko))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_library_boss_notifications': {e}")

@tasks.loop(seconds=10)
async def check_boss_notifications():
    try:
        now = datetime.now(TZ_THAI)
        changed = False
        
        with schedule_lock:
            schedule_copy = boss_schedule.copy()
        
        for boss_name, data in list(schedule_copy.items()):
            spawn_time = parse_to_thai_datetime(data["spawn_time"])
            if not spawn_time: continue

            channel_id = data.get("channel_id")
            if channel_id is not None:
                try: channel_id = int(channel_id)
                except ValueError: channel_id = None

            notified_advance = parse_bool(data.get("notified_advance", False))
            notified_spawn = parse_bool(data.get("notified_spawn", False))

            channel = None
            if channel_id:
                channel = bot.get_channel(channel_id)
                if not channel:
                    # อย่า fetch Discord API ทุก 10 วินาทีเมื่อ channel หาไม่เจอ
                    now_mono = time.monotonic()
                    last_fetch = last_channel_fetch_attempt.get(channel_id, 0.0)
                    if now_mono - last_fetch >= 300:
                        last_channel_fetch_attempt[channel_id] = now_mono
                        try:
                            channel = await bot.fetch_channel(channel_id)
                        except Exception:
                            channel = None

            channels_to_notify = []
            if channel:
                channels_to_notify.append(channel)
            else:
                for guild in bot.guilds:
                    fb_channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                    if not fb_channel:
                        fb_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                    if fb_channel:
                        channels_to_notify.append(fb_channel)

            time_left = (spawn_time - now).total_seconds()
            notice_limit = get_boss_advance_notice_seconds(boss_name)
            notice_text = get_boss_advance_notice_text(boss_name)
            notice_text_en = get_boss_advance_notice_text_en(boss_name)
            notice_text_ko = get_boss_advance_notice_text_ko(boss_name)
            spoken_name = get_boss_pronunciation(boss_name)
            
            if 0 < time_left <= notice_limit and not notified_advance:
                embed = discord.Embed(
                    title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
                    description=f"บอส **{boss_name}** จะเกิดในอีก **{notice_text}**!\nเวลาเกิด: **{spawn_time.strftime('%H:%M:%S น.')}**",
                    color=discord.Color.gold()
                )
                
                notified_guild_ids = set()
                
                for ch in channels_to_notify:
                    guild = ch.guild if hasattr(ch, "guild") else None
                    mentions = []
                    if guild:
                        for role_id in TARGET_ROLE_IDS:
                            role = guild.get_role(role_id)
                            if role: mentions.append(role.mention)
                    
                    mention_target = " ".join(mentions) if mentions else ""
                    send_content = mention_target if mention_target.strip() else None
                    try:
                        await ch.send(content=send_content, embed=embed)
                    except Exception as e:
                        print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")

                target_guilds_for_voice = set()
                if channel and hasattr(channel, "guild") and channel.guild:
                    target_guilds_for_voice.add(channel.guild)
                else:
                    for g in bot.guilds:
                        target_guilds_for_voice.add(g)

                for guild in target_guilds_for_voice:
                    if guild.id not in notified_guild_ids:
                        spoken_th = f"บอส {spoken_name} จะเกิดในอีก {notice_text} ค่ะ"
                        spoken_en = f"Boss {boss_name} will spawn in {notice_text_en}."
                        spoken_ko = f"보스 {boss_name}가 {notice_text_ko} 후에 나타납니다."
                        asyncio.create_task(speak_in_guild(guild, text_th=spoken_th, text_en=spoken_en, text_ko=spoken_ko))
                        notified_guild_ids.add(guild.id)
                    
                with schedule_lock:
                    if boss_name in boss_schedule:
                        boss_schedule[boss_name]["notified_advance"] = True
                changed = True

            if time_left <= 0 and not notified_spawn:
                embed = discord.Embed(
                    title="⚔️ บอสเกิดแล้ว!",
                    description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
                    color=discord.Color.green()
                )
                
                notified_guild_ids = set()
                
                for ch in channels_to_notify:
                    guild = ch.guild if hasattr(ch, "guild") else None
                    mentions = []
                    if guild:
                        for role_id in TARGET_ROLE_IDS:
                            role = guild.get_role(role_id)
                            if role: mentions.append(role.mention)
                    
                    mention_target = " ".join(mentions) if mentions else ""
                    send_content = mention_target if mention_target.strip() else None
                    try:
                        await ch.send(content=send_content, embed=embed)
                    except Exception as e:
                        print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")

                target_guilds_for_voice = set()
                if channel and hasattr(channel, "guild") and channel.guild:
                    target_guilds_for_voice.add(channel.guild)
                else:
                    for g in bot.guilds:
                        target_guilds_for_voice.add(g)

                for guild in target_guilds_for_voice:
                    if guild.id not in notified_guild_ids:
                        spoken_th = f"บอส {spoken_name} เกิดแล้วค่ะ"
                        spoken_en = f"Boss {boss_name} has spawned."
                        spoken_ko = f"보스 {boss_name}가 나타났습니다."
                        asyncio.create_task(speak_in_guild(guild, text_th=spoken_th, text_en=spoken_en, text_ko=spoken_ko))
                        notified_guild_ids.add(guild.id)
                    
                with schedule_lock:
                    if boss_name in boss_schedule:
                        boss_schedule[boss_name]["notified_spawn"] = True
                changed = True

        if changed:
            await save_boss_data()
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_boss_notifications': {e}")

@tasks.loop(seconds=60)
async def update_live_embed():
    global cached_live_message
    try:
        if not live_message_config: return
        channel_id = live_message_config.get("channel_id")
        message_id = live_message_config.get("message_id")
        if not channel_id or not message_id: return

        if cached_live_message is None or cached_live_message.id != message_id:
            channel = bot.get_channel(channel_id)
            if not channel:
                try: channel = await bot.fetch_channel(channel_id)
                except Exception: return
            try: cached_live_message = await channel.fetch_message(message_id)
            except Exception: return

        now = datetime.now(TZ_THAI)
        embed = discord.Embed(title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time", description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.teal())

        with schedule_lock:
            schedule_copy = boss_schedule.copy()

        if not schedule_copy:
            embed.add_field(name="📌 สถานะ", value="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ", inline=False)
        else:
            sorted_bosses = sorted(
                schedule_copy.items(), 
                key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now
            )
            
            display_bosses = sorted_bosses[:20]
            for boss, data in display_bosses:
                spawn_time = parse_to_thai_datetime(data["spawn_time"])
                if not spawn_time: continue
                time_left_sec = (spawn_time - now).total_seconds()
                
                if time_left_sec <= 0: time_left_str = "เกิดแล้ว!"
                else:
                    m, s = divmod(int(time_left_sec), 60)
                    h, m = divmod(m, 60)
                    if h > 0: time_left_str = f"อีก {h} ชม. {m} นาที"
                    else: time_left_str = f"อีก {m} นาที {s} วินาที"

                notice_text = get_boss_advance_notice_text(boss)
                rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"
                embed.add_field(
                    name=f"👾 {boss}",
                    value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**\n*(ผู้บันทึก: {rec_by} | เตือนล่วงหน้า {notice_text})*",
                    inline=False
                )
            
            if len(sorted_bosses) > 20:
                embed.add_field(name="📌 หมายเหตุ", value=f"*และยังมีบอสอีก {len(sorted_bosses) - 20} ตัวในคิว*", inline=False)

        embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")
        try: await cached_live_message.edit(embed=embed)
        except Exception as e: print(f"❌ อัปเดต Live Embed ไม่สำเร็จ: {e}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'update_live_embed': {e}")

@tasks.loop(seconds=60)
async def check_auto_disconnect():
    try:
        now = datetime.now(TZ_THAI)
        for guild in bot.guilds:
            vc = guild.voice_client
            if vc and vc.is_connected() and vc.channel:
                human_members = [m for m in vc.channel.members if not m.bot]
                # ห้องที่ตั้งด้วย /setvoice ต้องคง connection ไว้ ไม่ให้ task นี้ตัดสาย
                configured = get_configured_voice_channel(guild)
                if configured and vc.channel and vc.channel.id == configured.id:
                    if guild.id in voice_empty_start:
                        del voice_empty_start[guild.id]
                    continue

                if len(human_members) == 0:
                    if guild.id not in voice_empty_start:
                        voice_empty_start[guild.id] = now
                    else:
                        elapsed = (now - voice_empty_start[guild.id]).total_seconds()
                        if elapsed >= 180:
                            try:
                                await vc.disconnect()
                                print(f"🔌 Auto-disconnected จาก {vc.channel.name} เนื่องจากไม่มีสมาชิกอยู่ในห้องเกิน 3 นาที")
                            except Exception as e: print(f"❌ ตัดสายไม่สำเร็จ: {e}")
                            del voice_empty_start[guild.id]
                else:
                    if guild.id in voice_empty_start:
                        del voice_empty_start[guild.id]
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_auto_disconnect': {e}")

async def boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Discord autocomplete must finish fast and return <=25 valid choices."""
    try:
        needle = (current or "").strip().casefold()
        names = sorted({str(x).strip() for x in BOSS_RESPAWN_TIMES.keys() if str(x).strip()}, key=str.casefold)
        if needle:
            names = [x for x in names if needle in x.casefold()]
        result = []
        for boss in names[:25]:
            # Discord Choice name/value max 100 chars.
            value = boss[:100]
            label = boss[:100]
            result.append(app_commands.Choice(name=label, value=value))
        return result
    except Exception as e:
        print(f"⚠️ boss autocomplete error: {e}")
        return []

# ==========================================
# 🎛️ 8. Dynamic Boss Select & Quick Actions
# ==========================================
class BossSelect(discord.ui.Select):
    def __init__(self, boss_chunk: list, placeholder: str, custom_id: str, row: int):
        options = [discord.SelectOption(label=boss, value=boss) for boss in boss_chunk]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, custom_id=custom_id, row=row)
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_boss = self.values[0]
        await interaction.response.send_message(f"🎯 เลือกบอส: **{self.values[0]}** เรียบร้อยแล้ว (กดปุ่ม ⚔️ บอสตายแล้ว เพื่อระบุเวลาได้ทันที)", ephemeral=True)

class KillBossModal(discord.ui.Modal, title="⚔️ บันทึกเวลาบอสตาย"):
    kill_time_input = discord.ui.TextInput(
        label="เวลาที่บอสตาย (ไม่ใส่ = เวลาปัจจุบัน)", style=discord.TextStyle.short,
        placeholder="ตัวอย่าง: 17:30 หรือ 1730 (ปล่อยว่างได้)", required=False, max_length=8
    )
    def __init__(self, selected_boss: str):
        super().__init__()
        self.selected_boss = selected_boss
    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.now(TZ_THAI)
        raw_val = self.kill_time_input.value.strip() if self.kill_time_input.value else ""
        try: boss_died_at = parse_time_input(raw_val, now)
        except Exception:
            await interaction.response.send_message("❌ รูปแบบเวลาไม่ถูกต้อง! กรุณากรอกแบบ **17:30** หรือ **1730** (หรือเว้นว่างไว้เพื่อใช้เวลาปัจจุบัน)", ephemeral=True)
            return

        await interaction.response.defer()
        canonical_name = get_boss_canonical_name(self.selected_boss)
        respawn_time = get_boss_respawn_time(canonical_name)
        next_spawn = boss_died_at + respawn_time
        is_already_past = next_spawn <= now
        user_name = interaction.user.display_name

        with schedule_lock:
            boss_schedule[canonical_name] = {
                "spawn_time": next_spawn,
                "killTimeMs": int(boss_died_at.timestamp() * 1000),
                "channel_id": interaction.channel_id,
                "notified_advance": is_already_past,
                "notified_spawn": is_already_past,
                "noticeMinutes": int(get_boss_advance_notice_seconds(canonical_name) / 60),
                "recorded_by": user_name
            }
        await save_boss_data()
        cd_text = get_boss_cd_text(canonical_name)

        embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
        embed.add_field(name="👾 ชื่อบอส", value=f"`{canonical_name}`", inline=True)
        embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
        embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=cd_text, inline=True)
        embed.add_field(name="👤 ผู้บันทึก", value=f"`{user_name}`", inline=True)
        embed.add_field(name="🔔 บอสจะเกิดเวลา", value=f"**{next_spawn.strftime('%H:%M:%S น.')}**", inline=False)
        embed.set_footer(text=f"บันทึกผ่าน Quick Action โดย {user_name}")

        await interaction.followup.send(embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "กดปุ่มบอสตาย (Quick Action)", f"👾 บอส: `{canonical_name}`\n⏱️ เวลาตาย: {boss_died_at.strftime('%H:%M:%S น.')}\n👤 ผู้บันทึก: `{user_name}`\n🔔 เวลาเกิดถัดไป: {next_spawn.strftime('%H:%M:%S น.')}", discord.Color.red())

class QuickActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        all_bosses = sorted(BOSS_RESPAWN_TIMES.keys())
        self.selected_boss = all_bosses[0] if all_bosses else "Wadangka"
        
        chunks = []
        for i in range(0, min(len(all_bosses), 75), 25): chunks.append(all_bosses[i:i + 25])
        
        start_num = 1
        for index, chunk in enumerate(chunks):
            end_num = start_num + len(chunk) - 1
            placeholder = f"🔻 เลือกบอส (ชุดที่ {index + 1}: ลำดับ {start_num}-{end_num})"
            self.add_item(BossSelect(chunk, placeholder, f"select_boss_quick_{index}", row=index))
            start_num = end_num + 1

    @discord.ui.button(label="⚔️ บอสตายแล้ว", style=discord.ButtonStyle.danger, custom_id="btn_boss_killed_quick", row=3)
    async def boss_killed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_user_permission(interaction.user):
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ใช้งานปุ่มนี้!", ephemeral=True)
            return
        modal = KillBossModal(selected_boss=self.selected_boss)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔔 เรียกคน", style=discord.ButtonStyle.primary, custom_id="btn_call_people_quick", row=3)
    async def call_people_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        allowed_role_names = ["Eternal", "Meaw", "Anti"]
        is_owner = (guild and guild.owner_id == interaction.user.id)
        has_required_role = False

        if member: has_required_role = any(role.name in allowed_role_names for role in member.roles)
        if not (is_owner or has_required_role):
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ใช้งานปุ่มนี้! (อนุญาตเฉพาะเจ้าของ Server และผู้ที่มีโรล Eternal, Meaw, Anti เท่านั้น)", ephemeral=True)
            return

        await interaction.response.defer()
        mentions = []
        if guild:
            for role in guild.roles:
                if role.name in allowed_role_names: mentions.append(role.mention)
        mention_target = " ".join(mentions) if mentions else "@everyone"
        canonical_name = get_boss_canonical_name(self.selected_boss)

        embed = discord.Embed(title="🔔 เรียกสมาชิกคนลุยบอส!", description=f"📢 {interaction.user.mention} เรียกสมาชิกลุยบอส **{canonical_name}** ด่วน!", color=discord.Color.gold())
        await interaction.followup.send(content=mention_target, embed=embed)

        spoken_boss = get_boss_pronunciation(canonical_name)
        spoken_text_th = f"เรียกคนลุยบอส {spoken_boss} ด่วนค่ะ"
        spoken_text_en = f"Calling everyone for boss {canonical_name} immediately."
        spoken_text_ko = f"보스 {canonical_name} 레이드에 당장 참여하세요."
        if guild: asyncio.create_task(speak_in_guild(guild, text_th=spoken_text_th, text_en=spoken_text_en, text_ko=spoken_text_ko))
        await send_audit_log(guild, interaction.user, "กดปุ่มเรียกคน (Quick Action)", f"🔔 เรียกคนลุยบอส: `{canonical_name}`", discord.Color.gold())

@bot.tree.command(name="panel", description="ส่งข้อความ Interactive Embed พร้อมปุ่มกด Quick Actions ในช่องนี้")
@has_allowed_role()
async def send_quick_panel(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(
        title="⚡ Quick Actions - แผงควบคุมเวลาบอส",
        description="เลือกชื่อบอสจากเมนูด้านล่าง แล้วกดปุ่มสั่งการได้ทันที:\n\n"
                    "• **🔻 เมนูเลือกบอส**: เลือกชื่อบอสที่ต้องการ\n"
                    "• **⚔️ บอสตายแล้ว**: กดเพื่อเปิดช่องพิมพ์ระบุเวลาตาย (เช่น `17:30`, `1730` หรือเว้นว่างไว้เพื่อใช้เวลาปัจจุบัน)\n"
                    "• **🔔 เรียกคน**: แท็กยศคนลุยบอส + ส่งเสียง TTS ประกาศตามในห้องเสียงทุกห้องที่มีคนอยู่",
        color=discord.Color.dark_purple()
    )
    embed.set_footer(text="ระบบปุ่มกดอัตโนมัติ 24/7 • Boss Control Panel")
    view = QuickActionsView()
    await interaction.followup.send(embed=embed, view=view)
    embed_summary, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
    if tts_text_th and interaction.guild:
        asyncio.create_task(speak_in_guild(interaction.guild, text_th=tts_text_th, text_en=tts_text_en, text_ko=tts_text_ko))

# ==========================================
# 🔊 9. Voice & Notify Commands
# ==========================================
@bot.tree.command(name="tts", description="ตั้งค่าเปิด-ปิดการแจ้งเตือนเสียง TTS แยกตามภาษา (ไทย, อังกฤษ, เกาหลี)")
@app_commands.describe(lang="เลือกภาษาที่ต้องการตั้งค่า", status="เลือกเปิด (on) หรือปิด (off)")
@app_commands.choices(
    lang=[
        app_commands.Choice(name="🇹🇭 ภาษาไทย (TH)", value="th"),
        app_commands.Choice(name="🇺🇸 ภาษาอังกฤษ (EN)", value="en"),
        app_commands.Choice(name="🇰🇷 ภาษาเกาหลี (KO)", value="ko")
    ],
    status=[
        app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"),
        app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")
    ]
)
@has_allowed_role()
async def toggle_tts_cmd(interaction: discord.Interaction, lang: app_commands.Choice[str], status: app_commands.Choice[str]):
    await interaction.response.defer()
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    is_on = (status.value == "on")
    lang_name = ""
    
    if lang.value == "th":
        tts_th_enabled = is_on
        lang_name = "🇹🇭 ภาษาไทย"
    elif lang.value == "en":
        tts_en_enabled = is_on
        lang_name = "🇺🇸 ภาษาอังกฤษ"
    elif lang.value == "ko":
        tts_ko_enabled = is_on
        lang_name = "🇰🇷 ภาษาเกาหลี"

    await save_bot_settings()
    status_text = "🟢 **เปิด**" if is_on else "🔴 **ปิด**"
    color = discord.Color.green() if is_on else discord.Color.red()
    embed = discord.Embed(
        title="⚙️ ตั้งค่าการแจ้งเตือนด้วยเสียง (TTS)",
        description=f"{status_text} การแจ้งเตือนเสียง {lang_name} เรียบร้อยแล้ว!\n*(ข้อมูลซิงค์กับ Dashboard และ Firebase)*",
        color=color
    )
    await interaction.followup.send(embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่า TTS เสียง (/tts)", f"ภาษา: `{lang.value.upper()}` | สถานะ: `{status.value.upper()}`", color)

@bot.tree.command(name="notify", description="เปิดหรือปิดระบบแจ้งเตือนสงคราม Battlefield (BF)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"), app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")])
@has_allowed_role()
async def toggle_notify(interaction: discord.Interaction, status: app_commands.Choice[str]):
    await interaction.response.defer()
    global bf_notify_enabled
    if status.value == "on":
        bf_notify_enabled = True
        msg = "🟢 **เปิด** ระบบแจ้งเตือน Battlefield (BF) เรียบร้อยแล้ว!"
        color = discord.Color.green()
    else:
        bf_notify_enabled = False
        msg = "🔴 **ปิด** ระบบแจ้งเตือน Battlefield (BF) เรียบร้อยแล้ว!"
        color = discord.Color.red()
    await save_bot_settings()
    embed = discord.Embed(title="⚙️ ตั้งค่าการแจ้งเตือน BF", description=msg, color=color)
    await interaction.followup.send(embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่าการแจ้งเตือน BF (/notify)", f"เปลี่ยนสถานะเป็น: `{status.value.upper()}`", color)

@bot.tree.command(name="ppl", description="เปิดหรือปิดระบบแจ้งเตือนเสียงต้อนรับสมาชิกเข้าห้องเสียง (ทั่วไป)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"), app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")])
@has_allowed_role()
async def toggle_ppl_notify(interaction: discord.Interaction, status: app_commands.Choice[str]):
    await interaction.response.defer()
    global ppl_notify_enabled
    if status.value == "on":
        ppl_notify_enabled = True
        msg = "🟢 **เปิด** ระบบแจ้งเตือนต้อนรับสมาชิกเข้าห้องเสียงเรียบร้อยแล้ว!"
        color = discord.Color.green()
    else:
        ppl_notify_enabled = False
        msg = "🔴 **ปิด** ระบบแจ้งเตือนต้อนรับสมาชิกเข้าห้องเสียงเรียบร้อยแล้ว!"
        color = discord.Color.red()
    await save_bot_settings()
    embed = discord.Embed(title="⚙️ ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้องเสียง", description=msg, color=color)
    await interaction.followup.send(embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)", f"เปลี่ยนสถานะเป็น: `{status.value.upper()}`", color)

@bot.tree.command(name="vip", description="[Admin Only] เปิด/ปิดและตั้งค่าระบบทักทายคนพิเศษ")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) ระบบทักทายคนพิเศษ", user="เลือกสมาชิกคนพิเศษ", message="ข้อความพูดทักทายคนพิเศษ")
@app_commands.choices(status=[app_commands.Choice(name="เปิดระบบทักทายคนพิเศษ (on)", value="on"), app_commands.Choice(name="ปิดระบบทักทายคนพิเศษ (off)", value="off")])
@app_commands.checks.has_permissions(administrator=True)
async def toggle_vip_greet(interaction: discord.Interaction, status: app_commands.Choice[str], user: discord.Member = None, message: str = None):
    await interaction.response.defer()
    global vip_config
    if status.value == "on":
        if not user or not message:
            await interaction.followup.send("❌ **ข้อมูลไม่ครบถ้วน!** กรุณาระบุทั้ง **user** และ **message**", ephemeral=True)
            return
        vip_config = {"enabled": True, "user_id": user.id, "user_name": user.display_name, "message": message}
        await save_vip_config()
        embed = discord.Embed(title="🌟 เปิดใช้งานระบบทักทายคนพิเศษ (VIP)", description=f"🟢 **สถานะ:** เปิดใช้งาน\n👤 **คนพิเศษ:** {user.mention}\n💬 **คำทักทาย:** \"{message}\"", color=discord.Color.gold())
        await interaction.followup.send(embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "เปิดระบบทักทายคนพิเศษ (/vip)", f"👤 คนพิเศษ: `{user.display_name}`\n💬 ข้อความ: {message}", discord.Color.gold())
    else:
        vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
        await save_vip_config()
        embed = discord.Embed(title="⚙️ ปิดระบบทักทายคนพิเศษ (VIP)", description="🔴 **สถานะ:** ปิดใช้งานเรียบร้อยแล้ว", color=discord.Color.red())
        await interaction.followup.send(embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "ปิดระบบทักทายคนพิเศษ (/vip)", "ยกเลิกข้อมูลคนพิเศษเรียบร้อยแล้ว", discord.Color.red())

@bot.tree.command(name="setvoice", description="ตั้งค่าห้อง Voice สำหรับ Boss TTS แบบถาวร")
@app_commands.describe(channel="ห้อง Voice ที่ต้องการให้บอทเข้าและประกาศ (เว้นว่าง = ห้องที่คุณอยู่)")
@has_allowed_role()
async def set_voice(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    # Acknowledge immediately. Never wait for Firebase/Discord Voice before responding.
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"❌ /setvoice defer failed: {e}")
        return

    try:
        target = channel
        if target is None:
            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.followup.send(
                    "❌ กรุณาเข้าห้อง Voice ก่อน หรือเลือกห้อง Voice ในคำสั่ง /setvoice",
                    ephemeral=True
                )
                return
            target = interaction.user.voice.channel

        guild_id = interaction.guild.id
        voice_config[str(guild_id)] = {
            "guild_id": guild_id,
            "voice_channel_id": int(target.id),
            "channel_name": target.name,
            "enabled": True,
            "updated_by": str(interaction.user.id),
            "updated_at": datetime.now(TZ_THAI).isoformat()
        }

        # Respond first so Discord never reports "The application did not respond".
        await interaction.followup.send(
            f"🔊 บันทึกห้อง Voice **{target.name}** แล้ว\n"
            f"ID: `{target.id}`\n"
            f"กำลังเชื่อมต่อ Voice และบันทึก Firebase...",
            ephemeral=True
        )

        async def finish_voice_setup():
            try:
                await asyncio.wait_for(save_voice_config(), timeout=8)
            except Exception as e:
                print(f"❌ /setvoice Firebase save failed: {e}")

            try:
                vc = await asyncio.wait_for(
                    ensure_configured_voice(interaction.guild),
                    timeout=25
                )
                status = "🟢 เชื่อมต่อ Voice สำเร็จ" if vc and vc.is_connected() else "🟡 บันทึกค่าแล้ว แต่เชื่อมต่อ Voice ไม่สำเร็จ"
                print(f"🔊 /setvoice {interaction.guild.name}: {status}")
            except Exception as e:
                print(f"❌ /setvoice voice connection failed: {e}")

            try:
                await send_audit_log(
                    interaction.guild,
                    interaction.user,
                    "ตั้งค่าห้อง Voice (/setvoice)",
                    f"🔊 ห้อง: {target.mention}\nID: `{target.id}`",
                    discord.Color.green()
                )
            except Exception as e:
                print(f"⚠️ /setvoice audit log failed: {e}")

        asyncio.create_task(finish_voice_setup())

    except Exception as e:
        print(f"❌ /setvoice unexpected error: {e}")
        traceback.print_exc()
        try:
            await interaction.followup.send(f"❌ /setvoice เกิดข้อผิดพลาด: `{e}`", ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="join", description="ดึงบอทเข้าห้องเสียงที่คุณกำลังใช้งาน")
async def join_voice(interaction: discord.Interaction):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ คุณต้องเชื่อมต่ออยู่ในห้องเสียงก่อนใช้คำสั่งนี้!", ephemeral=True)
        return
    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    if guild.voice_client is not None: await guild.voice_client.move_to(voice_channel)
    else: await voice_channel.connect()
    embed = discord.Embed(title="🔊 เชื่อมต่อห้องเสียงสำเร็จ", description=f"บอทเข้าสู่ห้องเสียง **{voice_channel.name}** เรียบร้อยแล้ว!", color=discord.Color.green())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="leave", description="สั่งให้บอทออกจากห้องเสียง")
async def leave_voice(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    if guild.voice_client:
        await guild.voice_client.disconnect()
    if str(guild.id) in voice_config:
        voice_config[str(guild.id)]["enabled"] = False
        await save_voice_config()
        await interaction.followup.send("👋 ออกจากห้องเสียงแล้ว และปิดการเชื่อมต่อถาวรของ /setvoice ชั่วคราวแล้วครับ")
    else:
        await interaction.followup.send("❌ บอทไม่ได้อยู่ในห้องเสียงใดๆ ในขณะนี้", ephemeral=True)

@bot.tree.command(name="disconnect", description="ตัดการเชื่อมต่อเสียงและหยุดการเล่นเสียงของบอททันที")
async def disconnect_voice(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            if vc.is_playing(): vc.stop()
            await vc.disconnect()
            await interaction.followup.send("⏹️ บอทหยุดการทำงานและออกจากห้องเสียงเรียบร้อยแล้ว!")
        else:
            await interaction.followup.send("❌ บอทไม่ได้อยู่ในห้องเสียงครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ เกิดข้อผิดพลาด: `{e}`")

@bot.tree.command(name="notice", description="ประกาศข้อความเสียงไปยังทุกห้องสนทนาที่มีคนอยู่")
@app_commands.describe(message="ข้อความที่ต้องการให้บอทประกาศ")
@has_allowed_role()
async def notice_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    if not message.strip():
        await interaction.followup.send("❌ กรุณาระบุข้อความที่ต้องการประกาศครับ", ephemeral=True)
        return
    embed = discord.Embed(title="📢 ประกาศข้อความเสียง (Global Notice)", description=f"**ข้อความ:** {message}\n\nกำลังไล่ประกาศในทุกห้องเสียงที่มีสมาชิกอยู่...", color=discord.Color.blue())
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)
    asyncio.create_task(speak_in_guild(interaction.guild, text_th=message))

# ==========================================
# ⚔️ 10. Boss Slash Commands
# ==========================================
def generate_boss_time_summary():
    now = datetime.now(TZ_THAI)
    with schedule_lock: schedule_copy = boss_schedule.copy()
    if not schedule_copy: return None, "ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบครับ", None, None

    sorted_bosses = sorted(schedule_copy.items(), key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now)
    embed = discord.Embed(title="⌛ สรุปเวลาที่เหลือของบอสทุกตัว (เรียงจากน้อยไปมาก)", description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.purple())
    
    tts_lines_th = ["สรุปเวลาบอสเรียงจากน้อยไปมากค่ะ"]
    tts_lines_en = ["Boss time summary from earliest to latest."]
    tts_lines_ko = ["보스 스폰 시간 요약입니다."]

    display_bosses = sorted_bosses[:20]
    for boss, data in display_bosses:
        spawn_time = parse_to_thai_datetime(data["spawn_time"])
        if not spawn_time: continue
        time_left_sec = (spawn_time - now).total_seconds()
        spoken_name = get_boss_pronunciation(boss)
        rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"

        if time_left_sec <= 0:
            time_left_str = "เกิดแล้ว!"
            tts_lines_th.append(f"บอส {spoken_name} เกิดแล้วค่ะ")
            tts_lines_en.append(f"Boss {boss} has spawned.")
            tts_lines_ko.append(f"보스 {boss}가 나타났습니다.")
        else:
            m, s = divmod(int(time_left_sec), 60)
            h, m = divmod(m, 60)
            parts = []
            if h > 0: parts.append(f"{h} ชม.")
            if m > 0 or h > 0: parts.append(f"{m} นาที")
            parts.append(f"{s} วินาที")
            time_left_str = f"อีก {' '.join(parts)}"

            if h > 0: 
                tts_time_th = f"{h} ชั่วโมง {m} นาที"
                tts_time_en = f"{h} hours and {m} minutes"
                tts_time_ko = f"{h}시간 {m}분" if m > 0 else f"{h}시간"
            elif m > 0: 
                tts_time_th = f"{m} นาที"
                tts_time_en = f"{m} minutes"
                tts_time_ko = f"{m}분"
            else: 
                tts_time_th = f"{s} วินาที"
                tts_time_en = f"{s} seconds"
                tts_time_ko = f"{s}초"
                
            tts_lines_th.append(f"บอส {spoken_name} เหลืออีก {tts_time_th}")
            tts_lines_en.append(f"Boss {boss} in {tts_time_en}.")
            tts_lines_ko.append(f"보스 {boss}가 {tts_time_ko} 남았습니다.")

        embed.add_field(name=f"👾 {boss}", value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**\n*(บันทึกโดย: {rec_by})*", inline=False)

    if len(sorted_bosses) > 20:
        embed.add_field(name="📌 หมายเหตุ", value=f"*ยังมีบอสอีก {len(sorted_bosses) - 20} ตัว สามารถดูเพิ่มเติมได้บน Dashboard*", inline=False)
    
    tts_text_th = " ".join(tts_lines_th)
    tts_text_en = " ".join(tts_lines_en)
    tts_text_ko = " ".join(tts_lines_ko)
    return embed, tts_text_th, tts_text_en, tts_text_ko

@bot.tree.command(name="time", description="คำนวณเวลาที่เหลือของบอสทุกตัว เรียงจากน้อยไปมาก และส่งเสียงอ่าน TTS ในห้องเสียง")
async def boss_time_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
    if embed is None:
        await interaction.followup.send(tts_text_th)
        return
    await interaction.followup.send(embed=embed)
    asyncio.create_task(speak_in_guild(interaction.guild, text_th=tts_text_th, text_en=tts_text_en, text_ko=tts_text_ko))
    await send_audit_log(interaction.guild, interaction.user, "เช็กเวลาบอสพร้อม TTS (/time)", "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย", discord.Color.purple())

@bot.command(name="time")
async def boss_time_prefix(ctx: commands.Context):
    embed, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
    if embed is None:
        await ctx.send(tts_text_th)
        return
    await ctx.send(embed=embed)
    asyncio.create_task(speak_in_guild(ctx.guild, text_th=tts_text_th, text_en=tts_text_en, text_ko=tts_text_ko))
    await send_audit_log(ctx.guild, ctx.author, "เช็กเวลาบอสพร้อม TTS (!time)", "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย", discord.Color.purple())

@bot.tree.command(name="kill", description="บันทึกเวลาที่บอสตายเพื่อเริ่มคำนวณเวลานับถอยหลัง")
@app_commands.describe(
    boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการบันทึกเวลา",
    kill_time="ระบุเวลาที่บอสตาย (เช่น 17:30 หรือ 1730) ถ้าไม่ระบุจะใช้เวลาปัจจุบัน"
)
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str = None):
    # Acknowledge immediately.
    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"❌ /kill defer failed: {e}")
        return

    try:
        canonical_name = get_boss_canonical_name(boss_name)
        now = datetime.now(TZ_THAI)

        try:
            boss_died_at = parse_time_input(kill_time, now)
        except ValueError:
            await interaction.followup.send(
                "❌ รูปแบบเวลาไม่ถูกต้อง! กรุณากรอกแบบ **17:30** หรือ **1730**",
                ephemeral=True
            )
            return

        respawn_time = get_boss_respawn_time(canonical_name)
        next_spawn = boss_died_at + respawn_time
        is_already_past = next_spawn <= now
        user_name = interaction.user.display_name

        record = {
            "spawn_time": next_spawn.isoformat(),
            "killTimeMs": int(boss_died_at.timestamp() * 1000),
            "channelId": interaction.channel_id,
            "notifiedNotice": is_already_past,
            "notifiedSpawn": is_already_past,
            "noticeMinutes": int(get_boss_advance_notice_seconds(canonical_name) / 60),
            "recordedBy": user_name,
            "spawnTimeMs": int(next_spawn.timestamp() * 1000)
        }

        with schedule_lock:
            boss_schedule[canonical_name] = {
                "spawn_time": next_spawn,
                "killTimeMs": record["killTimeMs"],
                "channel_id": interaction.channel_id,
                "notified_advance": is_already_past,
                "notified_spawn": is_already_past,
                "noticeMinutes": record["noticeMinutes"],
                "recorded_by": user_name
            }

        cd_text = get_boss_cd_text(canonical_name)

        embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
        embed.add_field(name="👾 ชื่อบอส", value=f"`{canonical_name}`", inline=True)
        embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
        embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=cd_text, inline=True)
        embed.add_field(name="👤 ผู้บันทึก", value=f"`{user_name}`", inline=True)
        embed.add_field(name="🔔 บอสจะเกิดเวลา", value=f"**{next_spawn.strftime('%H:%M:%S น.')}**", inline=False)
        embed.set_footer(text=f"บันทึกโดย {user_name}")

        # Send Discord response before any Firebase/local I/O.
        await interaction.followup.send(embed=embed)

        async def persist_kill():
            try:
                saved_to_firebase = await asyncio.wait_for(save_boss_data(), timeout=15)
                if saved_to_firebase:
                    print(f"💾 /kill Firebase saved: {canonical_name} -> {next_spawn.isoformat()}")
                else:
                    print(f"⚠️ /kill saved only to local fallback: {canonical_name} -> {next_spawn.isoformat()}")
            except Exception as e:
                print(f"❌ /kill save failed: {e}")
                traceback.print_exc()

            try:
                await send_audit_log(
                    interaction.guild,
                    interaction.user,
                    "บันทึกเวลาบอสตาย (/kill)",
                    f"👾 บอส: `{canonical_name}`\n"
                    f"👤 ผู้บันทึก: `{user_name}`\n"
                    f"🔔 เวลาเกิดถัดไป: {next_spawn.strftime('%H:%M:%S น.')}",
                    discord.Color.red()
                )
            except Exception as e:
                print(f"⚠️ /kill audit log failed: {e}")

        asyncio.create_task(persist_kill())

    except Exception as e:
        print(f"❌ /kill unexpected error: {e}")
        traceback.print_exc()
        try:
            await interaction.followup.send(f"❌ /kill เกิดข้อผิดพลาด: `{e}`", ephemeral=True)
        except Exception:
            pass

add_group = app_commands.Group(name="add", description="คำสั่งจัดการข้อมูลบอส")
bot.tree.add_command(add_group)

@add_group.command(name="boss", description="เพิ่มบอสใหม่เข้าไปในระบบ (ไม่สร้าง Timer)")
@app_commands.describe(
    name="ชื่อบอสใหม่",
    hours="คูลดาวน์ชั่วโมง (ใช้กำหนดค่าให้ /kill; ไม่สร้าง Timer)",
    minutes="คูลดาวน์นาที",
    seconds="คูลดาวน์วินาที",
    notice_minutes="แจ้งเตือนล่วงหน้ากี่นาที"
)
@has_allowed_role()
async def add_boss(interaction: discord.Interaction, name: str, hours: int = 0, minutes: int = 30, seconds: int = 0, notice_minutes: int = 5):
    await interaction.response.defer()
    name = (name or "").strip()
    if not name:
        await interaction.followup.send("❌ กรุณาระบุชื่อบอส", ephemeral=True)
        return
    if any(c in name for c in "/\\.#$[]"):
        await interaction.followup.send("❌ ชื่อบอสมีอักขระที่ Firebase ไม่อนุญาต (/ . # $ [ ])", ephemeral=True)
        return
    total_seconds = hours * 3600 + minutes * 60 + seconds
    if total_seconds <= 0 or notice_minutes < 1:
        await interaction.followup.send("❌ CD ต้องมากกว่า 0 วินาที และ notice ต้องอย่างน้อย 1 นาที", ephemeral=True)
        return
    canonical = get_boss_canonical_name(name)
    if canonical in BOSS_RESPAWN_TIMES and canonical in DEFAULT_BOSS_NAMES:
        await interaction.followup.send(f"⚠️ บอส **{canonical}** มีอยู่ในระบบแล้ว — /addboss ใช้เพิ่มชื่อบอสใหม่เท่านั้น ไม่สร้าง Timer", ephemeral=True)
        return
    if "wadangka" in canonical.lower() or "วาดังการ์" in canonical:
        notice_minutes = 30
    BOSS_RESPAWN_TIMES[canonical] = timedelta(seconds=total_seconds)
    BOSS_CD_TEXT[canonical] = (f"{hours} ชั่วโมง " if hours else "") + (f"{minutes} นาที " if minutes else "") + (f"{seconds} วินาที" if seconds else "")
    BOSS_CD_TEXT[canonical] = BOSS_CD_TEXT[canonical].strip() or "0 วินาที"
    ADVANCE_NOTICE_SECONDS[canonical] = notice_minutes * 60
    ADVANCE_NOTICE_TEXT[canonical] = f"{notice_minutes} นาที"
    BOSS_PRONUNCIATION.setdefault(canonical, canonical)
    await save_custom_bosses_to_github()
    # IMPORTANT: /addboss never writes boss_schedule.
    await interaction.followup.send(
        f"✅ เพิ่มบอส **{canonical}** เข้า Boss Definition สำเร็จ\n"
        f"⏳ CD สำหรับ /kill: **{BOSS_CD_TEXT[canonical]}**\n"
        f"🔔 แจ้งเตือนล่วงหน้า: **{notice_minutes} นาที**\n"
        f"📌 ยังไม่ได้สร้าง Timer — ใช้ `/kill {canonical}` เมื่อบอสตาย",
        ephemeral=False
    )
    await send_audit_log(interaction.guild, interaction.user, "เพิ่มบอส (/addboss)", f"➕ `{canonical}` | CD {BOSS_CD_TEXT[canonical]} | ไม่มีการสร้าง boss_schedule", discord.Color.green())

@bot.tree.command(name="delboss", description="ลบบอสออกจากตารางนับถอยหลัง")
@app_commands.describe(boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def del_boss(interaction: discord.Interaction, boss_name: str):
    await interaction.response.defer()
    matched_key = None
    with schedule_lock:
        for k in list(boss_schedule.keys()):
            if k.lower() == boss_name.lower():
                matched_key = k
                break
        if matched_key: del boss_schedule[matched_key]

    if matched_key:
        try: await asyncio.to_thread(db.reference(f'boss_schedule/{matched_key}').delete)
        except Exception: pass
        await save_boss_data()
        
        embed = discord.Embed(title="🗑️ ลบบอสสำเร็จ", description=f"ทำการลบข้อมูลเวลาของบอส **{matched_key}** ออกจากระบบเรียบร้อยแล้ว", color=discord.Color.orange())
        await interaction.followup.send(embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "ลบบอส (/delboss)", f"🗑️ ลบบอส: `{matched_key}`", discord.Color.orange())
    else:
        await interaction.followup.send(f"❌ ไม่พบบอส **{boss_name}** ในตารางนับถอยหลังขณะนี้", ephemeral=True)

@bot.tree.command(name="status", description="เช็กสถานะเวลาบอสทั้งหมดที่กำลังนับถอยหลัง")
async def boss_status(interaction: discord.Interaction):
    await interaction.response.defer()
    with schedule_lock: schedule_copy = boss_schedule.copy()
    if not schedule_copy:
        embed = discord.Embed(title="📜 ตารางเวลาบอส", description="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ\nใช้คำสั่ง `/kill [ชื่อบอส]` เพื่อเริ่มบันทึกเวลาได้เลยครับ", color=discord.Color.blue())
        await interaction.followup.send(embed=embed)
        return

    now = datetime.now(TZ_THAI)
    embed = discord.Embed(title="📜 ตารางเวลาบอสเกิดทั้งหมด", description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.blue())
    sorted_bosses = sorted(schedule_copy.items(), key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now)
    
    display_bosses = sorted_bosses[:20]
    for boss, data in display_bosses:
        spawn_time = parse_to_thai_datetime(data["spawn_time"])
        if not spawn_time: continue
        time_left_sec = (spawn_time - now).total_seconds()
        
        if time_left_sec <= 0: time_left_str = "เกิดแล้ว!"
        else:
            m, s = divmod(int(time_left_sec), 60)
            h, m = divmod(m, 60)
            if h > 0: time_left_str = f"อีก {h} ชม. {m} นาที"
            else: time_left_str = f"อีก {m} นาที {s} วินาที"

        notice_text = get_boss_advance_notice_text(boss)
        rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"
        embed.add_field(name=f"👾 {boss}", value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**\n*(ผู้บันทึก: {rec_by} | เตือนล่วงหน้า {notice_text})*", inline=False)

    if len(sorted_bosses) > 20:
        embed.add_field(name="📌 หมายเหตุ", value=f"*และยังมีบอสอีก {len(sorted_bosses) - 20} ตัวในคิว*", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="setlive", description="ตั้งค่าป้ายไฟนับถอยหลังเวลาบอสเกิด Real-time ในช่องนี้")
@has_allowed_role()
async def set_live(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.now(TZ_THAI)
    embed = discord.Embed(title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time", description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.teal())
    embed.add_field(name="📌 สถานะ", value="กำลังเริ่มต้นระบบ...", inline=False)
    embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")

    msg = await interaction.followup.send(embed=embed)
    global live_message_config, cached_live_message
    live_message_config = {"channel_id": interaction.channel_id, "message_id": msg.id}
    cached_live_message = msg
    await save_live_config()
    await send_audit_log(interaction.guild, interaction.user, "สร้าง Live Embed (/setlive)", f"📌 ช่อง: <#{interaction.channel_id}>\nMessage ID: `{msg.id}`", discord.Color.teal())

@bot.tree.command(name="attendance", description="แจ้งเตือนเช็คชื่อบอสพร้อมโค้ดและไอเทมดรอป")
@app_commands.describe(
    boss_name="ชื่อบอสที่ต้องการเช็คชื่อ",
    code="โค้ดสำหรับเช็คชื่อ (Code)",
    drop_item="ไอเทมที่ดรอป (Drop Item)"
)
@has_allowed_role()
async def attendance_command(interaction: discord.Interaction, boss_name: str, code: str, drop_item: str):
    await interaction.response.defer()
    
    embed = discord.Embed(
        title="📢 แจ้งเตือนเช็คชื่อบอส (Attendance)",
        color=discord.Color.green(),
        timestamp=datetime.now(TZ_THAI)
    )
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="🔑 โค้ด (Code)", value=f"**{code}**", inline=True)
    embed.add_field(name="🎁 ไอเทมดรอป", value=f"`{drop_item}`", inline=False)
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}")
    
    await interaction.followup.send(content="✅ ส่งประกาศเช็คชื่อสำเร็จ!", embed=embed)
    
    canonical_name = get_boss_canonical_name(boss_name)
    spoken_name = get_boss_pronunciation(canonical_name)
    
    spoken_th = f"ประกาศเช็คชื่อบอส {spoken_name} โค้ดคือ {code} ไอเทมที่ดรอปคือ {drop_item} ค่ะ"
    spoken_en = f"Attendance for boss {boss_name}. The code is {code}. Drop item is {drop_item}."
    spoken_ko = f"보스 {boss_name} 출석 체크입니다. 코드는 {code} 이며, 드롭 아이템은 {drop_item} 입니다."
    
    asyncio.create_task(speak_in_guild(interaction.guild, text_th=spoken_th, text_en=spoken_en, text_ko=spoken_ko))
    
    if interaction.guild:
        attendance_channel = discord.utils.get(interaction.guild.text_channels, name="boss-attendance")
        if attendance_channel:
            log_embed = discord.Embed(
                title="📝 Audit Log: ประกาศเช็คชื่อบอส",
                color=discord.Color.green(),
                timestamp=datetime.now(TZ_THAI)
            )
            log_embed.add_field(name="👤 ผู้ประกาศ", value=f"{interaction.user.mention} (`{interaction.user.name}`)", inline=False)
            log_embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
            log_embed.add_field(name="🔑 โค้ด (Code)", value=f"**{code}**", inline=True)
            log_embed.add_field(name="🎁 ไอเทมดรอป", value=f"`{drop_item}`", inline=False)
            log_embed.set_footer(text=f"User ID: {interaction.user.id}")
            try:
                await attendance_channel.send(embed=log_embed)
            except Exception as e:
                print(f"❌ ส่ง Audit Log ใน boss-attendance ไม่สำเร็จ: {e}")

# ==========================================
# 🚀 11. Run Bot Entry Point
# ==========================================
async def run_bot_with_backoff(token: str):
    """Run Discord with bounded retry and explicitly close failed HTTP sessions."""
    backoff = 10
    while True:
        try:
            print("🔌 กำลังเชื่อมต่อ Discord Gateway...")
            await bot.start(token, reconnect=True)
            print("🛑 Discord bot stopped normally.")
            return
        except discord.HTTPException as e:
            status = getattr(e, "status", None)
            if status == 429:
                retry_after = getattr(e, "retry_after", 0)
                try: retry_after = float(retry_after or 0)
                except (TypeError, ValueError): retry_after = 0
                delay = max(retry_after, backoff)
                print(f"🛑 Discord 429 — รอ {delay:.0f} วินาทีก่อนเชื่อมต่อใหม่")
            else:
                delay = backoff
                print(f"❌ Discord HTTP error status={status}: {e}")
            try:
                await bot.close()
            except Exception as close_error:
                print(f"⚠️ ปิด Discord HTTP session หลัง error ไม่สำเร็จ: {close_error}")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 300)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"⚠️ Discord connection error: {e} — ปิด session แล้ว retry ใน {backoff} วินาที")
            try:
                await bot.close()
            except Exception as close_error:
                print(f"⚠️ ปิด Discord HTTP session ไม่สำเร็จ: {close_error}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
        except RuntimeError as e:
            if "Session is closed" in str(e):
                print("⚠️ Discord session ถูกปิด — จะสร้าง session ใหม่ในรอบถัดไป")
                try: await bot.close()
                except Exception: pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            raise
        except Exception as e:
            print(f"❌ Discord startup error: {e}")
            traceback.print_exc()
            try: await bot.close()
            except Exception: pass
            raise

if __name__ == "__main__":
    # Render needs the HTTP listener immediately; start it exactly once.
    keep_alive()
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        try:
            asyncio.run(run_bot_with_backoff(TOKEN))
        except KeyboardInterrupt:
            print("🛑 หยุดบอทแล้ว")
        finally:
            # Only close the Discord aiohttp session when the process is really shutting down.
            try:
                if not bot.is_closed():
                    asyncio.run(bot.close())
            except Exception as e:
                print(f"⚠️ ปิด Discord client ไม่สำเร็จ: {e}")
    else:
        print("⚠️ กรุณาตั้งค่า DISCORD_TOKEN ใน Environment Variable หรือระบุ Token สำหรับรันบอท")
