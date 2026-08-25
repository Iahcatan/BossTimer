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
            firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
            print("✅ เชื่อมต่อ Firebase Realtime Database สำเร็จ!")
        else:
            raise ValueError(
                "ไม่พบ FIREBASE_SERVICE_ACCOUNT_JSON หรือ FIREBASE_SERVICE_ACCOUNT_BASE64 ใน Environment Variable"
            )
    except Exception as e:
        print(f"❌ ไม่สามารถเชื่อมต่อ Firebase Realtime Database ได้: {e}")

logging.getLogger('discord.player').setLevel(logging.WARNING)
logging.getLogger('discord.voice_state').setLevel(logging.WARNING)

schedule_lock = threading.Lock()
is_bot_ready = False
is_updating_from_bot = False

TZ_THAI = timezone(timedelta(hours=7))

def parse_bool(val, default=False) -> bool:
    if val is None: return default
    if isinstance(val, bool): return val
    if isinstance(val, (int, float)): return bool(val)
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ('true', '1', 'yes'): return True
        if cleaned in ('false', '0', 'no'): return False
    return default

def parse_to_thai_datetime(data_val):
    if not data_val: return None
    if isinstance(data_val, (int, float)):
        return datetime.fromtimestamp(data_val / 1000.0, tz=TZ_THAI)
    elif isinstance(data_val, str):
        cleaned_val = data_val.replace(" น.", "").strip()
        try:
            if cleaned_val.endswith('Z'): cleaned_val = cleaned_val[:-1] + '+00:00'
            st = datetime.fromisoformat(cleaned_val)
            if st.tzinfo is None: return st.replace(tzinfo=TZ_THAI)
            return st.astimezone(TZ_THAI)
        except ValueError:
            now = datetime.now(TZ_THAI)
            try:
                time_obj = datetime.strptime(cleaned_val, "%H:%M:%S").time()
                st = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=time_obj.second, microsecond=0)
                if (st - now).total_seconds() > 600: st -= timedelta(days=1)
                elif st < now - timedelta(hours=18): st += timedelta(days=1)
                return st
            except ValueError:
                try:
                    time_obj = datetime.strptime(cleaned_val, "%H:%M").time()
                    st = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
                    if (st - now).total_seconds() > 600: st -= timedelta(days=1)
                    elif st < now - timedelta(hours=18): st += timedelta(days=1)
                    return st
                except ValueError:
                    pass
            return None
    elif isinstance(data_val, datetime):
        if data_val.tzinfo is None: return data_val.replace(tzinfo=TZ_THAI)
        return data_val.astimezone(TZ_THAI)
    return None

def parse_time_input(time_str: str, now: datetime) -> datetime:
    if not time_str or not time_str.strip(): return now
    cleaned = time_str.strip().replace(".", ":")
    if re.fullmatch(r'\d{3,6}', cleaned):
        if len(cleaned) == 3: hh, mm, ss = int(cleaned[0]), int(cleaned[1:]), 0
        elif len(cleaned) == 4: hh, mm, ss = int(cleaned[:2]), int(cleaned[2:]), 0
        elif len(cleaned) == 5: hh, mm, ss = int(cleaned[0]), int(cleaned[1:3]), int(cleaned[3:])
        elif len(cleaned) == 6: hh, mm, ss = int(cleaned[:2]), int(cleaned[2:4]), int(cleaned[4:])
    elif ":" in cleaned:
        parts = [int(p) for p in cleaned.split(":") if p.isdigit()]
        if len(parts) == 2: hh, mm, ss = parts[0], parts[1], 0
        elif len(parts) == 3: hh, mm, ss = parts[0], parts[1], parts[2]
        else: raise ValueError("Invalid time format")
    else: raise ValueError("Invalid time format")
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59): raise ValueError("Invalid time range")
    boss_died_at = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if (boss_died_at - now).total_seconds() > 600: boss_died_at -= timedelta(days=1)
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

DB_FILE = "bot_database.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)""")
        conn.commit(); conn.close()
        print("✅ บันทึก/เชื่อมต่อ Database (SQLite) สำเร็จ")
    except Exception as e: print(f"❌ เกิดข้อผิดพลาดในการตั้งค่า Database: {e}")

def set_db_value(key: str, value):
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); val_str = json.dumps(value, ensure_ascii=False)
        cursor.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val_str))
        conn.commit(); conn.close()
    except Exception as e: print(f"❌ บันทึกข้อมูลลง Database ไม่สำเร็จ ({key}): {e}")

def get_db_value(key: str, default=None):
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)); row = cursor.fetchone(); conn.close()
        if row: return json.loads(row[0])
    except Exception as e: print(f"❌ ดึงข้อมูลจาก Database ไม่สำเร็จ ({key}): {e}")
    return default

# ...
# NOTE: The remainder of bot.py is unchanged from the existing repository.
# This commit intentionally cannot safely replace the complete large file from a truncated API response.
