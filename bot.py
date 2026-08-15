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
is_updating_from_bot = False

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone ไทย & Helper Functions
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))

def parse_bool(val, default=False) -> bool:
    """แปลงค่าต่างๆ (String, Bool, Int) ให้เป็น Boolean อย่างถูกต้อง"""
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
                if st < now - timedelta(hours=12):
                    st += timedelta(days=1)
                return st
            except ValueError:
                try:
                    time_obj = datetime.strptime(cleaned_val, "%H:%M").time()
                    st = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
                    if st < now - timedelta(hours=12):
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
lib_notify_enabled = True
ppl_notify_enabled = True
vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
last_bf_notified_hour = -1
last_lib_notified_key = ""

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
    "Bigmama": "บิ๊กมาม่า",
    "Chief Magief": "ชีฟ มาเกียฟ",
    "Faith": "เฟธ",
    "Apapa": "อาปาป้า",
    "Corrupt Forest Keeper": "คอร์รัปต์ ฟอเรสต์ คีปเปอร์",
    "Recluse": "เรคลูซ",
    "Blackskull": "แบล็กสกัลป์",
    "Sleepy Kooii": "สลีปปี้ คูอี",
    "Awaken Kooii": "อเวเคน คูอี",
    "Eeheehee": "อีฮีฮี",
    "Ooheeheek": "โอฮีฮีก",
    "Oohehe": "โอเฮเฮ้",
    "Guardian Imp": "การ์เดียน อิมป์",
    "Devilang": "เดวิลแลง",
    "Blackjuno": "แบล็กจูโน่",
    "Blacksky": "แบล็กสกาย",
    "Red Fox": "เรดฟ็อกซ์",
    "7tailfox": "เซเว่นเทลฟ็อกซ์",
    "777Tailfox": "ทริปเปิลเซเว่นเทลฟ็อกซ์",
    "Sunrise Flower": "ซันไรส์ ฟลาวเวอร์",
    "Magma Senior Thief": "แมกม่า ซีเนียร์ ธีฟ",
    "Bbinikjoe": "บีนิกโจ",
    "Bigmouse": "บิ๊กเมาส์",
    "Caligo": "คาลิโก้",
    "Poison Root Flower": "พอยซัน รูท ฟลาวเวอร์",
    "Contaminated Queen Bee": "คอนทามิเนตเต็ด ควีนบี",
    "Rotten Pudding": "รอตเทน พุดดิ้ง",
    "Swamp Flower Monster": "สแวมป์ ฟลาวเวอร์ มอนสเตอร์",
    "Ukpana": "อุคปาน่า",
    "Darlene the Witch": "ดาร์ลีน เดอะ วิทช์",
    "Illust": "อิลลัสต์",
    "Actaemon": "แอคธีมอน",
    "Aiyo's Protector": "ไอโย โปรเตกเตอร์",
    "Glucose": "กลูโคส",
    "Overload": "โอเวอร์โหลด",
    "Soul Lich": "โซล ลิช",
    "Platanista": "พลานิสต้า",
    "Barslaf": "บาร์สลาฟ",
    "Billiard": "บิลเลียด",
    "Shaaack": "ชาค",
    "Suuuk": "ซุก",
    "Sususuk": "ซูซูซุก",
    "sandgrave": "แซนด์เกรฟ",
    "Elder Beholder": "เอลเดอร์ บีโฮลเดอร์"
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

    try:
        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"❌ ส่ง Audit Log ไม่สำเร็จ: {e}")

# ==========================================
# 🛡️ 4. Check สำหรับตรวจสอบสิทธิ์ผู้ใช้งาน
# ==========================================
def check_user_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if not TARGET_ROLE_IDS:
        return True
    user_role_ids = [role.id for role in member.roles]
    return any(role_id in TARGET_ROLE_IDS for role_id in user_role_ids)

def has_allowed_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
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
            data_to_save[boss_name] = {
                "spawn_time": st.isoformat(),
                "spawnTimeMs": spawn_ms,
                "channel_id": data.get("channel_id"),
                "notified_advance": parse_bool(data.get("notified_advance", False)),
                "notified_spawn": parse_bool(data.get("notified_spawn", False)),
                "noticeMinutes": int(ADVANCE_NOTICE_SECONDS.get(boss_name, 300) / 60)
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
        now = datetime.now(TZ_THAI)
        with schedule_lock:
            boss_schedule.clear()
            for boss_name, data in saved_data.items():
                if isinstance(data, dict):
                    raw_st = data.get("spawnTimeMs") or data.get("spawn_time")
                    st = parse_to_thai_datetime(raw_st)
                    if st:
                        notified_adv = parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
                        notified_spwn = parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))
                        
                        time_left = (st - now).total_seconds()
                        notice_limit = ADVANCE_NOTICE_SECONDS.get(boss_name, 300)
                        if time_left > notice_limit:
                            notified_adv = False
                            notified_spwn = False
                        elif time_left > 0:
                            notified_spwn = False
                            
                        boss_schedule[boss_name] = {
                            "spawn_time": st,
                            "channel_id": data.get("channel_id"),
                            "notified_advance": notified_adv,
                            "notified_spawn": notified_spwn
                        }
        print(f"✅ โหลดตารางบอสจาก Firebase สำเร็จ {len(boss_schedule)} รายการ")

def start_firebase_listener(loop):
    def listener(event):
        global is_updating_from_bot
        if not is_bot_ready or is_updating_from_bot:
            return

        try:
            ref_boss = db.reference('boss_schedule')
            snapshot = ref_boss.get()
            if snapshot and isinstance(snapshot, dict):
                now = datetime.now(TZ_THAI)
                with schedule_lock:
                    for boss_name, data in snapshot.items():
                        if not isinstance(data, dict): continue
                        
                        raw_st = data.get("spawnTimeMs") or data.get("spawn_time")
                        st = parse_to_thai_datetime(raw_st)
                        if not st: continue

                        existing = boss_schedule.get(boss_name)
                        
                        # ✅ หากเป็นบอสใหม่ หรือเวลาเกิดเปลี่ยนไป ให้ Reset การแจ้งเตือนเสมอ!
                        if not existing or existing.get("spawn_time") != st:
                            notified_adv = False
                            notified_spwn = False
                        else:
                            notified_adv = parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
                            notified_spwn = parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))

                        time_left = (st - now).total_seconds()
                        notice_limit = ADVANCE_NOTICE_SECONDS.get(boss_name, 300)
                        
                        # Reset อัตโนมัติป้องกัน Flag ค้าง
                        if time_left > notice_limit:
                            notified_adv = False
                            notified_spwn = False
                        elif time_left > 0:
                            notified_spwn = False

                        boss_schedule[boss_name] = {
                            "spawn_time": st,
                            "channel_id": data.get("channel_id") or (existing.get("channel_id") if existing else None),
                            "notified_advance": notified_adv,
                            "notified_spawn": notified_spwn
                        }
                    
                    # ลบข้อมูลบอสที่ถูกลบออกจาก Firebase
                    for boss_name in list(boss_schedule.keys()):
                        if boss_name not in snapshot:
                            del boss_schedule[boss_name]
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดใน Firebase Listener: {e}")

    try:
        ref_boss = db.reference('boss_schedule')
        ref_boss.listen(listener)
        print("🟢 Firebase Listener พร้อมทำงานเรียบร้อย!")
    except Exception as e:
        print(f"❌ ไม่สามารถเปิด Firebase Listener ได้: {e}")

async def save_bot_settings():
    settings_data = {
        "bf_notify_enabled": bf_notify_enabled,
        "lib_notify_enabled": lib_notify_enabled,
        "ppl_notify_enabled": ppl_notify_enabled
    }
    try:
        await asyncio.to_thread(db.reference('bot_settings').set, settings_data)
    except Exception: pass
    await asyncio.to_thread(set_db_value, "bf_notify_enabled", bf_notify_enabled)
    await asyncio.to_thread(set_db_value, "lib_notify_enabled", lib_notify_enabled)
    await asyncio.to_thread(set_db_value, "ppl_notify_enabled", ppl_notify_enabled)
    await asyncio.to_thread(save_json_local, SETTINGS_FILE, settings_data)

async def load_bot_settings():
    global bf_notify_enabled, lib_notify_enabled, ppl_notify_enabled
    data = None
    try:
        data = await asyncio.to_thread(db.reference('bot_settings').get)
    except Exception: pass

    if not data:
        db_bf = get_db_value("bf_notify_enabled", None)
        db_lib = get_db_value("lib_notify_enabled", None)
        db_ppl = get_db_value("ppl_notify_enabled", None)
        if db_bf is not None: bf_notify_enabled = db_bf
        if db_lib is not None: lib_notify_enabled = db_lib
        if db_ppl is not None: ppl_notify_enabled = db_ppl
        return

    if data:
        bf_notify_enabled = data.get("bf_notify_enabled", True)
        lib_notify_enabled = data.get("lib_notify_enabled", True)
        ppl_notify_enabled = data.get("ppl_notify_enabled", True)

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

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(
            title="🚫 ปฏิเสธการเข้าถึง",
            description="คำสั่งนี้อนุญาตเฉพาะ **Administrator (ผู้ดูแลระบบ)** เท่านั้นครับ!",
            color=discord.Color.red()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        embed = discord.Embed(
            title="🚫 ปฏิเสธการเข้าถึง",
            description="คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้!\nอนุญาตเฉพาะผู้ได้รับสิทธิ์หรือมีบทบาทที่กำหนดเท่านั้นครับ",
            color=discord.Color.red()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        print(f"❌ เกิดข้อผิดพลาดของระบบ: {error}")

def get_ffmpeg_path():
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"⚠️ ไม่สามารถโหลด FFmpeg จาก imageio-ffmpeg ได้: {e}")

    cwd = os.getcwd()
    for filename in ["ffmpeg.exe", "ffmpeg"]:
        local_path = os.path.join(cwd, filename)
        if os.path.exists(local_path):
            return local_path

    for filename in ["ffmpeg.exe", "ffmpeg"]:
        bin_path = os.path.join(cwd, "ffmpeg", "bin", filename)
        if os.path.exists(bin_path):
            return bin_path

    system_path = shutil.which("ffmpeg")
    if system_path:
        return system_path

    return "ffmpeg"

def clean_display_name(name: str) -> str:
    if not name: return "สมาชิก"
    cleaned = re.sub(r'[^\w\s\u0E00-\u0E7F]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else "สมาชิก"

async def speak_in_guild(guild: discord.Guild, text: str, target_channel: discord.VoiceChannel = None):
    if not guild or not text: return

    if guild.id not in voice_locks:
        voice_locks[guild.id] = asyncio.Lock()

    async with voice_locks[guild.id]:
        if target_channel:
            target_channels = [target_channel]
        else:
            target_channels = [
                channel for channel in guild.voice_channels
                if any(not m.bot for m in channel.members)
            ]

        if not target_channels:
            return

        unique_id = uuid.uuid4().hex
        tts_filename = f"temp_tts_{guild.id}_{unique_id}.mp3"

        try:
            communicate = edge_tts.Communicate(text, VOICE_THAI, rate="-20%", pitch="+10Hz")
            await communicate.save(tts_filename)
            if not os.path.exists(tts_filename) or os.path.getsize(tts_filename) == 0:
                return
        except Exception as tts_err:
            print(f"❌ เกิดข้อผิดพลาดในการแปลง TTS: {tts_err}")
            return

        ffmpeg_executable = get_ffmpeg_path()

        try:
            for idx, channel in enumerate(target_channels):
                try:
                    vc = guild.voice_client
                    if vc is None:
                        vc = await channel.connect(reconnect=True, timeout=15)
                        await asyncio.sleep(1.0)
                    else:
                        if not vc.is_connected():
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            vc = await channel.connect(reconnect=True, timeout=15)
                            await asyncio.sleep(1.0)
                        elif vc.channel != channel:
                            await vc.move_to(channel)
                            await asyncio.sleep(1.0)

                    if vc.is_playing():
                        vc.stop()

                    audio_source = discord.FFmpegPCMAudio(
                        tts_filename,
                        executable=ffmpeg_executable,
                        before_options="-loglevel error",
                        options="-vn"
                    )

                    loop = asyncio.get_running_loop()
                    play_finished = asyncio.Event()

                    def after_playing(error):
                        if error:
                            print(f"❌ เกิดข้อผิดพลาดขณะเล่นเสียงใน {channel.name}: {error}")
                        loop.call_soon_threadsafe(play_finished.set)

                    try:
                        vc.play(audio_source, after=after_playing)
                        await asyncio.wait_for(play_finished.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        print(f"⚠️ การเล่นเสียง TTS หมดเวลา (Timeout) ในห้อง {channel.name}")
                    except Exception as play_err:
                        print(f"❌ ระบบเล่นเสียงขัดข้องในห้อง {channel.name}: {play_err}")
                        loop.call_soon_threadsafe(play_finished.set)

                    if idx < len(target_channels) - 1:
                        await asyncio.sleep(1.5)

                except Exception as e:
                    print(f"❌ เกิดข้อผิดพลาดในการเข้าห้องเสียง {channel.name}: {e}")

            vc = guild.voice_client
            if vc and vc.is_connected():
                try:
                    await vc.disconnect()
                except Exception as e:
                    print(f"❌ เกิดข้อผิดพลาดในการตัดสาย: {e}")
        finally:
            if os.path.exists(tts_filename):
                try:
                    os.remove(tts_filename)
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
            if greeting_text:
                asyncio.create_task(speak_in_guild(member.guild, greeting_text, target_channel=after.channel))
        
        elif ppl_notify_enabled:
            user_name = clean_display_name(member.display_name)
            channel_name = clean_display_name(after.channel.name)
            greeting_text = f"ยินดีต้อนรับคุณ {user_name} เข้าสู่ห้อง{channel_name}"
            asyncio.create_task(speak_in_guild(member.guild, greeting_text, target_channel=after.channel))

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

    bot.add_view(QuickActionsView())

    await asyncio.sleep(3)
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    
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

    is_bot_ready = True
    loop = asyncio.get_running_loop()
    threading.Thread(target=start_firebase_listener, args=(loop,), daemon=True).start()

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
                        except Exception as e:
                            print(f"❌ ส่งข้อความเตือน BF ไม่สำเร็จ: {e}")
                    
                    spoken_text = "Battlefield กำลังจะเริ่มในอีก 3 นาทีค่ะ"
                    asyncio.create_task(speak_in_guild(guild, spoken_text))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_bf_notifications': {e}")

@tasks.loop(seconds=30)
async def check_library_boss_notifications():
    global last_lib_notified_key, lib_notify_enabled
    if not lib_notify_enabled: return

    try:
        now = datetime.now(TZ_THAI)
        # เช็กเวลา 08:50 น. และ 20:50 น. ทุกวัน
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
                        except Exception as e:
                            print(f"❌ ส่งข้อความเตือน Library Boss ไม่สำเร็จ: {e}")

                    spoken_text = "Library Boss ถึงเวลาเตรียมตัวแล้วค่ะ"
                    asyncio.create_task(speak_in_guild(guild, spoken_text))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_library_boss_notifications': {e}")

# 🔥 ปรับลดเวลาตรวจเช็กเวลาบอสเป็นทุกๆ 10 วินาที เพื่อการตอบสนองที่เรียลไทม์และไม่หลุดเตือน
@tasks.loop(seconds=10)
async def check_boss_notifications():
    try:
        now = datetime.now(TZ_THAI)
        changed = False
        
        with schedule_lock:
            schedule_copy = boss_schedule.copy()
        
        for boss_name, data in list(schedule_copy.items()):
            spawn_time = parse_to_thai_datetime(data["spawn_time"])
            if not spawn_time:
                continue

            channel_id = data.get("channel_id")
            if channel_id is not None:
                try:
                    channel_id = int(channel_id)
                except ValueError:
                    channel_id = None

            # แปลง Boolean อย่างรัดกุม รองรับทั้งสตริงจาก Web และ True/False แท้
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

            if not channels_to_notify: 
                continue

            time_left = (spawn_time - now).total_seconds()
            notice_limit = ADVANCE_NOTICE_SECONDS.get(boss_name, 300)
            notice_text = ADVANCE_NOTICE_TEXT.get(boss_name, "5 นาที")
            spoken_name = BOSS_PRONUNCIATION.get(boss_name, boss_name)
            
            # [สเต็ป 1] แจ้งเตือนล่วงหน้าตามเวลาที่ตั้งค่าไว้
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

                    if guild and guild.id not in notified_guild_ids:
                        notified_guild_ids.add(guild.id)
                        asyncio.create_task(speak_in_guild(guild, f"บอส {spoken_name} จะเกิดในอีก {notice_text} ค่ะ"))
                    
                with schedule_lock:
                    if boss_name in boss_schedule:
                        boss_schedule[boss_name]["notified_advance"] = True
                changed = True

            # [สเต็ป 2] แจ้งเตือนเมื่อบอสเกิดทันที
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

                    if guild and guild.id not in notified_guild_ids:
                        notified_guild_ids.add(guild.id)
                        asyncio.create_task(speak_in_guild(guild, f"บอส {spoken_name} เกิดแล้วค่ะ"))
                    
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

            try:
                cached_live_message = await channel.fetch_message(message_id)
            except Exception: return

        now = datetime.now(TZ_THAI)
        embed = discord.Embed(
            title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time",
            description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`",
            color=discord.Color.teal()
        )

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
                
                if time_left_sec <= 0:
                    time_left_str = "เกิดแล้ว!"
                else:
                    m, s = divmod(int(time_left_sec), 60)
                    h, m = divmod(m, 60)
                    if h > 0:
                        time_left_str = f"อีก {h} ชม. {m} นาที"
                    else:
                        time_left_str = f"อีก {m} นาที {s} วินาที"

                notice_text = ADVANCE_NOTICE_TEXT.get(boss, "5 นาที")
                embed.add_field(
                    name=f"👾 {boss}",
                    value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**\n*(เตือนล่วงหน้า {notice_text})*",
                    inline=False
                )
            
            if len(sorted_bosses) > 20:
                embed.add_field(name="📌 หมายเหตุ", value=f"*และยังมีบอสอีก {len(sorted_bosses) - 20} ตัวในคิว*", inline=False)

        embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")
        try:
            await cached_live_message.edit(embed=embed)
        except Exception as e:
            print(f"❌ อัปเดต Live Embed ไม่สำเร็จ: {e}")
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
                if len(human_members) == 0:
                    if guild.id not in voice_empty_start:
                        voice_empty_start[guild.id] = now
                    else:
                        elapsed = (now - voice_empty_start[guild.id]).total_seconds()
                        if elapsed >= 180:
                            try:
                                await vc.disconnect()
                                print(f"🔌 Auto-disconnected จาก {vc.channel.name} เนื่องจากไม่มีสมาชิกอยู่ในห้องเกิน 3 นาที")
                            except Exception as e:
                                print(f"❌ ตัดสายไม่สำเร็จ: {e}")
                            del voice_empty_start[guild.id]
                else:
                    if guild.id in voice_empty_start:
                        del voice_empty_start[guild.id]
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_auto_disconnect': {e}")

async def boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    all_bosses = sorted(BOSS_RESPAWN_TIMES.keys())
    
    if not current:
        return [app_commands.Choice(name=boss, value=boss) for boss in all_bosses[:25]]
    
    choices = [
        app_commands.Choice(name=boss, value=boss)
        for boss in all_bosses
        if current.lower() in boss.lower()
    ]
    return choices[:25]

# ==========================================
# 🎛️ 8. Dynamic Boss Select & Quick Actions
# ==========================================
class BossSelect(discord.ui.Select):
    def __init__(self, boss_chunk: list, placeholder: str, custom_id: str, row: int):
        options = [
            discord.SelectOption(label=boss, value=boss)
            for boss in boss_chunk
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_boss = self.values[0]
        await interaction.response.send_message(
            f"🎯 เลือกบอส: **{self.values[0]}** เรียบร้อยแล้ว (กดปุ่ม ⚔️ บอสตายแล้ว เพื่อระบุเวลาได้ทันที)", 
            ephemeral=True
        )

class KillBossModal(discord.ui.Modal, title="⚔️ บันทึกเวลาบอสตาย"):
    kill_time_input = discord.ui.TextInput(
        label="เวลาที่บอสตาย (ไม่ใส่ = เวลาปัจจุบัน)",
        style=discord.TextStyle.short,
        placeholder="ตัวอย่าง: 17:30 หรือ 1730 (ปล่อยว่างได้)",
        required=False,
        max_length=8
    )

    def __init__(self, selected_boss: str):
        super().__init__()
        self.selected_boss = selected_boss

    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.now(TZ_THAI)
        raw_val = self.kill_time_input.value.strip() if self.kill_time_input.value else ""
        
        try:
            boss_died_at = parse_time_input(raw_val, now)
        except Exception:
            await interaction.response.send_message(
                "❌ รูปแบบเวลาไม่ถูกต้อง! กรุณากรอกแบบ **17:30** หรือ **1730** (หรือเว้นว่างไว้เพื่อใช้เวลาปัจจุบัน)",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        next_spawn = boss_died_at + BOSS_RESPAWN_TIMES[self.selected_boss]

        with schedule_lock:
            boss_schedule[self.selected_boss] = {
                "spawn_time": next_spawn,
                "channel_id": interaction.channel_id,
                "notified_advance": False,
                "notified_spawn": False
            }
        await save_boss_data()

        embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
        embed.add_field(name="👾 ชื่อบอส", value=f"`{self.selected_boss}`", inline=True)
        embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
        embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=BOSS_CD_TEXT[self.selected_boss], inline=True)
        embed.add_field(name="🔔 บอสจะเกิดเวลา", value=f"**{next_spawn.strftime('%H:%M:%S น.')}**", inline=False)
        embed.set_footer(text=f"บันทึกผ่าน Quick Action โดย {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)
        await send_audit_log(
            interaction.guild, 
            interaction.user, 
            "กดปุ่มบอสตาย (Quick Action)", 
            f"👾 บอส: `{self.selected_boss}`\n⏱️ เวลาตาย: {boss_died_at.strftime('%H:%M:%S น.')}\n🔔 เวลาเกิดถัดไป: {next_spawn.strftime('%H:%M:%S น.')}", 
            discord.Color.red()
        )

class QuickActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        all_bosses = sorted(BOSS_RESPAWN_TIMES.keys())
        self.selected_boss = all_bosses[0] if all_bosses else "Wadangka"
        
        chunks = []
        for i in range(0, min(len(all_bosses), 75), 25):
            chunks.append(all_bosses[i:i + 25])
        
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

        if member:
            has_required_role = any(role.name in allowed_role_names for role in member.roles)

        if not (is_owner or has_required_role):
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ใช้งานปุ่มนี้! (อนุญาตเฉพาะเจ้าของ Server และผู้ที่มีโรล Eternal, Meaw, Anti เท่านั้น)", ephemeral=True)
            return

        await interaction.response.defer()

        mentions = []
        if guild:
            for role in guild.roles:
                if role.name in allowed_role_names:
                    mentions.append(role.mention)

        mention_target = " ".join(mentions) if mentions else "@everyone"

        embed = discord.Embed(
            title="🔔 เรียกสมาชิกคนลุยบอส!",
            description=f"📢 {interaction.user.mention} เรียกสมาชิกลุยบอส **{self.selected_boss}** ด่วน!",
            color=discord.Color.gold()
        )
        await interaction.followup.send(content=mention_target, embed=embed)

        spoken_boss = BOSS_PRONUNCIATION.get(self.selected_boss, self.selected_boss)
        spoken_text = f"เรียกคนลุยบอส {spoken_boss} ด่วนค่ะ"
        if guild:
            asyncio.create_task(speak_in_guild(guild, spoken_text))

        await send_audit_log(guild, interaction.user, "กดปุ่มเรียกคน (Quick Action)", f"🔔 เรียกคนลุยบอส: `{self.selected_boss}`", discord.Color.gold())

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

    embed_summary, tts_text = generate_boss_time_summary()
    if tts_text and interaction.guild:
        asyncio.create_task(speak_in_guild(interaction.guild, tts_text))

# ==========================================
# 🔊 9. Voice & Notify Commands
# ==========================================
@bot.tree.command(name="notify", description="เปิดหรือปิดระบบแจ้งเตือนสงคราม Battlefield (BF)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[
    app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"),
    app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")
])
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
@app_commands.choices(status=[
    app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"),
    app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")
])
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
@app_commands.describe(
    status="เลือกเปิด (on) หรือปิด (off) ระบบทักทายคนพิเศษ",
    user="เลือกสมาชิกคนพิเศษ",
    message="ข้อความพูดทักทายคนพิเศษ"
)
@app_commands.choices(status=[
    app_commands.Choice(name="เปิดระบบทักทายคนพิเศษ (on)", value="on"),
    app_commands.Choice(name="ปิดระบบทักทายคนพิเศษ (off)", value="off")
])
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

@bot.tree.command(name="join", description="ดึงบอทเข้าห้องเสียงที่คุณกำลังใช้งาน")
async def join_voice(interaction: discord.Interaction):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ คุณต้องเชื่อมต่ออยู่ในห้องเสียงก่อนใช้คำสั่งนี้!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild

    if guild.voice_client is not None:
        await guild.voice_client.move_to(voice_channel)
    else:
        await voice_channel.connect()

    embed = discord.Embed(title="🔊 เชื่อมต่อห้องเสียงสำเร็จ", description=f"บอทเข้าสู่ห้องเสียง **{voice_channel.name}** เรียบร้อยแล้ว!", color=discord.Color.green())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="leave", description="สั่งให้บอทออกจากห้องเสียง")
async def leave_voice(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild

    if guild.voice_client:
        await guild.voice_client.disconnect()
        await interaction.followup.send("👋 ออกจากห้องเสียงเรียบร้อยแล้ว!")
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

    embed = discord.Embed(
        title="📢 ประกาศข้อความเสียง (Global Notice)",
        description=f"**ข้อความ:** {message}\n\nกำลังไล่ประกาศในทุกห้องเสียงที่มีสมาชิกอยู่...",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(speak_in_guild(interaction.guild, message))

# ==========================================
# ⚔️ 10. Boss Slash Commands
# ==========================================
def generate_boss_time_summary():
    now = datetime.now(TZ_THAI)
    with schedule_lock:
        schedule_copy = boss_schedule.copy()

    if not schedule_copy:
        return None, "ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบครับ"

    sorted_bosses = sorted(
        schedule_copy.items(), 
        key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now
    )

    embed = discord.Embed(
        title="⌛ สรุปเวลาที่เหลือของบอสทุกตัว (เรียงจากน้อยไปมาก)",
        description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`",
        color=discord.Color.purple()
    )

    tts_lines = ["สรุปเวลาบอสเรียงจากน้อยไปมากค่ะ"]

    display_bosses = sorted_bosses[:20]
    for boss, data in display_bosses:
        spawn_time = parse_to_thai_datetime(data["spawn_time"])
        if not spawn_time: continue

        time_left_sec = (spawn_time - now).total_seconds()
        spoken_name = BOSS_PRONUNCIATION.get(boss, boss)

        if time_left_sec <= 0:
            time_left_str = "เกิดแล้ว!"
            tts_lines.append(f"บอส {spoken_name} เกิดแล้วค่ะ")
        else:
            m, s = divmod(int(time_left_sec), 60)
            h, m = divmod(m, 60)

            parts = []
            if h > 0: parts.append(f"{h} ชม.")
            if m > 0 or h > 0: parts.append(f"{m} นาที")
            parts.append(f"{s} วินาที")

            time_left_str = f"อีก {' '.join(parts)}"

            if h > 0: tts_time = f"{h} ชั่วโมง {m} นาที"
            elif m > 0: tts_time = f"{m} นาที"
            else: tts_time = f"{s} วินาที"

            tts_lines.append(f"บอส {spoken_name} เหลืออีก {tts_time}")

        embed.add_field(
            name=f"👾 {boss}",
            value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**",
            inline=False
        )

    if len(sorted_bosses) > 20:
        embed.add_field(name="📌 หมายเหตุ", value=f"*ยังมีบอสอีก {len(sorted_bosses) - 20} ตัว สามารถดูเพิ่มเติมได้บน Dashboard*", inline=False)

    tts_text = " ".join(tts_lines)
    return embed, tts_text

@bot.tree.command(name="time", description="คำนวณเวลาที่เหลือของบอสทุกตัว เรียงจากน้อยไปมาก และส่งเสียงอ่าน TTS ในห้องเสียง")
async def boss_time_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, tts_text = generate_boss_time_summary()

    if embed is None:
        await interaction.followup.send(tts_text)
        return

    await interaction.followup.send(embed=embed)

    asyncio.create_task(speak_in_guild(interaction.guild, tts_text))
    await send_audit_log(interaction.guild, interaction.user, "เช็กเวลาบอสพร้อม TTS (/time)", "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย", discord.Color.purple())

@bot.command(name="time")
async def boss_time_prefix(ctx: commands.Context):
    embed, tts_text = generate_boss_time_summary()

    if embed is None:
        await ctx.send(tts_text)
        return

    await ctx.send(embed=embed)

    asyncio.create_task(speak_in_guild(ctx.guild, tts_text))
    await send_audit_log(ctx.guild, ctx.author, "เช็กเวลาบอสพร้อม TTS (!time)", "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย", discord.Color.purple())

@bot.tree.command(name="kill", description="บันทึกเวลาที่บอสตายเพื่อเริ่มคำนวณเวลานับถอยหลัง")
@app_commands.describe(
    boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการบันทึกเวลา",
    kill_time="ระบุเวลาที่บอสตาย (เช่น 17:30 หรือ 1730) ถ้าไม่ระบุจะใช้เวลาปัจจุบัน"
)
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str = None):
    await interaction.response.defer()
    
    matched_name = None
    for b in BOSS_RESPAWN_TIMES.keys():
        if b.lower() == boss_name.lower():
            matched_name = b
            break

    if not matched_name:
        await interaction.followup.send(f"❌ ไม่พบชื่อบอส **{boss_name}** ในระบบ!", ephemeral=True)
        return

    now = datetime.now(TZ_THAI)
    try:
        boss_died_at = parse_time_input(kill_time, now)
    except ValueError:
        await interaction.followup.send("❌ รูปแบบเวลาไม่ถูกต้อง! กรุณากรอกแบบ **17:30** หรือ **1730**", ephemeral=True)
        return

    next_spawn = boss_died_at + BOSS_RESPAWN_TIMES[matched_name]

    with schedule_lock:
        boss_schedule[matched_name] = {
            "spawn_time": next_spawn,
            "channel_id": interaction.channel_id,
            "notified_advance": False,
            "notified_spawn": False
        }
    await save_boss_data()

    embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{matched_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
    embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=BOSS_CD_TEXT[matched_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=f"**{next_spawn.strftime('%H:%M:%S น.')}**", inline=False)
    embed.set_footer(text=f"บันทึกโดย {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "บันทึกเวลาบอสตาย (/kill)", f"👾 บอส: `{matched_name}`\n🔔 เวลาเกิดถัดไป: {next_spawn.strftime('%H:%M:%S น.')}", discord.Color.red())

@bot.tree.command(name="addboss", description="เพิ่มบอสใหม่หรือแก้ไขเวลา คูลดาวน์ / เวลาเตือนล่วงหน้า")
@app_commands.describe(
    name="ชื่อบอสที่ต้องการเพิ่มหรือแก้ไข",
    hours="จำนวนชั่วโมงคูลดาวน์",
    minutes="จำนวนนาทีคูลดาวน์",
    seconds="จำนวนวินาทีคูลดาวน์",
    notice_minutes="เวลาที่ต้องการให้เตือนล่วงหน้า (นาที)"
)
@has_allowed_role()
async def add_boss(interaction: discord.Interaction, name: str, hours: int = 0, minutes: int = 0, seconds: int = 0, notice_minutes: int = 5):
    await interaction.response.defer()
    
    total_seconds = (hours * 3600) + (minutes * 60) + seconds
    if total_seconds <= 0:
        await interaction.followup.send("❌ เวลาคูลดาวน์รวมต้องมากกว่า 0 วินาทีครับ!", ephemeral=True)
        return

    matched_name = name
    for existing_name in BOSS_RESPAWN_TIMES.keys():
        if existing_name.lower() == name.lower():
            matched_name = existing_name
            break

    BOSS_RESPAWN_TIMES[matched_name] = timedelta(seconds=total_seconds)
    
    cd_parts = []
    if hours > 0: cd_parts.append(f"{hours} ชั่วโมง")
    if minutes > 0: cd_parts.append(f"{minutes} นาที")
    if seconds > 0: cd_parts.append(f"{seconds} วินาที")
    cd_text = " ".join(cd_parts) if cd_parts else "0 วินาที"
    
    BOSS_CD_TEXT[matched_name] = cd_text
    ADVANCE_NOTICE_SECONDS[matched_name] = notice_minutes * 60
    ADVANCE_NOTICE_TEXT[matched_name] = f"{notice_minutes} นาที"
    if matched_name not in BOSS_PRONUNCIATION:
        BOSS_PRONUNCIATION[matched_name] = matched_name

    await save_custom_bosses_to_github()

    with schedule_lock:
        boss_schedule[matched_name] = {
            "spawn_time": datetime.now(TZ_THAI),
            "channel_id": interaction.channel_id,
            "notified_advance": True,
            "notified_spawn": True
        }
    await save_boss_data()

    embed = discord.Embed(title="✅ เพิ่ม/แก้ไขบอสสำเร็จ", color=discord.Color.green())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{matched_name}`", inline=True)
    embed.add_field(name="⏳ คูลดาวน์", value=cd_text, inline=True)
    embed.add_field(name="🔔 เตือนล่วงหน้า", value=f"{notice_minutes} นาที", inline=True)
    
    await interaction.followup.send(embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "เพิ่ม/แก้ไขบอส (/addboss)", f"➕ บอส: `{matched_name}`\n⏳ คูลดาวน์: {cd_text}", discord.Color.green())

@bot.tree.command(name="delboss", description="ลบบอสออกจากตารางนับถอยหลัง")
@app_commands.describe(boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def del_boss(interaction: discord.Interaction, boss_name: str):
    await interaction.response.defer()

    matched_key = None
    with schedule_lock:
        for k in boss_schedule.keys():
            if k.lower() == boss_name.lower():
                matched_key = k
                break

        if matched_key:
            del boss_schedule[matched_key]

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

    with schedule_lock:
        schedule_copy = boss_schedule.copy()

    if not schedule_copy:
        embed = discord.Embed(title="📜 ตารางเวลาบอส", description="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ\nใช้คำสั่ง `/kill [ชื่อบอส]` เพื่อเริ่มบันทึกเวลาได้เลยครับ", color=discord.Color.blue())
        await interaction.followup.send(embed=embed)
        return

    now = datetime.now(TZ_THAI)
    embed = discord.Embed(title="📜 ตารางเวลาบอสเกิดทั้งหมด", description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.blue())

    sorted_bosses = sorted(
        schedule_copy.items(), 
        key=lambda x: parse_to_thai_datetime(x[1]["spawn_time"]) or now
    )
    
    display_bosses = sorted_bosses[:20]
    for boss, data in display_bosses:
        spawn_time = parse_to_thai_datetime(data["spawn_time"])
        if not spawn_time: continue
        
        time_left_sec = (spawn_time - now).total_seconds()
        
        if time_left_sec <= 0:
            time_left_str = "เกิดแล้ว!"
        else:
            m, s = divmod(int(time_left_sec), 60)
            h, m = divmod(m, 60)
            if h > 0:
                time_left_str = f"อีก {h} ชม. {m} นาที"
            else:
                time_left_str = f"อีก {m} นาที {s} วินาที"

        notice_text = ADVANCE_NOTICE_TEXT.get(boss, "5 นาที")
        embed.add_field(name=f"👾 {boss}", value=f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | นับถอยหลัง: **{time_left_str}**\n*(เตือนล่วงหน้า {notice_text})*", inline=False)

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

# ==========================================
# 🚀 11. Run Bot
# ==========================================
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        while True:
            try:
                bot.run(token)
                break
            except discord.errors.HTTPException as e:
                if e.status == 429:
                    print("⚠️ ติด Rate Limit ตอนเริ่มต้นระบบ กำลังพักและรอ 60 วินาทีก่อนลองรันใหม่...")
                    time.sleep(60)
                else:
                    raise e
            except Exception as e:
                print(f"❌ เกิดข้อผิดพลาดของระบบ: {e}")
                break
    else:
        print("❌ ไม่พบ DISCORD_TOKEN ใน Environment Variables! กรุณาตั้งค่าก่อนรันบอท")
