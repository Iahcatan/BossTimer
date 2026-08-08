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
is_updating_from_bot = False  # Flag สำหรับป้องกัน Infinite Re-entry จาก Listener

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone ไทย & Config
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))

def parse_to_thai_datetime(data_val):
    """ฟังก์ชันช่วยแปลงข้อมูลเวลาจากชนิดต่างๆ ให้เป็น datetime (TZ_THAI)"""
    if isinstance(data_val, (int, float)):
        return datetime.fromtimestamp(data_val / 1000.0, tz=TZ_THAI)
    elif isinstance(data_val, str):
        try:
            st = datetime.fromisoformat(data_val)
            if st.tzinfo is None:
                return st.replace(tzinfo=TZ_THAI)
            return st.astimezone(TZ_THAI)
        except Exception:
            return None
    elif isinstance(data_val, datetime):
        if data_val.tzinfo is None:
            return data_val.replace(tzinfo=TZ_THAI)
        return data_val.astimezone(TZ_THAI)
    return None

def parse_time_input(time_str: str, now: datetime) -> datetime:
    """แปลงรูปแบบเวลาที่ผู้ใช้กรอก (เช่น 17:30, 1730, 17.30) ให้เป็น datetime"""
    if not time_str or not time_str.strip():
        return now

    cleaned = time_str.strip().replace(".", ":")

    # กรณีเป็นตัวเลข 3 หรือ 4 หลัก เช่น 1730, 530, 0530
    if re.fullmatch(r'\d{3,4}', cleaned):
        if len(cleaned) == 3:
            hh = int(cleaned[0])
            mm = int(cleaned[1:])
        else:
            hh = int(cleaned[:2])
            mm = int(cleaned[2:])
        ss = 0
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
    if boss_died_at > now:
        boss_died_at -= timedelta(days=1)
    return boss_died_at

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
        
    sorted_bosses = sorted(schedule_copy.items(), key=lambda x: x[1]["spawn_time"])
    
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
            "notice_text": ADVANCE_NOTICE_TEXT.get(boss_name, "5 นาที")
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

bf_notify_enabled = True
ppl_notify_enabled = True
vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
last_bf_notified_hour = -1

cached_live_message = None
VOICE_THAI = "th-TH-PremwadeeNeural"

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
    "Wadangka": 1800,
    "Elemental Queen": 1800,
    "Tank": 300,
    "Swirl Flame": 300,
    "Maelstrom": 300,
    "Twister": 300,
    "Bigmama": 1800,
    "Chief Magief": 300,
    "Faith": 1800,
    "Apapa": 300,
    "Corrupt Forest Keeper": 300,
    "Recluse": 1800,
    "Blackskull": 300,
    "Sleepy Kooii": 300,
    "Awaken Kooii": 300,
    "Eeheehee": 300,
    "Ooheeheek": 300,
    "Oohehe": 300,
    "Guardian Imp": 300,
    "Devilang": 1800,
    "Blackjuno": 300,
    "Blacksky": 300,
    "Red Fox": 300,
    "7tailfox": 300,
    "777Tailfox": 300,
    "Sunrise Flower": 300,
    "Magma Senior Thief": 300,
    "Bbinikjoe": 300,
    "Bigmouse": 300,
    "Caligo": 3600,
    "Poison Root Flower": 300,
    "Contaminated Queen Bee": 300,
    "Rotten Pudding": 300,
    "Swamp Flower Monster": 300,
    "Ukpana": 1800,
    "Darlene the Witch": 1800,
    "Illust": 1800,
    "Actaemon": 1800,
    "Aiyo's Protector": 1800,
    "Glucose": 300,
    "Overload": 300,
    "Soul Lich": 1800,
    "Platanista": 3600,
    "Barslaf": 1800,
    "Billiard": 1800,
    "Shaaack": 300,
    "Suuuk": 300,
    "Sususuk": 300,
    "sandgrave": 300,
    "Elder Beholder": 300
}

ADVANCE_NOTICE_TEXT = {
    "Wadangka": "30 นาที",
    "Elemental Queen": "30 นาที",
    "Tank": "5 นาที",
    "Swirl Flame": "5 นาที",
    "Maelstrom": "5 นาที",
    "Twister": "5 นาที",
    "Bigmama": "30 นาที",
    "Chief Magief": "5 นาที",
    "Faith": "30 นาที",
    "Apapa": "5 นาที",
    "Corrupt Forest Keeper": "5 นาที",
    "Recluse": "30 นาที",
    "Blackskull": "5 นาที",
    "Sleepy Kooii": "5 นาที",
    "Awaken Kooii": "5 นาที",
    "Eeheehee": "5 นาที",
    "Ooheeheek": "5 นาที",
    "Oohehe": "5 นาที",
    "Guardian Imp": "5 นาที",
    "Devilang": "30 นาที",
    "Blackjuno": "5 นาที",
    "Blacksky": "5 นาที",
    "Red Fox": "5 นาที",
    "7tailfox": "5 นาที",
    "777Tailfox": "5 นาที",
    "Sunrise Flower": "5 นาที",
    "Magma Senior Thief": "5 นาที",
    "Bbinikjoe": "5 นาที",
    "Bigmouse": "5 นาที",
    "Caligo": "1 ชั่วโมง",
    "Poison Root Flower": "5 นาที",
    "Contaminated Queen Bee": "5 นาที",
    "Rotten Pudding": "5 นาที",
    "Swamp Flower Monster": "5 นาที",
    "Ukpana": "30 นาที",
    "Darlene the Witch": "30 นาที",
    "Illust": "30 นาที",
    "Actaemon": "30 นาที",
    "Aiyo's Protector": "30 นาที",
    "Glucose": "5 นาที",
    "Overload": "5 นาที",
    "Soul Lich": "30 นาที",
    "Platanista": "1 ชั่วโมง",
    "Barslaf": "30 นาที",
    "Billiard": "30 นาที",
    "Shaaack": "5 นาที",
    "Suuuk": "5 นาที",
    "Sususuk": "5 นาที",
    "sandgrave": "5 นาที",
    "Elder Beholder": "5 นาที"
}

BOSS_PRONUNCIATION = {
    "Wadangka": "วาดังการ์",
    "Elemental Queen": "เอเลเมนทัล ควีน",
    "Tank": "แท้งก์",
    "Swirl Flame": "สเวิร์ล เฟลม",
    "Maelstrom": "เมลสตรอม",
    "Twister": "ทวิสเตอร์",
    "Bigmama": "บิ๊กมาม่า",ต้องขออภัยในความไม่สะดวกก่อนหน้านี้ด้วยครับ เข้าใจเลยว่าการต้องมานั่งหาจุดแก้และนำโค้ดไปวางทีละส่วนนั้นทำให้เสียเวลาและเสี่ยงต่อการเกิดข้อผิดพลาดได้ง่าย 

นี่คือโค้ดฉบับสมบูรณ์ทั้งหมดตามที่คุณต้องการครับ คุณสามารถกดคัดลอก (Copy) โค้ดด้านล่างนี้ทั้งหมด แล้วนำไปวางทับโค้ดในไฟล์เดิมได้เลยทันที โดยที่ระบบและสาระสำคัญส่วนอื่นๆ จะยังคงทำงานได้ตามปกติครับ[cite: 1]

```python
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

# 🔥 Firebase Admin SDK Setup
import firebase_admin
from firebase_admin import credentials, db

# ==========================================
# 🔥 0. เชื่อมต่อ Firebase Realtime Database
# ==========================================
FIREBASE_KEY_PATH = "firebase-key.json"
DATABASE_URL = "[https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app](https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app)"

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
is_updating_from_bot = False  # Flag สำหรับป้องกัน Infinite Re-entry จาก Listener

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone ไทย & Config
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))

def parse_to_thai_datetime(data_val):
    """ฟังก์ชันช่วยแปลงข้อมูลเวลาจากชนิดต่างๆ ให้เป็น datetime (TZ_THAI)"""
    if isinstance(data_val, (int, float)):
        return datetime.fromtimestamp(data_val / 1000.0, tz=TZ_THAI)
    elif isinstance(data_val, str):
        try:
            st = datetime.fromisoformat(data_val)
            if st.tzinfo is None:
                return st.replace(tzinfo=TZ_THAI)
            return st.astimezone(TZ_THAI)
        except Exception:
            return None
    elif isinstance(data_val, datetime):
        if data_val.tzinfo is None:
            return data_val.replace(tzinfo=TZ_THAI)
        return data_val.astimezone(TZ_THAI)
    return None

def parse_time_input(time_str: str, now: datetime) -> datetime:
    """แปลงรูปแบบเวลาที่ผู้ใช้กรอก (เช่น 17:30, 1730, 17.30) ให้เป็น datetime"""
    if not time_str or not time_str.strip():
        return now

    cleaned = time_str.strip().replace(".", ":")

    # กรณีเป็นตัวเลข 3 หรือ 4 หลัก เช่น 1730, 530, 0530
    if re.fullmatch(r'\d{3,4}', cleaned
