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
from flask import Flask, render_template_string
from waitress import serve
import edge_tts
import imageio_ffmpeg

from keep_alive import keep_alive

# เรียกใช้ Web server จำลองก่อนเริ่มการทำงานของ Bot
keep_alive()

# 🔥 Firebase Admin SDK Setup
import firebase_admin
from firebase_admin import credentials, db

# ==========================================
# 🔥 0. เชื่อมต่อ Firebase Realtime Database
# ==========================================
FIREBASE_KEY_PATH = "firebase-key.json"
DATABASE_URL = "https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app"

if not firebase_admin._apps:
    if os.path.exists(FIREBASE_KEY_PATH):
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred, {
            'databaseURL': DATABASE_URL
        })
        print("✅ เชื่อมต่อ Firebase Realtime Database สำเร็จ!")
    else:
        print(f"⚠️ ไม่พบไฟล์ {FIREBASE_KEY_PATH} ในโฟลเดอร์บอท! กรุณาตรวจสอบไฟล์ Service Account Key")

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
DB_FILE = "bot_database.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        conn.close()
        print("✅ บันทึก/เชื่อมต่อ Database (SQLite) สำเร็จ")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการตั้งค่า Database: {e}")

def set_db_value(key: str, value):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        val_str = json.dumps(value, ensure_ascii=False)
        cursor.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val_str)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ บันทึกข้อมูลลง Database ไม่สำเร็จ ({key}): {e}")

def get_db_value(key: str, default=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        print(f"❌ ดึงข้อมูลจาก Database ไม่สำเร็จ ({key}): {e}")
    return default

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
    </style>
</head>
<body>
    <div class="container py-5">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h2>⚔️ Boss Timer Dashboard</h2>
            <span class="badge bg-success status-badge">🟢 Bot Online</span>
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

    return render_template_string(HTML_TEMPLATE, bosses=boss_list)

def run_web():
    port = int(os.environ.get("PORT", 5000))
    serve(app, host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# ==========================================
# ⚙️ Config & Global Variables
# ==========================================
DATA_FILE = "boss_data.json"
CUSTOM_BOSSES_FILE = "custom_bosses.json"
LIVE_CONFIG_FILE = "live_config.json"
VIP_CONFIG_FILE = "vip_config.json"
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
disconnect_tasks = {}

# 🔥 Settings variables
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
    "Wadangka": "2 ชั่วโมง 30 นาที", "Elemental Queen": "2 ชั่วโมง 30 นาที", "Tank": "58 นาที 20 วินาที",
    "Swirl Flame": "58 นาที 20 วินาที", "Maelstrom": "58 นาที 20 วินาที", "Twister": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง", "Chief Magief": "30 นาที", "Faith": "5 ชั่วโมง 53 นาที",
    "Apapa": "15 นาที", "Corrupt Forest Keeper": "58 นาที", "Recluse": "11 ชั่วโมง 23 นาที",
    "Blackskull": "56 นาที 50 วินาที", "Sleepy Kooii": "20 นาที", "Awaken Kooii": "1 ชั่วโมง 3 นาที",
    "Eeheehee": "1 ชั่วโมง 6 นาที 48 วินาที", "Ooheeheek": "1 ชั่วโมง 8 นาที 3 วินาที", "Oohehe": "1 ชั่วโมง 5 นาที 8 วินาที",
    "Guardian Imp": "1 ชั่วโมง 3 นาที", "Devilang": "5 ชั่วโมง 33 นาที", "Blackjuno": "35 นาที",
    "Blacksky": "35 นาที", "Red Fox": "20 นาที", "7tailfox": "20 นาที", "777Tailfox": "30 นาที",
    "Sunrise Flower": "20 นาที", "Magma Senior Thief": "20 นาที", "Bbinikjoe": "20 นาที",
    "Bigmouse": "20 นาที", "Caligo": "7 วัน", "Poison Root Flower": "28 นาที 10 วินาที",
    "Contaminated Queen Bee": "28 นาที", "Rotten Pudding": "30 นาที", "Swamp Flower Monster": "30 นาที",
    "Ukpana": "48 ชั่วโมง", "Darlene the Witch": "72 ชั่วโมง", "Illust": "72 ชั่วโมง",
    "Actaemon": "6 ชั่วโมง", "Aiyo's Protector": "72 ชั่วโมง", "Glucose": "30 นาที",
    "Overload": "29 นาที 52 วินาที", "Soul Lich": "24 ชั่วโมง 15 นาที", "Platanista": "168 ชั่วโมง (7 วัน)",
    "Barslaf": "48 ชั่วโมง", "Billiard": "7 ชั่วโมง 55 นาที 3 วินาที", "Shaaack": "30 นาที",
    "Suuuk": "20 นาที", "Sususuk": "20 นาที", "sandgrave": "20 นาที", "Elder Beholder": "20 นาที"
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

async def save_boss_data():
    global is_updating_from_bot
    with schedule_lock:
        data_to_save = {}
        for boss_name, data in boss_schedule.items():
            st = parse_to_thai_datetime(data["spawn_time"])
            if not st: continue
            spawn_ms = int(st.timestamp() * 1000)
            rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"
            data_to_save[boss_name] = {
                "spawn_time": st.isoformat(),
                "spawnTimeMs": spawn_ms,
                "channel_id": data.get("channel_id"),
                "notified_advance": parse_bool(data.get("notified_advance", False)),
                "notified_spawn": parse_bool(data.get("notified_spawn", False)),
                "noticeMinutes": int(get_boss_advance_notice_seconds(boss_name) / 60),
                "recorded_by": rec_by,
                "recordedBy": rec_by
            }
    
    try:
        is_updating_from_bot = True
        ref_boss = db.reference('boss_schedule')
        await asyncio.to_thread(ref_boss.set, data_to_save)
    except Exception as e:
        print(f"❌ บันทึกตารางบอสลง Firebase ไม่สำเร็จ: {e}")
    finally:
        is_updating_from_bot = False

    await asyncio.to_thread(set_db_value, "boss_schedule", data_to_save)
    await asyncio.to_thread(save_json_local, DATA_FILE, data_to_save)

async def load_boss_data():
    global boss_schedule
    saved_data = None
    try:
        ref_boss = db.reference('boss_schedule')
        saved_data = await asyncio.to_thread(ref_boss.get)
    except Exception as e:
        print(f"⚠️ ดึงข้อมูลบอสจาก Firebase ไม่สำเร็จ: {e}")

    if not saved_data:
        saved_data = get_db_value("boss_schedule", None)

    if saved_data and isinstance(saved_data, dict):
        with schedule_lock:
            boss_schedule.clear()
            for boss_name, data in saved_data.items():
                if isinstance(data, dict):
                    raw_st = data.get("spawnTimeMs") or data.get("spawn_time")
                    st = parse_to_thai_datetime(raw_st)
                    if st:
                        canonical_name = get_boss_canonical_name(boss_name)
                        notified_adv = parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
                        notified_spwn = parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))
                        rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"
                        
                        boss_schedule[canonical_name] = {
                            "spawn_time": st,
                            "channel_id": data.get("channel_id"),
                            "notified_advance": notified_adv,
                            "notified_spawn": notified_spwn,
                            "recorded_by": rec_by
                        }
        print(f"✅ โหลดตารางบอสจาก Firebase สำเร็จ {len(boss_schedule)} รายการ")

def start_firebase_listener(loop):
    def listener(event):
        global is_updating_from_bot
        if not is_bot_ready or is_updating_from_bot: return

        try:
            ref_boss = db.reference('boss_schedule')
            snapshot = ref_boss.get()
            if snapshot and isinstance(snapshot, dict):
                with schedule_lock:
                    for boss_name, data in snapshot.items():
                        if not isinstance(data, dict): continue
                        
                        canonical_name = get_boss_canonical_name(boss_name)
                        raw_st = data.get("spawnTimeMs") or data.get("spawn_time")
                        st = parse_to_thai_datetime(raw_st)
                        if not st: continue

                        raw_kt = data.get("kill_time") or data.get("die_time") or data.get("killed_at")
                        if raw_kt:
                            kt = parse_to_thai_datetime(raw_kt)
                            if kt and abs((st - kt).total_seconds()) < 5:
                                cd = get_boss_respawn_time(canonical_name)
                                st = kt + cd

                        existing = boss_schedule.get(canonical_name) or boss_schedule.get(boss_name)
                        rec_by = data.get("recorded_by") or data.get("recordedBy") or (existing.get("recorded_by") if existing else "-")
                        
                        notified_adv = parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
                        notified_spwn = parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))

                        if existing and existing.get("spawn_time") == st:
                            notified_adv = existing.get("notified_advance", notified_adv)
                            notified_spwn = existing.get("notified_spawn", notified_spwn)

                        boss_schedule[canonical_name] = {
                            "spawn_time": st,
                            "channel_id": data.get("channel_id") or (existing.get("channel_id") if existing else None),
                            "notified_advance": notified_adv,
                            "notified_spawn": notified_spwn,
                            "recorded_by": rec_by
                        }
                    
                    for key in list(boss_schedule.keys()):
                        if key not in snapshot and get_boss_canonical_name(key) not in [get_boss_canonical_name(k) for k in snapshot.keys()]:
                            del boss_schedule[key]
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดใน Firebase Listener: {e}")

    try:
        ref_boss = db.reference('boss_schedule')
        ref_boss.listen(listener)
        print("🟢 Firebase Listener พร้อมทำงานเรียบร้อย!")
    except Exception as e:
        print(f"❌ ไม่สามารถเปิด Firebase Listener ได้: {e}")

# 🔥 เพิ่ม Listener สำหรับอ่านค่า Bot Settings แบบ Realtime จากเว็บ
def start_firebase_settings_listener(loop):
    def listener(event):
        global bf_notify_enabled, lib_notify_enabled, ppl_notify_enabled
        global tts_th_enabled, tts_en_enabled, tts_ko_enabled
        try:
            ref_settings = db.reference('bot_settings')
            snapshot = ref_settings.get()
            if snapshot and isinstance(snapshot, dict):
                bf_notify_enabled = snapshot.get("bf_notify_enabled", bf_notify_enabled)
                lib_notify_enabled = snapshot.get("lib_notify_enabled", lib_notify_enabled)
                ppl_notify_enabled = snapshot.get("ppl_notify_enabled", ppl_notify_enabled)
                tts_th_enabled = snapshot.get("tts_th_enabled", tts_th_enabled)
                tts_en_enabled = snapshot.get("tts_en_enabled", tts_en_enabled)
                tts_ko_enabled = snapshot.get("tts_ko_enabled", tts_ko_enabled)
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดใน Firebase Settings Listener: {e}")

    try:
        ref_settings = db.reference('bot_settings')
        ref_settings.listen(listener)
        print("🟢 Firebase Settings Listener พร้อมทำงานเรียบร้อย!")
    except Exception as e:
        print(f"❌ ไม่สามารถเปิด Firebase Settings Listener ได้: {e}")

async def save_bot_settings():
    settings_data = {
        "bf_notify_enabled": bf_notify_enabled,
        "lib_notify_enabled": lib_notify_enabled,
        "ppl_notify_enabled": ppl_notify_enabled,
        "tts_th_enabled": tts_th_enabled,
        "tts_en_enabled": tts_en_enabled,
        "tts_ko_enabled": tts_ko_enabled
    }
    try: await asyncio.to_thread(db.reference('bot_settings').set, settings_data)
    except Exception: pass
    await asyncio.to_thread(set_db_value, "bf_notify_enabled", bf_notify_enabled)
    await asyncio.to_thread(set_db_value, "lib_notify_enabled", lib_notify_enabled)
    await asyncio.to_thread(set_db_value, "ppl_notify_enabled", ppl_notify_enabled)
    await asyncio.to_thread(set_db_value, "tts_th_enabled", tts_th_enabled)
    await asyncio.to_thread(set_db_value, "tts_en_enabled", tts_en_enabled)
    await asyncio.to_thread(set_db_value, "tts_ko_enabled", tts_ko_enabled)
    await asyncio.to_thread(save_json_local, SETTINGS_FILE, settings_data)

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
        if db_bf is not None: bf_notify_enabled = db_bf
        if db_lib is not None: lib_notify_enabled = db_lib
        if db_ppl is not None: ppl_notify_enabled = db_ppl
        if db_th is not None: tts_th_enabled = db_th
        if db_en is not None: tts_en_enabled = db_en
        if db_ko is not None: tts_ko_enabled = db_ko
        return

    if data:
        bf_notify_enabled = data.get("bf_notify_enabled", True)
        lib_notify_enabled = data.get("lib_notify_enabled", True)
        ppl_notify_enabled = data.get("ppl_notify_enabled", True)
        tts_th_enabled = data.get("tts_th_enabled", True)
        tts_en_enabled = data.get("tts_en_enabled", True)
        tts_ko_enabled = data.get("tts_ko_enabled", True)

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
    try: custom_data = await asyncio.to_thread(db.reference('custom_bosses').get)
    except Exception: pass
    if not custom_data: custom_data = get_db_value("custom_bosses", None)
    if custom_data:
        for boss_name, data in custom_data.items():
            BOSS_RESPAWN_TIMES[boss_name] = timedelta(seconds=data["total_seconds"])
            BOSS_CD_TEXT[boss_name] = data["cd_text"]
            ADVANCE_NOTICE_SECONDS[boss_name] = data["notice_seconds"]
            ADVANCE_NOTICE_TEXT[boss_name] = data["notice_text"]
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

# 🔥 แก้ไขฟังก์ชันแจงเตือนด้วยเสียงเพื่อรองรับการเปิด-ปิดในแต่ละภาษา
async def speak_in_guild(guild: discord.Guild, text_th: str, text_en: str = None, text_ko: str = None, target_channel: discord.VoiceChannel = None):
    # ปรับเงื่อนไขใหม่: อนุญาตให้ทำงานถ้ามีข้อความภาษาใดภาษาหนึ่ง
    if not guild or (not text_th and not text_en and not text_ko): return
    
    if guild.id not in voice_locks:
        voice_locks[guild.id] = asyncio.Lock()

    if guild.id in disconnect_tasks:
        disconnect_tasks[guild.id].cancel()
        del disconnect_tasks[guild.id]

    async with voice_locks[guild.id]:
        if target_channel:
            target_channels = [target_channel]
        else:
            target_channels = [
                channel for channel in guild.voice_channels
                if len(channel.members) > 0 and any(not m.bot for m in channel.members)
            ]
            if not target_channels and guild.voice_client and guild.voice_client.channel:
                target_channels = [guild.voice_client.channel]
            if not target_channels and guild.voice_channels:
                target_channels = [guild.voice_channels[0]]

        if not target_channels: return

        unique_id = uuid.uuid4().hex
        tts_filename_th = f"temp_tts_th_{guild.id}_{unique_id}.mp3" if text_th else None
        tts_filename_en = f"temp_tts_en_{guild.id}_{unique_id}.mp3" if text_en else None
        tts_filename_ko = f"temp_tts_ko_{guild.id}_{unique_id}.mp3" if text_ko else None

        try:
            if text_th:
                communicate_th = edge_tts.Communicate(text_th, VOICE_THAI, rate="-20%", pitch="+10Hz")
                await communicate_th.save(tts_filename_th)
            
            if text_en:
                communicate_en = edge_tts.Communicate(text_en, VOICE_ENG, rate="-10%", pitch="+0Hz")
                await communicate_en.save(tts_filename_en)
                
            if text_ko:
                communicate_ko = edge_tts.Communicate(text_ko, VOICE_KOR, rate="-10%", pitch="+0Hz")
                await communicate_ko.save(tts_filename_ko)
        except Exception as tts_err:
            print(f"❌ เกิดข้อผิดพลาดในการแปลง TTS: {tts_err}")
            return

        ffmpeg_executable = get_ffmpeg_path()
        try:
            for idx, channel in enumerate(target_channels):
                try:
                    vc = guild.voice_client
                    
                    if vc is None:
                        try:
                            vc = await channel.connect(reconnect=True, timeout=15)
                        except discord.ClientException:
                            if guild.voice_client:
                                await guild.voice_client.disconnect(force=True)
                            vc = await channel.connect(reconnect=True, timeout=15)
                    else:
                        if not vc.is_connected():
                            try: await vc.disconnect(force=True)
                            except: pass
                            vc = await channel.connect(reconnect=True, timeout=15)
                        elif vc.channel.id != channel.id:
                            try:
                                await vc.move_to(channel)
                            except discord.ClientException:
                                await vc.disconnect(force=True)
                                vc = await channel.connect(reconnect=True, timeout=15)
                    
                    await asyncio.sleep(1.0)

                    if vc.is_playing(): 
                        vc.stop()

                    if vc.is_connected():
                        # เล่นเสียงภาษาไทย (ถ้าเปิด)
                        if text_th and os.path.exists(tts_filename_th) and os.path.getsize(tts_filename_th) > 0:
                            audio_source_th = discord.FFmpegPCMAudio(
                                tts_filename_th, executable=ffmpeg_executable, before_options="-loglevel error", options="-vn"
                            )
                            loop = asyncio.get_running_loop()
                            play_finished_th = asyncio.Event()

                            def after_playing_th(error):
                                if error: print(f"❌ เกิดข้อผิดพลาดขณะเล่นเสียง (TH) ใน {channel.name}: {error}")
                                loop.call_soon_threadsafe(play_finished_th.set)

                            try:
                                vc.play(audio_source_th, after=after_playing_th)
                                await asyncio.wait_for(play_finished_th.wait(), timeout=30)
                            except asyncio.TimeoutError:
                                print(f"⚠️ การเล่นเสียง (TH) หมดเวลา (Timeout) ในห้อง {channel.name}")
                                if vc.is_playing(): vc.stop()
                            except Exception as play_err:
                                print(f"❌ ระบบเล่นเสียงขัดข้อง (TH) ในห้อง {channel.name}: {play_err}")
                                loop.call_soon_threadsafe(play_finished_th.set)

                        # เล่นเสียงภาษาอังกฤษ (ถ้าเปิด)
                        if text_en and vc.is_connected() and os.path.exists(tts_filename_en) and os.path.getsize(tts_filename_en) > 0:
                            if text_th: await asyncio.sleep(0.5) # พักหายใจถ้ามีเสียงไทยมาก่อน
                            
                            audio_source_en = discord.FFmpegPCMAudio(
                                tts_filename_en, executable=ffmpeg_executable, before_options="-loglevel error", options="-vn"
                            )
                            play_finished_en = asyncio.Event()

                            def after_playing_en(error):
                                if error: print(f"❌ เกิดข้อผิดพลาดขณะเล่นเสียง (EN) ใน {channel.name}: {error}")
                                loop.call_soon_threadsafe(play_finished_en.set)

                            try:
                                vc.play(audio_source_en, after=after_playing_en)
                                await asyncio.wait_for(play_finished_en.wait(), timeout=30)
                            except asyncio.TimeoutError:
                                print(f"⚠️ การเล่นเสียง (EN) หมดเวลา (Timeout) ในห้อง {channel.name}")
                                if vc.is_playing(): vc.stop()
                            except Exception as play_err:
                                print(f"❌ ระบบเล่นเสียงขัดข้อง (EN) ในห้อง {channel.name}: {play_err}")
                                loop.call_soon_threadsafe(play_finished_en.set)
                                
                        # เล่นเสียงภาษาเกาหลี (ถ้าเปิด)
                        if text_ko and vc.is_connected() and os.path.exists(tts_filename_ko) and os.path.getsize(tts_filename_ko) > 0:
                            if text_th or text_en: await asyncio.sleep(0.5) # พักหายใจถ้ามีเสียงก่อนหน้า
                            
                            audio_source_ko = discord.FFmpegPCMAudio(
                                tts_filename_ko, executable=ffmpeg_executable, before_options="-loglevel error", options="-vn"
                            )
                            play_finished_ko = asyncio.Event()

                            def after_playing_ko(error):
                                if error: print(f"❌ เกิดข้อผิดพลาดขณะเล่นเสียง (KO) ใน {channel.name}: {error}")
                                loop.call_soon_threadsafe(play_finished_ko.set)

                            try:
                                vc.play(audio_source_ko, after=after_playing_ko)
                                await asyncio.wait_for(play_finished_ko.wait(), timeout=30)
                            except asyncio.TimeoutError:
                                print(f"⚠️ การเล่นเสียง (KO) หมดเวลา (Timeout) ในห้อง {channel.name}")
                                if vc.is_playing(): vc.stop()
                            except Exception as play_err:
                                print(f"❌ ระบบเล่นเสียงขัดข้อง (KO) ในห้อง {channel.name}: {play_err}")
                                loop.call_soon_threadsafe(play_finished_ko.set)

                    if idx < len(target_channels) - 1: await asyncio.sleep(1.5)
                
                except Exception as e:
                    print(f"❌ เกิดข้อผิดพลาดในการเข้าห้องเสียง {channel.name}: {e}")
                    if guild.voice_client:
                        try: await guild.voice_client.disconnect(force=True)
                        except: pass

        finally:
            if tts_filename_th and os.path.exists(tts_filename_th):
                try: os.remove(tts_filename_th)
                except Exception: pass
            if tts_filename_en and os.path.exists(tts_filename_en):
                try: os.remove(tts_filename_en)
                except Exception: pass
            if tts_filename_ko and os.path.exists(tts_filename_ko):
                try: os.remove(tts_filename_ko)
                except Exception: pass

    async def auto_disconnect_after_delay():
        try:
            await asyncio.sleep(10)
            if guild.voice_client and guild.voice_client.is_connected():
                await guild.voice_client.disconnect(force=True)
                print(f"🔌 ออกจากห้องเสียงอัตโนมัติเนื่องจากไม่มีการใช้งานเกิน 10 วินาที ในเซิร์ฟเวอร์: {guild.name}")
        except asyncio.CancelledError:
            pass

    disconnect_tasks[guild.id] = asyncio.create_task(auto_disconnect_after_delay())

# ==========================================
# 🔊 Event แจ้งเตือน + ทักทายเมื่อมีคนเข้าห้องเสียง
# ==========================================
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    global ppl_notify_enabled, vip_config
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    if member.bot: return

    if before.channel != after.channel and after.channel is not None:
        if vip_config.get("enabled", False) and member.id == vip_config.get("user_id"):
            greeting_text = vip_config.get("message", "")
            if greeting_text: asyncio.create_task(speak_in_guild(member.guild, greeting_text, target_channel=after.channel))
        elif ppl_notify_enabled:
            user_name = clean_display_name(member.display_name)
            channel_name = clean_display_name(after.channel.name)
            
            # ตรวจสอบว่าภาษาไหนเปิดใช้งานบ้าง
            greeting_text_th = f"ยินดีต้อนรับคุณ {user_name} เข้าสู่ห้อง{channel_name}" if tts_th_enabled else None
            greeting_text_en = f"Welcome {user_name} to {channel_name}." if tts_en_enabled else None
            greeting_text_ko = f"{user_name}님, {channel_name} 방에 오신 것을 환영합니다." if tts_ko_enabled else None
            
            asyncio.create_task(speak_in_guild(member.guild, greeting_text_th, greeting_text_en, greeting_text_ko, target_channel=after.channel))

@bot.event
async def on_ready():
    global is_bot_ready
    if is_bot_ready:
        print("🔄 บอท Reconnect สำเร็จ (ข้ามการโหลดข้อมูลซ้ำ)")
        return
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🔊 ใช้ FFmpeg จากตำแหน่ง: {get_ffmpeg_path()}")

    init_db()
    await load_bot_settings()
    await load_custom_bosses()
    await load_boss_data()
    await load_live_config()
    await load_vip_config()

    # ไม่มี QuickActionsView ใน source ที่ให้มา จึงอาจต้องตรวจสอบ class นี้นะครับ หากไม่มีให้ลบบรรทัดล่างออก
    try: bot.add_view(QuickActionsView()) 
    except: pass

    await asyncio.sleep(3)
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    
    if not check_boss_notifications.is_running(): check_boss_notifications.start()
    if not check_bf_notifications.is_running(): check_bf_notifications.start()
    if not check_library_boss_notifications.is_running(): check_library_boss_notifications.start()
    
    # ไม่มี update_live_embed และ check_auto_disconnect ใน source ที่ให้มา ถ้าลูปเหล่านี้มีอยู่ในโค้ดจริงของคุณ อย่าลืมนำกลับมาด้วยครับ
    try:
        if not update_live_embed.is_running(): update_live_embed.start()
        if not check_auto_disconnect.is_running(): check_auto_disconnect.start()
    except: pass

    is_bot_ready = True
    loop = asyncio.get_running_loop()
    threading.Thread(target=start_firebase_listener, args=(loop,), daemon=True).start()
    threading.Thread(target=start_firebase_settings_listener, args=(loop,), daemon=True).start()

# ==========================================
# ⏰ 7. Tasks เช็กเวลาเตือน + BF + Library Boss + Live Embed + Auto-Disconnect
# ==========================================
@tasks.loop(seconds=30)
async def check_bf_notifications():
    global last_bf_notified_hour, bf_notify_enabled
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
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
                    
                    # ตรวจสอบภาษาที่เปิดอยู่
                    spoken_text_th = "Battlefield กำลังจะเริ่มในอีก 3 นาทีค่ะ" if tts_th_enabled else None
                    spoken_text_en = "Battlefield will start in 3 minutes." if tts_en_enabled else None
                    spoken_text_ko = "배틀필드가 3분 후에 시작됩니다." if tts_ko_enabled else None
                    
                    asyncio.create_task(speak_in_guild(guild, spoken_text_th, spoken_text_en, spoken_text_ko))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_bf_notifications': {e}")

@tasks.loop(seconds=30)
async def check_library_boss_notifications():
    global last_lib_notified_key, lib_notify_enabled
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
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

                    # ตรวจสอบภาษาที่เปิดอยู่
                    spoken_text_th = "Library Boss ถึงเวลาเตรียมตัวแล้วค่ะ" if tts_th_enabled else None
                    spoken_text_en = "It's time to prepare for Library Boss." if tts_en_enabled else None
                    spoken_text_ko = "도서관 보스 준비 시간입니다." if tts_ko_enabled else None
                    
                    asyncio.create_task(speak_in_guild(guild, spoken_text_th, spoken_text_en, spoken_text_ko))
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
                    try: channel = await bot.fetch_channel(channel_id)
                    except Exception: channel = None

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
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_boss_notifications': {e}")
