# SKYNET_BOSSTIMER_V73_COMMAND18_INTEGRITY_FIX_2026-09-08
import os

# 🛡️ ON-DEMAND MULTI-CHANNEL PATCH v3
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
import queue
from collections import deque
from typing import Optional
import aiohttp
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, render_template_string, request, jsonify, Response
from waitress import serve
import edge_tts
import imageio_ffmpeg

# 🔥 Firebase Admin SDK Setup
import firebase_admin
from firebase_admin import credentials, db, auth as firebase_auth

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

NOTICE_BF_PATCH_VERSION = "V96_LIBRARY_BOSS_SCHEDULE_CANONICAL_FIX_2026-09-11-R1"

# V57 runtime split:
# - web = Render Dashboard/Firebase/API only; NEVER starts Discord Gateway.
# - bot = dedicated bot runtime; owns Discord Gateway/REST/Voice/TTS listeners.
# Defaulting to web is deliberate: a missing env var must never accidentally
# start Discord traffic from the Render shared outbound IP.
SKYNET_RUNTIME_ROLE = os.environ.get("SKYNET_RUNTIME_ROLE", "web").strip().lower()
if SKYNET_RUNTIME_ROLE not in {"web", "bot"}:
    raise RuntimeError("SKYNET_RUNTIME_ROLE must be exactly 'web' or 'bot'")
print(f"🧭 SKYNET runtime role: {SKYNET_RUNTIME_ROLE}")
print(f"🧩 BOT PATCH VERSION: {NOTICE_BF_PATCH_VERSION}")

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

def parse_date_input(date_str: str, now: datetime):
    """Parse DD/MM/YYYY. Blank date means today in Thailand timezone."""
    if not date_str or not str(date_str).strip():
        return now.date()
    cleaned = str(date_str).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if not m:
        raise ValueError("Invalid date format")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day, tzinfo=TZ_THAI).date()
    except ValueError:
        raise ValueError("Invalid date value")

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

HTML_TEMPLATE = '<!DOCTYPE html>\n<html lang="th">\n<head>\n    <meta charset="UTF-8">\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n    <title>Boss Timer Dashboard</title>\n    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">\n    <link href="https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;600&display=swap" rel="stylesheet">\n    \n    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-app-compat.js"></script>\n    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-database-compat.js"></script>\n    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-auth-compat.js"></script>\n    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>\n\n    <style>\n        body {\n            background-color: #0f172a;\n            color: #f8fafc;\n            font-family: \'Kanit\', sans-serif;\n        }\n        .card {\n            background-color: #1e293b;\n            border: 1px solid #334155;\n            border-radius: 12px;\n        }\n        .form-label {\n            color: #ffffff !important;\n            font-weight: 500;\n        }\n        .form-control, .form-select {\n            background-color: #0f172a !important;\n            border: 1px solid #334155;\n            color: #ffffff !important;\n        }\n        .form-control::placeholder {\n            color: #64748b;\n        }\n        .form-control:focus, .form-select:focus {\n            background-color: #0f172a !important;\n            color: #ffffff !important;\n            border-color: #3b82f6;\n            box-shadow: none;\n        }\n        .table {\n            color: #f8fafc;\n        }\n        .table-dark {\n            --bs-table-bg: #1e293b;\n            --bs-table-hover-bg: #334155;\n        }\n        .status-badge {\n            font-size: 0.85rem;\n            padding: 5px 10px;\n            border-radius: 15px;\n        }\n        .settings-bar {\n            background-color: #1e293b;\n            border: 1px solid #334155;\n            border-radius: 12px;\n            padding: 12px 20px;\n        }\n        footer, footer small {\n            color: #ffffff !important;\n        }\n        .modal-content {\n            background-color: #1e293b;\n            color: #f8fafc;\n            border: 1px solid #334155;\n        }\n        .nav-tabs .nav-link {\n            color: #94a3b8;\n            border: none;\n        }\n        .nav-tabs .nav-link.active {\n            background-color: transparent;\n            color: #38bdf8;\n            border-bottom: 3px solid #38bdf8;\n            font-weight: 600;\n        }\n        .attendance-stat { min-height: 120px; }\n        .attendance-stat .attendance-stat-value { color: #ffffff !important; }\n        .attendance-stat .fs-3, .attendance-stat .fs-4, .attendance-stat .fs-5 { color: #ffffff !important; }\n        .attendance-status-open { color: #22c55e; }\n        .attendance-status-scheduled { color: #f59e0b; }\n        .attendance-status-closed { color: #94a3b8; }\n        .attendance-table td, .attendance-table th { white-space: nowrap; }\n    </style>\n</head>\n<body>\n    <div id="authContainer" class="container py-5" style="max-width: 450px;">\n        <div class="card p-4 shadow-lg">\n            \n            <div class="d-flex justify-content-end mb-2">\n                <select id="authLangSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeLanguage(this.value)">\n                    <option value="th">🇹🇭 ไทย (TH)</option>\n                    <option value="en">🇺🇸 English (EN)</option>\n                    <option value="ko">🇰🇷 한국어 (KO)</option>\n                </select>\n            </div>\n\n            <h3 class="text-center text-warning mb-4" data-i18n="authTitle">⚔️ Boss Timer Access</h3>\n            \n            <ul class="nav nav-tabs nav-justified mb-3" id="authTabs" role="tablist">\n                <li class="nav-item">\n                    <button class="nav-link active" id="login-tab" data-bs-toggle="tab" data-bs-target="#loginPane" type="button" data-i18n="tabLogin">เข้าสู่ระบบ</button>\n                </li>\n                <li class="nav-item">\n                    <button class="nav-link" id="register-tab" data-bs-toggle="tab" data-bs-target="#registerPane" type="button" data-i18n="tabRegister">ลงทะเบียน</button>\n                </li>\n            </ul>\n\n            <div class="tab-content">\n                <div class="tab-pane fade show active" id="loginPane" role="tabpanel">\n                    <form id="loginForm" autocomplete="off">\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelUsername">ชื่อผู้ใช้</label>\n                            <input type="text" id="loginUser" class="form-control" placeholder="กรอกชื่อผู้ใช้" data-i18n-ph="phLoginUser" required>\n                        </div>\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelPassword">รหัสผ่าน</label>\n                            <input type="password" id="loginPass" class="form-control" placeholder="กรอกรหัสผ่าน" data-i18n-ph="phLoginPass" required>\n                        </div>\n                        <div class="form-check mb-3">\n                            <input class="form-check-input" type="checkbox" id="rememberMe">\n                            <label class="form-check-label text-white" for="rememberMe" data-i18n="rememberMe">จำชื่อผู้ใช้</label>\n                        </div>\n                        <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnLogin">🔑 เข้าสู่ระบบ</button>\n                        <div id="loginDebug" class="small text-info mt-2" style="min-height:1.2em"></div>\n                    </form>\n                </div>\n\n                <div class="tab-pane fade" id="registerPane" role="tabpanel">\n                    <form id="registerForm" autocomplete="off">\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelUsername">ชื่อผู้ใช้</label>\n                            <input type="text" id="regUser" class="form-control" placeholder="ตั้งชื่อผู้ใช้" data-i18n-ph="phRegUser" required>\n                        </div>\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelPassword">รหัสผ่าน</label>\n                            <input type="password" id="regPass" class="form-control" placeholder="ตั้งรหัสผ่าน" data-i18n-ph="phRegPass" required>\n                        </div>\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelRegCode">โค้ดสำหรับสมัคร</label>\n                            <input type="password" id="regCode" class="form-control" placeholder="กรอกโค้ดสำหรับสมัคร" data-i18n-ph="phRegCode" required>\n                        </div>\n                        <button type="submit" class="btn btn-success w-100 fw-bold" data-i18n="btnRegister">📝 ลงทะเบียน</button>\n                    </form>\n                </div>\n            </div>\n        </div>\n    </div>\n\n    <div id="mainDashboard" class="container py-4" style="display: none;">\n        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">\n            <h2 data-i18n="title">⚔️ Boss Timer Dashboard</h2>\n            <div class="d-flex align-items-center gap-2">\n                <span class="badge bg-success status-badge" id="syncStatus" data-i18n="online">🟢 Realtime Sync Active</span>\n                <span class="badge bg-info text-dark status-badge" id="userBadge">👤 User</span>\n                <button id="adminPanelMenuBtn" onclick="scrollToAdminPanel()" data-admin-only class="btn btn-outline-danger btn-sm" style="display:none;">🛡️ Admin Panel</button>\n                <button onclick="openChangeCodeModal()" data-admin-only class="btn btn-outline-warning btn-sm" data-i18n="btnConfigCode">🔑 เปลี่ยนโค้ดสมัคร</button>\n                <button onclick="logout()" class="btn btn-outline-danger btn-sm" data-i18n="btnLogout">🚪 ออกจากระบบ</button>\n            </div>\n        </div>\n\n        <div class="settings-bar mb-4 d-flex flex-wrap align-items-center justify-content-between gap-3">\n            <div class="d-flex flex-wrap align-items-center gap-3">\n                <div class="d-flex align-items-center gap-2">\n                    <label class="form-label mb-0 text-nowrap" data-i18n="labelLanguage">🌐 ภาษา:</label>\n                    <select id="langSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeLanguage(this.value)">\n                        <option value="th">🇹🇭 ไทย (TH)</option>\n                        <option value="en">🇺🇸 English (EN)</option>\n                        <option value="ko">🇰🇷 한국어 (KO)</option>\n                    </select>\n                </div>\n                <div class="d-flex align-items-center gap-2">\n                    <label class="form-label mb-0 text-nowrap" data-i18n="labelTimezone">🌍 เขตเวลา:</label>\n                    <select id="tzSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeTimezone(this.value)">\n                        <option value="auto" data-i18n="tzAuto">💻 อัตโนมัติ (ตามเครื่อง)</option>\n                        <option value="UTC">🌐 UTC (Universal Time)</option>\n                        <option value="Asia/Bangkok">🇹🇭 Bangkok, Jakarta, Hanoi (UTC+7)</option>\n                        <option value="Asia/Singapore">🇸🇬 Singapore, KL, Manila (UTC+8)</option>\n                        <option value="Asia/Hong_Kong">🇭🇰 Hong Kong, Beijing, Taipei (UTC+8)</option>\n                        <option value="Asia/Tokyo">🇯🇵 Tokyo, Seoul (UTC+9)</option>\n                        <option value="Europe/London">🇬🇧 London, Dublin (UTC+0/+1)</option>\n                        <option value="America/New_York">🇺🇸 New York, Toronto (EST)</option>\n                    </select>\n                </div>\n                <div class="d-flex align-items-center gap-2">\n                    <span class="badge bg-dark border border-secondary text-info px-3 py-2 fs-6" id="liveClockDisplay" style="letter-spacing: 1px;">00:00:00</span>\n                </div>\n            </div>\n            <div class="d-flex gap-2">\n                <button onclick="openBotSettingsModal()" class="btn btn-outline-info btn-sm fw-bold" data-i18n="btnBotSettings">⚙️ ตั้งค่าบอท</button>\n                <button id="notifyToggleBtn" onclick="toggleNotifications()" class="btn btn-warning btn-sm fw-bold" data-i18n="enableNotify">🔔 เปิดระบบเสียง & แจ้งเตือน</button>\n            </div>\n        </div>\n\n        <!-- 🔐 Admin User Management -->\n        <div id="adminPanel" class="card p-4 mb-4 shadow-sm" style="display:none;">\n            <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">\n                <div>\n                    <h4 class="card-title text-danger mb-1" data-i18n="adminPanelTitle">🛡️ Admin • จัดการผู้ใช้งาน</h4>\n                    <small class="text-white-50" data-i18n="adminPanelSubtitle">อนุมัติผู้สมัคร ตรวจสอบประวัติ และดูผู้ที่กำลังใช้งาน • 🟢 ออนไลน์ = มี heartbeat ภายใน 90 วินาที • ปุ่มอนุมัติอยู่ในตารางด้านล่าง</small>\n                    <div id="adminDataStatus" class="small text-white-50 mt-1">กำลังตรวจสอบข้อมูลผู้ใช้งาน...</div>\n                </div>\n                <div class="d-flex gap-2">\n                    <button class="btn btn-outline-info btn-sm" onclick="loadAdminUsers()" data-i18n="adminRefresh">🔄 รีเฟรช</button>\n                    <button id="adminCollapseBtn" class="btn btn-outline-secondary btn-sm" onclick="toggleAdminPanel()">▼ เปิด</button>\n                </div>\n            </div>\n\n            <div id="adminPanelContent" style="display:none;">\n            <div class="row g-3 mb-3">\n                <div class="col-md-4">\n                    <div class="card p-3 h-100">\n                        <div class="text-warning small" data-i18n="adminPending">รออนุมัติ</div>\n                        <div class="fs-3 fw-bold" id="adminPendingCount">0</div>\n                    </div>\n                </div>\n                <div class="col-md-4">\n                    <div class="card p-3 h-100">\n                        <div class="text-success small" data-i18n="adminActive">กำลังใช้งาน</div>\n                        <div class="fs-3 fw-bold" id="adminActiveCount">0</div>\n                    </div>\n                </div>\n                <div class="col-md-4">\n                    <div class="card p-3 h-100">\n                        <div class="text-info small" data-i18n="adminTotal">ผู้ใช้ทั้งหมด</div>\n                        <div class="fs-3 fw-bold" id="adminTotalCount">0</div>\n                    </div>\n                </div>\n            </div>\n\n            <div class="table-responsive">\n                <table class="table table-dark table-hover align-middle mb-0">\n                    <thead>\n                        <tr>\n                            <th data-i18n="adminThUser">ผู้ใช้</th>\n                            <th data-i18n="adminThStatus">สถานะ</th>\n                            <th data-i18n="adminThCreated">สมัครเมื่อ</th>\n                            <th data-i18n="adminThLastLogin">เข้าสู่ระบบล่าสุด</th>\n                            <th data-i18n="adminThLastSeen">ใช้งานล่าสุด</th>\n                            <th data-i18n="adminThLogins">จำนวนครั้ง</th>\n                            <th data-i18n="adminThAction">จัดการ</th>\n                        </tr>\n                    </thead>\n                    <tbody id="adminUsersBody"></tbody>\n                </table>\n            </div>\n            </div>\n        </div>\n\n        <div class="card p-4 mb-4 shadow-sm">\n            <h4 class="card-title text-warning mb-3" data-i18n="formTitle">⏱️ บันทึกเวลาบอสตาย</h4>\n            <form id="bossForm" class="row g-3" autocomplete="off">\n                <div class="col-md-3">\n                    <label class="form-label" data-i18n="labelBoss">เลือก หรือ พิมพ์ชื่อบอส</label>\n                    <input list="bossOptions" id="bossSelect" class="form-control" placeholder="พิมพ์เพื่อค้นหา หรือคลิกเลือก..." data-i18n-ph="phBoss" required autocomplete="off">\n                    <datalist id="bossOptions"></datalist>\n                </div>\n                <div class="col-md-3">\n                    <label class="form-label" data-i18n="labelKillDate">วันที่(วัน/เดือน/ปี)</label>\n                    <input type="text" id="killDate" class="form-control" placeholder="เช่น 08/09/2026" data-i18n-ph="phKillDate" maxlength="10" autocomplete="off" inputmode="numeric">\n                    <small class="text-white-50" data-i18n="hintKillDate">*เว้นว่างไว้หากใช้วันที่ปัจจุบัน</small>\n                </div>\n                <div class="col-md-3">\n                    <label class="form-label" data-i18n="labelKillTime">เวลาที่ตาย (ระบบ 24 ชม.)</label>\n                    <input type="text" id="killTime" class="form-control" placeholder="เช่น 17:30 หรือ 1730" data-i18n-ph="phKillTime" maxlength="5" autocomplete="off" enterkeyhint="done">\n                    <small class="text-white-50" data-i18n="hintKillTime">*เว้นว่างไว้หากใช้เวลาปัจจุบัน</small>\n                </div>\n                <div class="col-md-2">\n                    <label class="form-label" data-i18n="labelSpTime">เพิ่มเวลาพิเศษ (นาที)</label>\n                    <input type="number" id="spTime" class="form-control" min="0" placeholder="0">\n                </div>\n                <div class="col-md-2">\n                    <label class="form-label" data-i18n="labelNotice">แจ้งเตือนล่วงหน้า (นาที)</label>\n                    <input type="number" id="noticeMinutes" class="form-control" min="1" value="5">\n                </div>\n                <div class="col-md-2 d-flex align-items-end">\n                    <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnSave">⚔️ บันทึกเวลา</button>\n                </div>\n            </form>\n        </div>\n\n        <div class="card p-4 shadow-sm">\n            <div class="d-flex justify-content-between align-items-center mb-3">\n                <h4 class="card-title text-info mb-0" data-i18n="tableTitle">📜 ตารางเวลาบอสล่าสุด</h4>\n                <button id="clearAllBtn" class="btn btn-outline-danger btn-sm" data-i18n="btnClear">ล้างตารางทั้งหมด</button>\n            </div>\n            \n            <div class="table-responsive">\n                <table class="table table-dark table-hover align-middle mb-0">\n                    <thead>\n                        <tr>\n                            <th data-i18n="thBoss">ชื่อบอส</th>\n                            <th>วันที่ตาย</th>\n                            <th data-i18n="thKillTime">เวลาตาย (24 ชม.)</th>\n                            <th data-i18n="thSpawnTime">เวลาเกิด (24 ชม.)</th>\n                            <th data-i18n="thCountdown">นับถอยหลัง</th>\n                            <th data-i18n="thNotice">เตือนล่วงหน้า</th>\n                            <th data-i18n="thRecordedBy">ผู้บันทึก</th>\n                            <th data-i18n="thAction">จัดการ</th>\n                        </tr>\n                    </thead>\n                    <tbody id="bossTableBody"></tbody>\n                </table>\n            </div>\n        </div>\n\n        <!-- ⚔️ Boss Raid Attendance -->\n        <div class="card p-4 mb-4 shadow-sm" id="attendanceDashboardSection">\n            <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">\n                <div>\n                    <h4 class="card-title text-warning mb-1" data-i18n="attendanceTitle">⚔️ Boss Raid Attendance</h4>\n                    <small class="text-white-50" data-i18n="attendanceSubtitle">เช็คชื่อกิจกรรมโจมตีบอสแบบ Real-time จาก Discord</small>\n                </div>\n                <div class="d-flex align-items-center gap-2">\n                    <span class="badge bg-success status-badge" id="attendanceRealtimeStatus" data-i18n="attendanceRealtime">🟢 Realtime</span>\n                    <button id="attendanceCollapseBtn" class="btn btn-outline-secondary btn-sm" onclick="toggleAttendancePanel()">▼ เปิด</button>\n                </div>\n            </div>\n            <div id="attendancePanelContent" style="display:none;">\n            <div class="row g-3 mb-3">\n                <div class="col-md-4">\n                    <div class="card p-3 attendance-stat h-100">\n                        <div class="text-info small" data-i18n="attendanceTotalRaids">กิจกรรมทั้งหมด</div>\n                        <div class="fs-3 fw-bold" id="attendanceTotalRaidsCount" class="attendance-stat-value fs-3 fw-bold">0</div>\n                    </div>\n                </div>\n                <div class="col-md-4">\n                    <div class="card p-3 attendance-stat h-100">\n                        <div class="text-success small" data-i18n="attendanceUniqueMembers">สมาชิกที่เข้าร่วม</div>\n                        <div class="fs-3 fw-bold" id="attendanceUniqueMembersCount" class="attendance-stat-value fs-3 fw-bold">0</div>\n                    </div>\n                </div>\n                <div class="col-md-4">\n                    <div class="card p-3 attendance-stat h-100">\n                        <div class="text-warning small" data-i18n="attendanceTotalCheckins">เช็คชื่อรวม</div>\n                        <div class="fs-3 fw-bold" id="attendanceTotalCheckinsCount" class="attendance-stat-value fs-3 fw-bold">0</div>\n                    </div>\n                </div>\n            </div>\n\n            <h5 class="text-info mt-3" data-i18n="attendanceCurrent">กิจกรรมปัจจุบัน / ที่กำลังจะเริ่ม</h5>\n            <div class="table-responsive mb-4">\n                <table class="table table-dark table-hover align-middle attendance-table mb-0">\n                    <thead><tr>\n                        <th data-i18n="attendanceBoss">บอส</th>\n                        <th data-i18n="attendanceDate">วันที่</th>\n                        <th data-i18n="attendanceAttackTime">เวลาโจมตี</th>\n                        <th data-i18n="attendanceOpenClose">เปิด–ปิด</th>\n                        <th data-i18n="attendanceCount">ผู้เข้าร่วม</th>\n                        <th data-i18n="attendanceStatus">สถานะ</th>\n                    </tr></thead>\n                    <tbody id="attendanceCurrentBody"><tr><td colspan="6" class="text-center text-muted">-</td></tr></tbody>\n                </table>\n            </div>\n\n            <div class="d-flex align-items-center justify-content-between gap-2 mt-3 mb-2">\n                <h5 class="text-info mb-0" data-i18n="attendanceHistory">ประวัติย้อนหลัง</h5>\n                <button id="attendanceHistoryCollapseBtn" type="button" class="btn btn-outline-secondary btn-sm" onclick="toggleAttendanceHistoryPanel()">▼ เปิด</button>\n            </div>\n            <div id="attendanceHistoryPanelContent" style="display:none;">\n            <div class="table-responsive mb-4">\n                <table class="table table-dark table-hover align-middle attendance-table mb-0">\n                    <thead><tr>\n                        <th data-i18n="attendanceBoss">บอส</th>\n                        <th data-i18n="attendanceDate">วันที่</th>\n                        <th data-i18n="attendanceAttackTime">เวลาโจมตี</th>\n                        <th data-i18n="attendanceCount">ผู้เข้าร่วม</th>\n                        <th data-i18n="attendanceCreatedBy">สร้างโดย</th>\n                        <th data-i18n="attendanceStatus">สถานะ</th>\n                    </tr></thead>\n                    <tbody id="attendanceHistoryBody"><tr><td colspan="6" class="text-center text-muted">-</td></tr></tbody>\n                </table>\n            </div>\n\n            </div>\n\n            <h5 class="text-warning" data-i18n="attendanceMonthly">📊 รายงานประจำเดือน</h5>\n            <div class="table-responsive">\n                <table class="table table-dark table-hover align-middle attendance-table mb-0">\n                    <thead><tr>\n                        <th data-i18n="attendanceMonth">เดือน</th>\n                        <th data-i18n="attendanceRaids">กิจกรรม</th>\n                        <th data-i18n="attendanceMembers">สมาชิก</th>\n                        <th data-i18n="attendanceChecks">เช็คชื่อ</th>\n                    </tr></thead>\n                    <tbody id="attendanceMonthlyBody"><tr><td colspan="4" class="text-center text-muted">-</td></tr></tbody>\n                </table>\n            </div>\n            <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mt-4">\n                <div class="d-flex align-items-center gap-2">\n                    <h5 class="text-warning mb-0" data-i18n="attendanceMemberMonthly">👤 สรุปรายสมาชิกสะสมรายเดือน</h5>\n                    <button id="attendanceMemberMonthlyCollapseBtn" type="button" class="btn btn-outline-secondary btn-sm" onclick="toggleAttendanceMemberMonthlyPanel()">▼ เปิด</button>\n                </div>\n                <select id="attendanceMemberMonthSelect" class="form-select form-select-sm bg-dark text-light border-secondary" style="max-width:220px;">\n                    <option value="all" data-i18n="attendanceAllMonths">ทุกเดือน</option>\n                </select>\n            </div>\n            <div id="attendanceMemberMonthlyPanelContent" style="display:none;">\n            <div class="table-responsive mt-2">\n                <table class="table table-dark table-hover align-middle attendance-table mb-0">\n                    <thead><tr>\n                        <th>#</th>\n                        <th data-i18n="attendanceMemberName">สมาชิก</th>\n                        <th data-i18n="attendanceMemberCount">จำนวนเข้าร่วม</th>\n                        <th data-i18n="attendanceMemberRaids">จำนวนกิจกรรม</th>\n                        <th data-i18n="attendanceLastCheckin">เช็คชื่อล่าสุด</th>\n                    </tr></thead>\n                    <tbody id="attendanceMemberMonthlyBody"><tr><td colspan="5" class="text-center text-muted">-</td></tr></tbody>\n                </table>\n            </div>\n            </div>\n\n            <!-- 👤 Individual Attendance Detail -->\n            <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mt-4 mb-2">\n                <div class="d-flex align-items-center gap-2">\n                    <h5 class="text-info mb-0" data-i18n="attendanceIndividual">👤 ข้อมูลการเข้าร่วมรายบุคคล</h5>\n                    <button id="attendanceIndividualCollapseBtn" type="button" class="btn btn-outline-secondary btn-sm" onclick="toggleAttendanceIndividualPanel()">▼ เปิด</button>\n                </div>\n                <div class="d-flex flex-wrap gap-2">\n                    <select id="attendanceIndividualMemberSelect" class="form-select form-select-sm bg-dark text-light border-secondary" style="min-width:220px;">\n                        <option value="" data-i18n="attendanceSelectMember">เลือกสมาชิก</option>\n                    </select>\n                    <select id="attendanceIndividualMonthSelect" class="form-select form-select-sm bg-dark text-light border-secondary" style="min-width:160px;">\n                        <option value="all" data-i18n="attendanceAllMonths">ทุกเดือน</option>\n                    </select>\n                </div>\n            </div>\n            <div id="attendanceIndividualPanelContent" style="display:none;">\n                <div class="row g-3 mb-3">\n                    <div class="col-md-3"><div class="card p-3 attendance-stat h-100"><div class="text-info small" data-i18n="attendanceIndividualName">สมาชิก</div><div class="fw-bold text-white" id="attendanceIndividualNameValue">-</div></div></div>\n                    <div class="col-md-3"><div class="card p-3 attendance-stat h-100"><div class="text-info small" data-i18n="attendanceIndividualCheckins">เช็คชื่อ</div><div class="attendance-stat-value fs-4 fw-bold" id="attendanceIndividualCheckinsValue">0</div></div></div>\n                    <div class="col-md-3"><div class="card p-3 attendance-stat h-100"><div class="text-info small" data-i18n="attendanceIndividualRaids">กิจกรรม</div><div class="attendance-stat-value fs-4 fw-bold" id="attendanceIndividualRaidsValue">0</div></div></div>\n                    <div class="col-md-3"><div class="card p-3 attendance-stat h-100"><div class="text-info small" data-i18n="attendanceIndividualLast">เช็คชื่อล่าสุด</div><div class="fw-bold text-white" id="attendanceIndividualLastValue">-</div></div></div>\n                </div>\n                <div class="table-responsive">\n                    <table class="table table-dark table-hover align-middle attendance-table mb-0">\n                        <thead><tr>\n                            <th data-i18n="attendanceIndividualDate">วันที่</th>\n                            <th data-i18n="attendanceIndividualBoss">บอส</th>\n                            <th data-i18n="attendanceIndividualAttack">เวลาโจมตี</th>\n                            <th data-i18n="attendanceIndividualCheckedAt">เวลาที่เช็คชื่อ</th>\n                            <th data-i18n="attendanceIndividualStatus">สถานะ</th>\n                        </tr></thead>\n                        <tbody id="attendanceIndividualBody"><tr><td colspan="5" class="text-center text-muted">-</td></tr></tbody>\n                    </table>\n                </div>\n            </div>\n            </div>\n        </div>\n\n        <footer class="text-center text-white mt-4">\n            <small data-i18n="footer">ระบบคำนวณเวลานับถอยหลังบอส Real-time • ข้อมูลบันทึกและซิงค์ผ่าน Cloud อัตโนมัติ</small>\n        </footer>\n    </div>\n\n    <!-- Modal เปลี่ยนรหัสผ่าน -->\n    <div class="modal fade" id="changeCodeModal" tabindex="-1" aria-hidden="true">\n        <div class="modal-dialog modal-dialog-centered">\n            <div class="modal-content p-3">\n                <div class="modal-header border-secondary">\n                    <h5 class="modal-title text-warning" data-i18n="modalChangeCodeTitle">🔑 เปลี่ยนโค้ดสำหรับสมัคร</h5>\n                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>\n                </div>\n                <div class="modal-body">\n                    <form id="changeCodeForm">\n                        <div class="mb-3">\n                            <label class="form-label" data-i18n="labelNewRegCode">โค้ดสมัครใหม่</label>\n                            <input type="text" id="newRegCodeInput" class="form-control" placeholder="กรอกโค้ดสำหรับสมัครใหม่" data-i18n-ph="phNewRegCode" required>\n                        </div>\n                        <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnSaveCode">💾 บันทึกโค้ดใหม่</button>\n                    </form>\n                </div>\n            </div>\n        </div>\n    </div>\n\n    <!-- Modal ตั้งค่า Bot Notification -->\n    <div class="modal fade" id="botSettingsModal" tabindex="-1" aria-hidden="true">\n        <div class="modal-dialog modal-dialog-centered">\n            <div class="modal-content p-3">\n                <div class="modal-header border-secondary">\n                    <h5 class="modal-title text-info" data-i18n="modalBotSettingsTitle">⚙️ ตั้งค่าแจ้งเตือนบอท</h5>\n                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>\n                </div>\n                <div class="modal-body">\n                    <h6 class="text-warning mb-3" data-i18n="labelVoiceLang">🔊 ภาษาที่ใช้พูดแจ้งเตือน (Voice Notification)</h6>\n                    <div class="form-check form-switch mb-2">\n                        <input class="form-check-input" type="checkbox" id="ttsThToggle">\n                        <label class="form-check-label" for="ttsThToggle">🇹🇭 ภาษาไทย (TH)</label>\n                    </div>\n                    <div class="form-check form-switch mb-2">\n                        <input class="form-check-input" type="checkbox" id="ttsEnToggle">\n                        <label class="form-check-label" for="ttsEnToggle">🇺🇸 ภาษาอังกฤษ (EN)</label>\n                    </div>\n                    <div class="form-check form-switch mb-3">\n                        <input class="form-check-input" type="checkbox" id="ttsKoToggle">\n                        <label class="form-check-label" for="ttsKoToggle">🇰🇷 ภาษาเกาหลี (KO)</label>\n                    </div>\n                    <h6 class="text-info mb-3" data-i18n="labelDiscordLang">💬 ภาษาที่ใช้แจ้งเตือนใน Discord</h6>\n                    <div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="discordThToggle"><label class="form-check-label" for="discordThToggle">🇹🇭 ภาษาไทย (TH)</label></div>\n                    <div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="discordEnToggle"><label class="form-check-label" for="discordEnToggle">🇺🇸 ภาษาอังกฤษ (EN)</label></div>\n                    <div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="discordKoToggle"><label class="form-check-label" for="discordKoToggle">🇰🇷 ภาษาเกาหลี (KO)</label></div>\n                </div>\n            </div>\n        </div>\n    </div>\n\n    <!-- V8: GitHub Pages loads the authoritative Firebase Web config from Render.\n         Render reads FIREBASE_WEB_CONFIG_JSON, including the real apiKey.\n         The script tag is intentionally before Firebase initialization so the config\n         is available before signInWithEmailAndPassword() can ever run. -->\n    <script src="https://bosstimer-ry18.onrender.com/api/firebase-config.js"></script>\n    <script>\n        (function bootstrapSkynetDashboard() {\n        // --- V8: FIREBASE CONFIG FROM RENDER ---\n        // Do NOT hard-code an API key in GitHub Pages. Render is the source of truth.\n        const firebaseConfig = window.SKYNET_FIREBASE_CONFIG || {};\n        const requiredFirebaseFields = [\'apiKey\',\'authDomain\',\'databaseURL\',\'projectId\',\'storageBucket\',\'messagingSenderId\',\'appId\'];\n        const missingFirebaseFields = requiredFirebaseFields.filter(k => !firebaseConfig[k]);\n        if (missingFirebaseFields.length) {\n            console.error(\'[SKYNET V8] Missing Firebase Web config:\', missingFirebaseFields);\n            const bootError = document.getElementById(\'loginDebug\');\n            if (bootError) {\n                bootError.style.display = \'block\';\n                bootError.textContent = \'❌ Firebase Config จาก Render ไม่ครบ: \' + missingFirebaseFields.join(\', \') +\n                    \'\\n\\nตรวจ Render Environment Variable: FIREBASE_WEB_CONFIG_JSON\';\n            }\n            throw new Error(\'FIREBASE_WEB_CONFIG_MISSING:\' + missingFirebaseFields.join(\',\'));\n        }\n\n        if (firebaseConfig.projectId !== \'skynet-3ad44\') {\n            console.error(\'[SKYNET V8] Wrong Firebase project:\', firebaseConfig.projectId);\n            throw new Error(\'FIREBASE_WRONG_PROJECT:\' + firebaseConfig.projectId);\n        }\n\n        // Initialize Firebase only after the authoritative Render config is loaded.\n        firebase.initializeApp(firebaseConfig);\n        const db = firebase.database();\n        const auth = firebase.auth();\n        const bossRef = db.ref(\'boss_schedule\');\n        const usersRef = db.ref(\'users\');\n        const settingsRef = db.ref(\'app_settings\');\n        const botSettingsRef = db.ref(\'bot_settings\'); // เพิ่ม Reference สำหรับการตั้งค่าบอท\n        const sessionsRef = db.ref(\'dashboard_sessions\');\n        const raidAttendanceRef = db.ref(\'raid_attendance\');\n        const monthlyReportsRef = db.ref(\'monthly_reports\');\n\n        // V5: bounded Firebase operations so the Login button can never remain stuck on loading.\n        function withTimeout(promise, ms, label) {\n            let timer;\n            const timeout = new Promise((_, reject) => {\n                timer = setTimeout(() => {\n                    const err = new Error(label || \'TIMEOUT\');\n                    err.code = \'SKYNET_TIMEOUT\';\n                    reject(err);\n                }, ms);\n            });\n            return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));\n        }\n\n        // 🔐 Admin account\n        // บัญชี Admin ต้องสร้างใน Firebase Authentication ก่อน แล้วกำหนด\n        // users/<ADMIN_UID> เป็น role=admin และ status=approved ใน Realtime Database\n        // ไม่มีการสร้าง Admin อัตโนมัติจากหน้าเว็บ เพื่อป้องกันผู้ใช้ทั่วไปยกระดับสิทธิ์\n        const ADMIN_DEFAULT_USERNAME = "admin";\n        const ACTIVE_SESSION_TIMEOUT_MS = 90000;\n\n        // โค้ดสมัครเริ่มต้น\n        let currentRegCode = "1234";\n        settingsRef.child(\'register_code\').on(\'value\', (snap) => {\n            if (snap.exists() && snap.val()) {\n                currentRegCode = snap.val();\n            } else {\n                settingsRef.child(\'register_code\').set("1234");\n            }\n        });\n\n        // ดึงการตั้งค่า Bot Settings ภาษาการแจ้งเตือน\n        botSettingsRef.on(\'value\', (snap) => {\n            const data = snap.val() || {};\n            // Firebase is authoritative. Missing values remain OFF until explicitly enabled.\n            document.getElementById(\'ttsThToggle\').checked = data.tts_th_enabled === true;\n            document.getElementById(\'ttsEnToggle\').checked = data.tts_en_enabled === true;\n            document.getElementById(\'ttsKoToggle\').checked = data.tts_ko_enabled === true;\n            document.getElementById(\'discordThToggle\').checked = data.discord_notify_th_enabled !== false;\n            document.getElementById(\'discordEnToggle\').checked = data.discord_notify_en_enabled !== false;\n            document.getElementById(\'discordKoToggle\').checked = data.discord_notify_ko_enabled !== false;\n        });\n\n        // ตรวจจับเมื่อมีการกดเปลี่ยนสวิตช์ภาษา\n        [\'ttsThToggle\', \'ttsEnToggle\', \'ttsKoToggle\', \'discordThToggle\', \'discordEnToggle\', \'discordKoToggle\'].forEach(id => {\n            document.getElementById(id).addEventListener(\'change\', async (e) => {\n                if (!await requireApprovedUser()) {\n                    e.target.checked = !e.target.checked;\n                    return;\n                }\n                const keyMap = {\n                    ttsThToggle: \'tts_th_enabled\', ttsEnToggle: \'tts_en_enabled\', ttsKoToggle: \'tts_ko_enabled\',\n                    discordThToggle: \'discord_notify_th_enabled\', discordEnToggle: \'discord_notify_en_enabled\', discordKoToggle: \'discord_notify_ko_enabled\'\n                };\n                await botSettingsRef.child(keyMap[id]).set(e.target.checked);\n            });\n        });\n\n        // ฟังก์ชันเปิด Modal ตั้งค่า Bot\n        async function openBotSettingsModal() {\n            if (!await requireApprovedUser()) return;\n            const modalEl = document.getElementById(\'botSettingsModal\');\n            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);\n            modal.show();\n        }\n\n        // --- 2. ฐานข้อมูลภาษา (i18n) ---\n        const TRANSLATIONS = {\n            th: {\n                authTitle: "⚔️ Boss Timer Access",\n                tabLogin: "เข้าสู่ระบบ",\n                tabRegister: "ลงทะเบียน",\n                labelUsername: "ชื่อผู้ใช้",\n                labelPassword: "รหัสผ่าน",\n                labelRegCode: "โค้ดสำหรับสมัคร",\n                rememberMe: "จำชื่อผู้ใช้",\n                btnLogin: "🔑 เข้าสู่ระบบ",\n                btnRegister: "📝 ลงทะเบียน",\n                btnLogout: "🚪 ออกจากระบบ",\n                btnConfigCode: "🔑 เปลี่ยนโค้ดสมัคร",\n                modalChangeCodeTitle: "🔑 เปลี่ยนโค้ดสำหรับสมัคร",\n                labelNewRegCode: "โค้ดสมัครใหม่",\n                btnSaveCode: "💾 บันทึกโค้ดใหม่",\n                btnBotSettings: "⚙️ ตั้งค่าบอท",\n                modalBotSettingsTitle: "⚙️ ตั้งค่าแจ้งเตือนบอท",\n                labelVoiceLang: "🔊 ภาษาที่ใช้พูดแจ้งเตือน (Voice Notification)",\n                labelDiscordLang: "💬 ภาษาที่ใช้แจ้งเตือนใน Discord",\n                phLoginUser: "กรอกชื่อผู้ใช้",\n                phLoginPass: "กรอกรหัสผ่าน",\n                phRegUser: "ตั้งชื่อผู้ใช้",\n                phRegPass: "ตั้งรหัสผ่าน",\n                phRegCode: "กรอกโค้ดสำหรับสมัคร",\n                phNewRegCode: "กรอกโค้ดสำหรับสมัครใหม่",\n                title: "⚔️ Boss Timer Dashboard",\n                online: "🟢 Realtime Sync Active",\n                labelLanguage: "🌐 ภาษา:",\n                labelTimezone: "🌍 เขตเวลา:",\n                tzAuto: "💻 อัตโนมัติ (ตามเครื่อง)",\n                enableNotify: "🔔 เปิดระบบเสียง & แจ้งเตือน",\n                disableNotify: "🔕 ปิดระบบเสียง & แจ้งเตือน",\n                formTitle: "⏱️ บันทึกเวลาบอสตาย",\n                labelBoss: "เลือก หรือ พิมพ์ชื่อบอส",\n                phBoss: "พิมพ์เพื่อค้นหา หรือคลิกเลือก...",\n                labelKillTime: "เวลาที่ตาย (ระบบ 24 ชม.)",\n                labelKillDate: "วันที่(วัน/เดือน/ปี)",\n                phKillDate: "เช่น 08/09/2026",\n                hintKillDate: "*เว้นว่างไว้หากใช้วันที่ปัจจุบัน",\n                labelSpTime: "เพิ่มเวลาพิเศษ (นาที)",\n                phKillTime: "เช่น 17:30 หรือ 1730",\n                hintKillTime: "*เว้นว่างไว้หากใช้เวลาปัจจุบัน",\n                labelNotice: "แจ้งเตือนล่วงหน้า (นาที)",\n                btnSave: "⚔️ บันทึกเวลา",\n                tableTitle: "📜 ตารางเวลาบอสล่าสุด",\n                btnClear: "ล้างตารางทั้งหมด",\n                thBoss: "ชื่อบอส",\n                thKillTime: "เวลาตาย (24 ชม.)",\n                thSpawnTime: "เวลาเกิด (24 ชม.)",\n                thCountdown: "นับถอยหลัง",\n                thNotice: "เตือนล่วงหน้า",\n                thRecordedBy: "ผู้บันทึก",\n                thAction: "จัดการ",\n                emptyMsg: "📌 ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ",\n                spawned: "⚔️ เกิดแล้ว!",\n                btnDelete: "ลบ",\n                hourUnit: "ชม.",\n                minUnit: "นาที",\n                secUnit: "วินาที",\n                footer: "ระบบคำนวณเวลานับถอยหลังบอส Real-time • ข้อมูลบันทึกและซิงค์ผ่าน Cloud อัตโนมัติ",\n                invalidTimeAlert: "กรุณากรอกเวลาให้ถูกต้องตามระบบ 24 ชั่วโมง (เช่น 08:30 หรือ 17:45)",\n                confirmClear: "คุณต้องการล้างตารางบอสทั้งหมดใช่หรือไม่?",\n                notifyReadyTitle: "⚔️ ระบบแจ้งเตือนพร้อมทำงาน",\n                notifyReadyBody: "จะมีการแจ้งเตือนเมื่อบอสใกล้เกิดและเมื่อบอสเกิดแล้ว",\n                noNotifySupport: "เบราว์เซอร์นี้ไม่รองรับการแจ้งเตือนแบบ Pop-up",\n                grantNotifyPrompt: "กรุณากดอนุญาต (Allow) การแจ้งเตือนในเบราว์เซอร์ของคุณ",\n                errRegCodeInvalid: "โค้ดสำหรับสมัครไม่ถูกต้อง!",\n                errUserExists: "ชื่อผู้ใช้นี้ถูกลงทะเบียนไปแล้ว!",\n                regSuccess: "ลงทะเบียนสำเร็จ! กรุณาเข้าสู่ระบบ",\n                errLoginFailed: "เข้าสู่ระบบไม่สำเร็จ",\n                codeChangedSuccess: "เปลี่ยนโค้ดสำหรับสมัครเรียบร้อยแล้ว!",\n                spawnNotifyTitle: "⚔️ {boss} เกิดแล้ว!",\n                spawnNotifyBody: "บอส {boss} ได้เกิดแล้วในขณะนี้",\n                noticeNotifyTitle: "⏳ {boss} ใกล้เกิด!",\n                noticeNotifyBody: "บอส {boss} จะเกิดในอีก {min} นาที",\n                regPending: "ลงทะเบียนสำเร็จ แต่บัญชียังรอ Admin อนุมัติ จึงยังเข้าใช้งานไม่ได้",\n                errPendingApproval: "บัญชีนี้กำลังรอ Admin อนุมัติ",\n                errAccountRejected: "บัญชีนี้ถูกปฏิเสธหรือปิดการใช้งานโดย Admin",\n                errAccountNotApproved: "บัญชีนี้ยังไม่ได้รับอนุมัติจาก Admin",\n                errWeakAccount: "ชื่อผู้ใช้ต้องมีอย่างน้อย 3 ตัวอักษร และรหัสผ่านอย่างน้อย 6 ตัวอักษร",\n                errAdminUsername: "ไม่สามารถใช้ชื่อ admin สำหรับการสมัครทั่วไปได้",\n                attendanceTitle: "⚔️ Boss Raid Attendance",\n                attendanceSubtitle: "เช็คชื่อกิจกรรมโจมตีบอสแบบ Real-time จาก Discord",\n                attendanceRealtime: "🟢 Real-time",\n                attendanceTotalRaids: "กิจกรรมทั้งหมด", attendanceUniqueMembers: "สมาชิกที่เข้าร่วม", attendanceTotalCheckins: "เช็คชื่อรวม",\n                attendanceCurrent: "กิจกรรมปัจจุบัน / ที่กำลังจะเริ่ม", attendanceHistory: "ประวัติย้อนหลัง", attendanceMonthly: "📊 รายงานประจำเดือน", attendanceMemberMonthly: "👤 สรุปรายสมาชิกสะสมรายเดือน", attendanceAllMonths: "ทุกเดือน", attendanceMemberName: "สมาชิก", attendanceMemberCount: "จำนวนเข้าร่วม", attendanceMemberRaids: "จำนวนกิจกรรม", attendanceLastCheckin: "เช็คชื่อล่าสุด", attendanceIndividual: "👤 ข้อมูลการเข้าร่วมรายบุคคล", attendanceSelectMember: "เลือกสมาชิก", attendanceIndividualName: "สมาชิก", attendanceIndividualCheckins: "เช็คชื่อ", attendanceIndividualRaids: "กิจกรรม", attendanceIndividualLast: "เช็คชื่อล่าสุด", attendanceIndividualDate: "วันที่", attendanceIndividualBoss: "บอส", attendanceIndividualAttack: "เวลาโจมตี", attendanceIndividualCheckedAt: "เวลาที่เช็คชื่อ", attendanceIndividualStatus: "สถานะ",\n                attendanceBoss: "บอส", attendanceDate: "วันที่", attendanceAttackTime: "เวลาโจมตี", attendanceOpenClose: "เปิด–ปิด",\n                attendanceCount: "ผู้เข้าร่วม", attendanceCreatedBy: "สร้างโดย", attendanceStatus: "สถานะ", attendanceMonth: "เดือน",\n                attendanceRaids: "กิจกรรม", attendanceMembers: "สมาชิก", attendanceChecks: "เช็คชื่อ", attendanceCollapseOpen: "▼ เปิด", attendanceCollapseClose: "▲ ปิด",\n                adminPanelTitle: "🛡️ Admin • จัดการผู้ใช้งาน",\n                adminPanelSubtitle: "อนุมัติผู้สมัคร ตรวจสอบประวัติ และดูผู้ที่กำลังใช้งาน • ปุ่มอนุมัติอยู่ในตารางด้านล่าง",\n                adminRefresh: "🔄 รีเฟรช",\n                adminPending: "รออนุมัติ",\n                adminActive: "กำลังใช้งาน",\n                adminTotal: "ผู้ใช้ทั้งหมด",\n                adminThUser: "ผู้ใช้",\n                adminThStatus: "สถานะ",\n                adminThCreated: "สมัครเมื่อ",\n                adminThLastLogin: "เข้าสู่ระบบล่าสุด",\n                adminThLastSeen: "ใช้งานล่าสุด",\n                adminThLogins: "จำนวนครั้ง",\n                adminThAction: "จัดการ",\n                adminApprove: "อนุมัติ",\n                adminReject: "ปฏิเสธ",\n                adminDisable: "ปิดใช้งาน",\n                adminActivate: "เปิดใช้งาน",\n                adminNoUsers: "ยังไม่มีผู้ใช้งาน",\n                adminOnly: "คำสั่งนี้อนุญาตเฉพาะ Admin",\n                adminCannotChangeAdmin: "ไม่สามารถเปลี่ยนสถานะบัญชี Admin ได้"\n            },\n            en: {\n                authTitle: "⚔️ Boss Timer Access",\n                tabLogin: "Login",\n                tabRegister: "Register",\n                labelUsername: "Username",\n                labelPassword: "Password",\n                labelRegCode: "Registration Code",\n                rememberMe: "Remember Username",\n                btnLogin: "🔑 Login",\n                btnRegister: "📝 Register",\n                btnLogout: "🚪 Logout",\n                btnConfigCode: "🔑 Change Reg Code",\n                modalChangeCodeTitle: "🔑 Change Registration Code",\n                labelNewRegCode: "New Registration Code",\n                btnSaveCode: "💾 Save New Code",\n                btnBotSettings: "⚙️ Bot Settings",\n                modalBotSettingsTitle: "⚙️ Bot Notification Settings",\n                labelVoiceLang: "🔊 Voice Notification Languages",\n                labelDiscordLang: "💬 Discord Notification Languages",\n                phLoginUser: "Enter username",\n                phLoginPass: "Enter password",\n                phRegUser: "Set username",\n                phRegPass: "Set password",\n                phRegCode: "Enter registration code",\n                phNewRegCode: "Enter new registration code",\n                title: "⚔️ Boss Timer Dashboard",\n                online: "🟢 Realtime Sync Active",\n                labelLanguage: "🌐 Language:",\n                labelTimezone: "🌍 Timezone:",\n                tzAuto: "💻 Auto (Device Local)",\n                enableNotify: "🔔 Enable Sound & Alerts",\n                disableNotify: "🔕 Disable Sound & Alerts",\n                formTitle: "⏱️ Record Boss Kill Time",\n                labelBoss: "Select or Type Boss Name",\n                phBoss: "Type to search or select...",\n                labelKillTime: "Kill Time (24h Format)",\n                labelKillDate: "Date (Day/Month/Year)",\n                phKillDate: "e.g. 08/09/2026",\n                hintKillDate: "*Leave blank to use today\'s date",\n                labelSpTime: "Special Time (mins)",\n                phKillTime: "e.g. 17:30 or 1730",\n                hintKillTime: "*Leave blank to use current time",\n                labelNotice: "Advance Notice (Mins)",\n                btnSave: "⚔️ Save Time",\n                tableTitle: "📜 Boss Schedule Table",\n                btnClear: "Clear All Data",\n                thBoss: "Boss Name",\n                thKillTime: "Kill Time (24h)",\n                thSpawnTime: "Spawn Time (24h)",\n                thCountdown: "Countdown",\n                thNotice: "Notice",\n                thRecordedBy: "Recorded By",\n                thAction: "Action",\n                emptyMsg: "📌 No boss times recorded in the system",\n                spawned: "⚔️ Spawned!",\n                btnDelete: "Delete",\n                hourUnit: "h",\n                minUnit: "m",\n                secUnit: "s",\n                footer: "Real-time Boss Countdown System • Synced with Cloud Database",\n                invalidTimeAlert: "Please enter time in valid 24-hour format (e.g. 08:30 or 17:45)",\n                confirmClear: "Are you sure you want to clear all boss timers?",\n                notifyReadyTitle: "⚔️ Notifications Active",\n                notifyReadyBody: "You will be alerted before boss spawns and when spawned.",\n                noNotifySupport: "This browser does not support Pop-up notifications.",\n                grantNotifyPrompt: "Please grant notification permissions in your browser.",\n                errRegCodeInvalid: "Invalid Registration Code!",\n                errUserExists: "Username already exists!",\n                regSuccess: "Registration successful! Please login.",\n                errLoginFailed: "Login failed",\n                codeChangedSuccess: "Registration code updated successfully!",\n                spawnNotifyTitle: "⚔️ {boss} Spawned!",\n                spawnNotifyBody: "Boss {boss} has spawned!",\n                noticeNotifyTitle: "⏳ {boss} Spawning Soon!",\n                noticeNotifyBody: "Boss {boss} will spawn in {min} minutes",\n                regPending: "Registration submitted. Your account is pending Admin approval.",\n                errPendingApproval: "This account is waiting for Admin approval.",\n                errAccountRejected: "This account was rejected or disabled by Admin.",\n                errAccountNotApproved: "This account has not been approved by Admin.",\n                errWeakAccount: "Username must be at least 3 characters and password at least 6 characters.",\n                errAdminUsername: "The admin username is reserved.",\n                attendanceTitle: "⚔️ Boss Raid Attendance",\n                attendanceSubtitle: "Real-time boss raid check-in from Discord",\n                attendanceRealtime: "🟢 Realtime",\n                attendanceTotalRaids: "Total Raids", attendanceUniqueMembers: "Unique Members", attendanceTotalCheckins: "Total Check-ins",\n                attendanceCurrent: "Current / Upcoming Activities", attendanceHistory: "Attendance History", attendanceMonthly: "📊 Monthly Reports", attendanceMemberMonthly: "👤 Monthly Member Totals", attendanceAllMonths: "All months", attendanceMemberName: "Member", attendanceMemberCount: "Check-ins", attendanceMemberRaids: "Raids", attendanceLastCheckin: "Last Check-in", attendanceIndividual: "👤 ข้อมูลการเข้าร่วมรายบุคคล", attendanceSelectMember: "เลือกสมาชิก", attendanceIndividualName: "สมาชิก", attendanceIndividualCheckins: "เช็คชื่อ", attendanceIndividualRaids: "กิจกรรม", attendanceIndividualLast: "เช็คชื่อล่าสุด", attendanceIndividualDate: "วันที่", attendanceIndividualBoss: "บอส", attendanceIndividualAttack: "เวลาโจมตี", attendanceIndividualCheckedAt: "เวลาที่เช็คชื่อ", attendanceIndividualStatus: "สถานะ",\n                attendanceBoss: "Boss", attendanceDate: "Date", attendanceAttackTime: "Attack Time", attendanceOpenClose: "Open–Close",\n                attendanceCount: "Participants", attendanceCreatedBy: "Created By", attendanceStatus: "Status", attendanceMonth: "Month",\n                attendanceRaids: "Raids", attendanceMembers: "Members", attendanceChecks: "Check-ins", attendanceCollapseOpen: "▼ Open", attendanceCollapseClose: "▲ Close",\n                adminPanelTitle: "🛡️ Admin • User Management",\n                adminPanelSubtitle: "Approve registrations, review history, and see active users. Approval buttons are in the table below.",\n                adminRefresh: "🔄 Refresh",\n                adminPending: "Pending",\n                adminActive: "Active Now",\n                adminTotal: "Total Users",\n                adminThUser: "User",\n                adminThStatus: "Status",\n                adminThCreated: "Registered",\n                adminThLastLogin: "Last Login",\n                adminThLastSeen: "Last Seen",\n                adminThLogins: "Logins",\n                adminThAction: "Action",\n                adminApprove: "Approve",\n                adminReject: "Reject",\n                adminDisable: "Disable",\n                adminActivate: "Activate",\n                adminNoUsers: "No users found.",\n                adminOnly: "Admin only.",\n                adminCannotChangeAdmin: "The Admin account cannot be changed."\n            },\n            ko: {\n                authTitle: "⚔️ Boss Timer Access",\n                tabLogin: "로그인",\n                tabRegister: "회원가입",\n                labelUsername: "사용자 이름",\n                labelPassword: "비밀번호",\n                labelRegCode: "가입 코드",\n                rememberMe: "사용자 이름 저장",\n                btnLogin: "🔑 로그인",\n                btnRegister: "📝 회원가입",\n                btnLogout: "🚪 로그아웃",\n                btnConfigCode: "🔑 가입 코드 변경",\n                modalChangeCodeTitle: "🔑 가입 코드 변경",\n                labelNewRegCode: "새 가입 코드",\n                btnSaveCode: "💾 새 코드 저장",\n                btnBotSettings: "⚙️ 봇 설정",\n                modalBotSettingsTitle: "⚙️ 봇 알림 설정",\n                labelVoiceLang: "🔊 음성 알림 언어",\n                labelDiscordLang: "💬 Discord 알림 언어",\n                phLoginUser: "사용자 이름을 입력하세요",\n                phLoginPass: "비밀번호를 입력하세요",\n                phRegUser: "사용자 이름 설정",\n                phRegPass: "비밀번호 설정",\n                phRegCode: "가입 코드를 입력하세요",\n                phNewRegCode: "새 가입 코드를 입력하세요",\n                title: "⚔️ Boss Timer Dashboard",\n                online: "🟢 실시간 동기화 활성화",\n                labelLanguage: "🌐 언어:",\n                labelTimezone: "🌍 시간대:",\n                tzAuto: "💻 자동 (기기 설정)",\n                enableNotify: "🔔 소리 및 알림 켜기",\n                disableNotify: "🔕 소리 및 알림 끄기",\n                formTitle: "⏱️ 보스 처치 시간 기록",\n                labelBoss: "보스 선택 또는 입력",\n                phBoss: "검색 또는 선택...",\n                labelKillTime: "처치 시간 (24시간 형식)",\n                labelKillDate: "날짜 (일/월/년)",\n                phKillDate: "예: 08/09/2026",\n                hintKillDate: "*오늘 날짜를 사용하려면 비워두세요",\n                labelSpTime: "추가 시간 (분)",\n                phKillTime: "예: 17:30 또는 1730",\n                hintKillTime: "*현재 시간을 사용하려면 비워두세요",\n                labelNotice: "사전 알림 (분)",\n                btnSave: "⚔️ 시간 저장",\n                tableTitle: "📜 최근 보스 시간표",\n                btnClear: "전체 목록 삭제",\n                thBoss: "보스 이름",\n                thKillTime: "처치 시간 (24h)",\n                thSpawnTime: "젠 시간 (24h)",\n                thCountdown: "카운트다운",\n                thNotice: "사전 알림",\n                thRecordedBy: "기록자",\n                thAction: "관리",\n                emptyMsg: "📌 시스템에 기록된 보스 시간이 없습니다",\n                spawned: "⚔️ 젠 완료!",\n                btnDelete: "삭제",\n                hourUnit: "시간",\n                minUnit: "분",\n                secUnit: "초",\n                footer: "실시간 보스 카운트다운 시스템 • 클라우드 자동 동기화",\n                invalidTimeAlert: "24시간 형식에 맞게 올바른 시간을 입력해주세요. (예: 08:30 หรือ 17:45)",\n                confirmClear: "모든 보스 타이머를 삭제하시겠습니까?",\n                notifyReadyTitle: "⚔️ 알림 시스템 준비 완료",\n                notifyReadyBody: "보스 젠 임박 및 젠 완료 시 알림이 전송됩니다.",\n                noNotifySupport: "이 브라우저는 팝업 알림을 지원하지 않습니다.",\n                grantNotifyPrompt: "브라우저에서 알림 권한을 허용해주세요.",\n                errRegCodeInvalid: "가입 코드가 올바르지 않습니다!",\n                errUserExists: "이미 존재하는 사용자 이름입니다!",\n                regSuccess: "회원가입 성공! 로그인해주세요.",\n                errLoginFailed: "로그인에 실패했습니다",\n                codeChangedSuccess: "가입 코드가 성공적으로 변경되었습니다!",\n                spawnNotifyTitle: "⚔️ {boss} 젠 완료!",\n                spawnNotifyBody: "보스 {boss}(이)가 지금 젠되었습니다.",\n                noticeNotifyTitle: "⏳ {boss} 젠 임박!",\n                noticeNotifyBody: "보스 {boss}(이)가 {min}분 후에 젠됩니다.",\n                regPending: "회원가입이 완료되었습니다. Admin 승인을 기다려 주세요.",\n                errPendingApproval: "이 계정은 Admin 승인을 기다리고 있습니다.",\n                errAccountRejected: "이 계정은 Admin에 의해 거부되었거나 비활성화되었습니다.",\n                errAccountNotApproved: "이 계정은 아직 Admin의 승인을 받지 못했습니다.",\n                errWeakAccount: "사용자 이름은 3자 이상, 비밀번호는 6자 이상이어야 합니다.",\n                errAdminUsername: "admin 사용자 이름은 일반 가입에 사용할 수 없습니다.",\n                attendanceTitle: "⚔️ 보스 레이드 출석",\n                attendanceSubtitle: "Discord에서 실시간 보스 레이드 출석 확인",\n                attendanceRealtime: "🟢 실시간",\n                attendanceTotalRaids: "전체 활동", attendanceUniqueMembers: "참여 회원", attendanceTotalCheckins: "총 출석",\n                attendanceCurrent: "현재 / 예정 활동", attendanceHistory: "출석 기록", attendanceMonthly: "📊 월간 보고서", attendanceMemberMonthly: "👤 월별 회원 누적", attendanceAllMonths: "전체 월", attendanceMemberName: "회원", attendanceMemberCount: "참여 횟수", attendanceMemberRaids: "활동 수", attendanceLastCheckin: "최근 출석", attendanceIndividual: "👤 ข้อมูลการเข้าร่วมรายบุคคล", attendanceSelectMember: "เลือกสมาชิก", attendanceIndividualName: "สมาชิก", attendanceIndividualCheckins: "เช็คชื่อ", attendanceIndividualRaids: "กิจกรรม", attendanceIndividualLast: "เช็คชื่อล่าสุด", attendanceIndividualDate: "วันที่", attendanceIndividualBoss: "บอส", attendanceIndividualAttack: "เวลาโจมตี", attendanceIndividualCheckedAt: "เวลาที่เช็คชื่อ", attendanceIndividualStatus: "สถานะ",\n                attendanceBoss: "보스", attendanceDate: "날짜", attendanceAttackTime: "공격 시간", attendanceOpenClose: "시작–마감",\n                attendanceCount: "참여자", attendanceCreatedBy: "생성자", attendanceStatus: "상태", attendanceMonth: "월",\n                attendanceRaids: "활동", attendanceMembers: "회원", attendanceChecks: "출석", attendanceCollapseOpen: "▼ 열기", attendanceCollapseClose: "▲ 닫기",\n                adminPanelTitle: "🛡️ Admin • 사용자 관리",\n                adminPanelSubtitle: "가입 승인, 이용 기록 및 현재 접속 사용자를 확인합니다.",\n                adminRefresh: "🔄 새로고침",\n                adminPending: "승인 대기",\n                adminActive: "현재 접속",\n                adminTotal: "전체 사용자",\n                adminThUser: "사용자",\n                adminThStatus: "상태",\n                adminThCreated: "가입일",\n                adminThLastLogin: "최근 로그인",\n                adminThLastSeen: "최근 활동",\n                adminThLogins: "로그인 횟수",\n                adminThAction: "관리",\n                adminApprove: "อนุมัติ",\n                adminReject: "ปฏิเสธ",\n                adminDisable: "ปิดใช้งาน",\n                adminActivate: "เปิดใช้งาน",\n                adminNoUsers: "ยังไม่มีผู้ใช้งาน",\n                adminOnly: "คำสั่งนี้อนุญาตเฉพาะ Admin",\n                adminCannotChangeAdmin: "ไม่สามารถเปลี่ยนสถานะบัญชี Admin ได้"\n            }\n        };\n\n        let currentLang = localStorage.getItem(\'app_lang\') || \'th\';\n        let currentTz = localStorage.getItem(\'app_tz\') || \'auto\';\n        let isNotifyEnabled = localStorage.getItem(\'notify_enabled\') === \'true\';\n        let activeBosses = {};\n\n        const BOSS_DATABASE = {\n            "Wadangka": { cd: 9000, notice: 30 },\n            "Elemental Queen": { cd: 9000, notice: 5 },\n            "Tank": { cd: 3500, notice: 5 },\n            "Swirl Flame": { cd: 3500, notice: 5 },\n            "Maelstrom": { cd: 3500, notice: 5 },\n            "Twister": { cd: 3500, notice: 5 },\n            "Bigmama": { cd: 172800, notice: 30 },\n            "Chief Magief": { cd: 1800, notice: 5 },\n            "Faith": { cd: 21180, notice: 30 },\n            "Apapa": { cd: 900, notice: 5 },\n            "Corrupt Forest Keeper": { cd: 3480, notice: 5 },\n            "Recluse": { cd: 40980, notice: 30 },\n            "Blackskull": { cd: 3410, notice: 5 },\n            "Sleepy Kooii": { cd: 1200, notice: 5 },\n            "Awaken Kooii": { cd: 3780, notice: 5 },\n            "Eeheehee": { cd: 4008, notice: 5 },\n            "Ooheeheek": { cd: 4083, notice: 5 },\n            "Oohehe": { cd: 3908, notice: 5 },\n            "Guardian Imp": { cd: 3780, notice: 5 },\n            "Devilang": { cd: 19980, notice: 30 },\n            "Blackjuno": { cd: 2100, notice: 5 },\n            "Blacksky": { cd: 2100, notice: 5 },\n            "Red Fox": { cd: 1200, notice: 5 },\n            "7tailfox": { cd: 1200, notice: 5 },\n            "777Tailfox": { cd: 1800, notice: 5 },\n            "Sunrise Flower": { cd: 1200, notice: 5 },\n            "Magma Senior Thief": { cd: 1200, notice: 5 },\n            "Bbinikjoe": { cd: 1200, notice: 5 },\n            "Bigmouse": { cd: 1200, notice: 5 },\n            "Caligo": { cd: 604800, notice: 60 },\n            "Poison Root Flower": { cd: 1690, notice: 5 },\n            "Contaminated Queen Bee": { cd: 1680, notice: 5 },\n            "Rotten Pudding": { cd: 1800, notice: 5 },\n            "Swamp Flower Monster": { cd: 1800, notice: 5 },\n            "Ukpana": { cd: 172800, notice: 30 },\n            "Darlene the Witch": { cd: 259200, notice: 30 },\n            "Illust": { cd: 259200, notice: 30 },\n            "Actaemon": { cd: 21600, notice: 30 },\n            "Aiyo\'s Protector": { cd: 259200, notice: 30 },\n            "Glucose": { cd: 1800, notice: 5 },\n            "Overload": { cd: 1792, notice: 5 },\n            "Soul Lich": { cd: 87300, notice: 30 },\n            "Platanista": { cd: 604800, notice: 60 },\n            "Barslaf": { cd: 172800, notice: 30 },\n            "Billiard": { cd: 28503, notice: 5 },\n            "Shaaack": { cd: 1800, notice: 5 },\n            "Suuuk": { cd: 1200, notice: 5 },\n            "Sususuk": { cd: 1200, notice: 5 },\n            "sandgrave": { cd: 1200, notice: 5 },\n            "Elder Beholder": { cd: 1200, notice: 5 }\n        };\n\n        // --- 3. ระบบ Authentication & Session (Firebase Authentication) ---\n        const AUTH_EMAIL_DOMAIN = "@skynet-3ad44.firebaseapp.com";\n        // Real Firebase Authentication email for the existing Admin account\n        // UID: cplvow7Vr6TAd62hREJYuX2w5e73\n        const ADMIN_AUTH_EMAIL = "m4ge999@gmail.com";\n        settingsRef.child(\'register_code\').on(\'value\', (snap) => {\n            if (snap.exists() && snap.val()) currentRegCode = snap.val();\n            else settingsRef.child(\'register_code\').set("1234");\n        });\n\n        function makeUserKey(username) {\n            return username.toLowerCase().replace(/[.#$\\[\\]\\/]/g, \'_\');\n        }\n\n        function usernameToAuthEmail(username) {\n            const key = makeUserKey(username);\n            if (key === makeUserKey(ADMIN_DEFAULT_USERNAME)) return ADMIN_AUTH_EMAIL;\n            return `${key}${AUTH_EMAIL_DOMAIN}`;\n        }\n\n        function formatAdminDate(value) {\n            if (!value) return \'-\';\n            const d = new Date(value);\n            if (isNaN(d.getTime())) return \'-\';\n            return d.toLocaleString(\'th-TH\', { year:\'numeric\', month:\'2-digit\', day:\'2-digit\', hour:\'2-digit\', minute:\'2-digit\', second:\'2-digit\' });\n        }\n\n        let currentSessionId = null;\n        let currentUserKey = null;\n        let currentUserData = null;\n        let heartbeatTimer = null;\n        let adminUsersListenerAttached = false;\n        let adminSessionsListenerAttached = false;\n        let adminRefreshTimer = null;\n\n        async function createUserSession(userKey, userData) {\n            currentUserKey = userKey;\n            currentUserData = userData;\n            const now = new Date().toISOString();\n            // Use a Firebase push id instead of crypto.randomUUID so every browser is supported.\n            const sessionRef = sessionsRef.push();\n            currentSessionId = sessionRef.key;\n            const sessionData = {\n                userKey, username:userData.username || \'\', loginAt:now, lastSeenAt:now, active:true\n            };\n            await withTimeout(sessionRef.set(sessionData), 8000, \'SESSION_WRITE_TIMEOUT\');\n            // Let Firebase mark this session inactive if the browser disconnects unexpectedly.\n            try {\n                sessionRef.onDisconnect().update({\n                    lastSeenAt: new Date().toISOString(), active:false, logoutAt:new Date().toISOString()\n                });\n            } catch (e) { console.warn(\'[SKYNET] onDisconnect setup failed:\', e); }\n            await withTimeout(usersRef.child(userKey).update({\n                lastLoginAt:now, lastSeenAt:now, lastLogoutAt:null, online:true,\n                loginCount:(Number(userData.loginCount)||0)+1\n            }), 8000, \'USER_WRITE_TIMEOUT\');\n            localStorage.setItem(\'logged_user\', userData.username || \'\');\n            localStorage.setItem(\'logged_user_key\', userKey);\n            localStorage.setItem(\'logged_session_id\', currentSessionId);\n            localStorage.setItem(\'logged_role\', userData.role || \'user\');\n            startSessionHeartbeat();\n        }\n\n        function startSessionHeartbeat() {\n            if (heartbeatTimer) clearInterval(heartbeatTimer);\n            heartbeatTimer = setInterval(async () => {\n                const sessionId=localStorage.getItem(\'logged_session_id\');\n                const userKey=localStorage.getItem(\'logged_user_key\');\n                if (!sessionId || !userKey || !auth.currentUser) return;\n                const now=new Date().toISOString();\n                try {\n                    await sessionsRef.child(sessionId).update({lastSeenAt:now,active:true});\n                    await usersRef.child(userKey).update({lastSeenAt:now,online:true});\n                } catch(err) { console.warn(\'Session heartbeat error:\',err); }\n            },30000);\n        }\n\n        async function endUserSession(signOutAuth=true) {\n            const sessionId=localStorage.getItem(\'logged_session_id\');\n            const userKey=localStorage.getItem(\'logged_user_key\');\n            const now=new Date().toISOString();\n            if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer=null; }\n            try {\n                if (sessionId) await sessionsRef.child(sessionId).update({lastSeenAt:now,logoutAt:now,active:false});\n                if (userKey) await usersRef.child(userKey).update({lastSeenAt:now,lastLogoutAt:now,online:false});\n            } catch(err) { console.warn(\'Session logout update error:\',err); }\n            if (signOutAuth) { try { await auth.signOut(); } catch(err) {} }\n            [\'logged_user\',\'logged_user_key\',\'logged_session_id\',\'logged_role\'].forEach(k=>localStorage.removeItem(k));\n            stopAttendanceRealtimeListener();\n            currentSessionId=null; currentUserKey=null; currentUserData=null;\n        }\n\n        async function checkAuthSession(firebaseUserOverride=null) {\n            const savedUser=localStorage.getItem(\'saved_username\');\n            if(savedUser){document.getElementById(\'loginUser\').value=savedUser;document.getElementById(\'rememberMe\').checked=true;}\n\n            const firebaseUser=firebaseUserOverride || auth.currentUser;\n            if(firebaseUser){\n                try{\n                    const snap=await withTimeout(usersRef.child(firebaseUser.uid).once(\'value\'), 10000, \'USER_READ_TIMEOUT\');\n                    if(!snap.exists()) throw new Error(\'USER_NOT_FOUND\');\n                    const userData=snap.val();\n                    if(userData.status!==\'approved\'){\n                        await endUserSession(true);\n                        throw new Error(userData.status===\'pending\'?\'ACCOUNT_PENDING\':\'ACCOUNT_NOT_APPROVED\');\n                    }\n\n                    currentUserData=userData;\n                    currentUserKey=firebaseUser.uid;\n                    document.getElementById(\'authContainer\').style.display=\'none\';\n                    document.getElementById(\'mainDashboard\').style.display=\'block\';\n                    document.getElementById(\'userBadge\').innerText=`👤 ${userData.username || firebaseUser.email}${userData.role===\'admin\'?\' • 🛡️ Admin\':\'\'}`;\n                    startAttendanceRealtimeListener();\n                    const adminPanel=document.getElementById(\'adminPanel\');\n                    const adminOnlyButtons=document.querySelectorAll(\'[data-admin-only]\');\n                    const adminMenu=document.getElementById(\'adminPanelMenuBtn\');\n                    if(userData.role===\'admin\'){\n                        adminPanel.style.display=\'block\';\n                        startAdminRealtimeListeners();\n                        adminOnlyButtons.forEach(el=>el.style.display=\'\');\n                        if(adminMenu) adminMenu.style.display=\'inline-block\';\n                        // Never block login on the admin list. It is non-critical UI data.\n                        loadAdminUsers().catch(err => { console.error(\'[SKYNET] Admin list load failed:\', err); const el=document.getElementById(\'adminDataStatus\'); if(el){el.className=\'small text-danger mt-1\';el.textContent=`❌ ${err.code || err.message || err}`;} });\n                    }else{\n                        adminPanel.style.display=\'none\';\n                        adminOnlyButtons.forEach(el=>el.style.display=\'none\');\n                        if(adminMenu) adminMenu.style.display=\'none\';\n                    }\n                    if(!currentSessionId) {\n                        createUserSession(firebaseUser.uid,userData).catch(err => console.warn(\'[SKYNET] Session sync skipped:\', err));\n                    } else startSessionHeartbeat();\n                    return true;\n                }catch(err){\n                    if(err.message===\'ACCOUNT_PENDING\') alert(TRANSLATIONS[currentLang].errPendingApproval);\n                    else if(err.message===\'ACCOUNT_NOT_APPROVED\') alert(TRANSLATIONS[currentLang].errAccountNotApproved);\n                }\n            }\n            document.getElementById(\'authContainer\').style.display=\'block\';\n            document.getElementById(\'mainDashboard\').style.display=\'none\';\n            const adminPanel=document.getElementById(\'adminPanel\'); if(adminPanel) adminPanel.style.display=\'none\';\n            const adminMenu=document.getElementById(\'adminPanelMenuBtn\'); if(adminMenu) adminMenu.style.display=\'none\';\n            document.querySelectorAll(\'[data-admin-only]\').forEach(el=>el.style.display=\'none\');\n            return false;\n        }\n\n        function scrollToAdminPanel(){\n            const panel=document.getElementById(\'adminPanel\');\n            if(panel) panel.scrollIntoView({behavior:\'smooth\',block:\'start\'});\n        }\n\n        document.getElementById(\'registerForm\').addEventListener(\'submit\',async(e)=>{\n            e.preventDefault();\n            const username=document.getElementById(\'regUser\').value.trim();\n            const password=document.getElementById(\'regPass\').value;\n            const regCode=document.getElementById(\'regCode\').value.trim();\n            if(username.length<3 || password.length<6){alert(TRANSLATIONS[currentLang].errWeakAccount);return;}\n            if(makeUserKey(username)===makeUserKey(ADMIN_DEFAULT_USERNAME)){alert(TRANSLATIONS[currentLang].errAdminUsername);return;}\n            if(regCode!==currentRegCode){alert(TRANSLATIONS[currentLang].errRegCodeInvalid);return;}\n            try{\n                const cred=await auth.createUserWithEmailAndPassword(usernameToAuthEmail(username),password);\n                const now=new Date().toISOString();\n                await usersRef.child(cred.user.uid).set({\n                    username, usernameKey:makeUserKey(username), role:\'user\', status:\'pending\', createdAt:now,\n                    approvedAt:null,approvedBy:null,rejectedAt:null,rejectedBy:null,lastLoginAt:null,lastLogoutAt:null,lastSeenAt:null,loginCount:0\n                });\n                await auth.signOut();\n                alert(TRANSLATIONS[currentLang].regPending);\n                document.getElementById(\'registerForm\').reset();\n                new bootstrap.Tab(document.getElementById(\'login-tab\')).show();\n            }catch(err){\n                console.error(err);\n                if(err.code===\'auth/email-already-in-use\') alert(TRANSLATIONS[currentLang].errUserExists);\n                else alert(TRANSLATIONS[currentLang].errLoginFailed);\n            }\n        });\n\n        document.getElementById(\'loginForm\').addEventListener(\'submit\', async (e) => {\n            e.preventDefault();\n            e.stopPropagation();\n            const username = document.getElementById(\'loginUser\').value.trim();\n            const password = document.getElementById(\'loginPass\').value;\n            const rememberMe = document.getElementById(\'rememberMe\').checked;\n            const submitBtn = e.currentTarget.querySelector(\'button[type=submit]\');\n            const debug = document.getElementById(\'loginDebug\');\n            const show = (msg) => {\n                if (debug) { debug.textContent = msg; debug.style.display = \'block\'; }\n                console.log(\'[SKYNET LOGIN]\', msg);\n            };\n            const fail = (msg) => {\n                show(\'❌ \' + msg);\n                alert(msg);\n                if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = \'🔑 เข้าสู่ระบบ\'; }\n            };\n            if (!username || !password) { fail(\'กรุณากรอกชื่อผู้ใช้และรหัสผ่าน\'); return; }\n            if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = \'⏳ กำลังเข้าสู่ระบบ...\'; }\n            let authEmail = username.includes(\'@\') ? username.toLowerCase() : usernameToAuthEmail(username);\n            if (makeUserKey(username) === \'admin\') authEmail = ADMIN_AUTH_EMAIL;\n            show(\'1/4 Firebase Config จาก Render พร้อม — กำลังเชื่อมต่อ Authentication: \' + authEmail);\n\n            try {\n                // Verify that the Firebase Auth SDK is actually ready before sending credentials.\n                if (!window.firebase || !firebase.auth) throw new Error(\'Firebase Authentication SDK โหลดไม่สำเร็จ\');\n                if (!firebase.apps || !firebase.apps.length) throw new Error(\'Firebase App ยังไม่ได้ initialize\');\n\n                // Important: use a real timer race. This prevents a browser/network hang from leaving the button stuck.\n                const loginPromise = auth.signInWithEmailAndPassword(authEmail, password);\n                const cred = await Promise.race([\n                    loginPromise,\n                    new Promise((_, reject) => setTimeout(() => {\n                        const er = new Error(\'Firebase Auth request timeout\'); er.code = \'AUTH_TIMEOUT\'; reject(er);\n                    }, 8000))\n                ]);\n\n                show(\'2/4 Firebase Authentication สำเร็จ — UID: \' + cred.user.uid);\n                const snap = await Promise.race([\n                    usersRef.child(cred.user.uid).once(\'value\'),\n                    new Promise((_, reject) => setTimeout(() => {\n                        const er = new Error(\'Database user read timeout\'); er.code = \'DB_TIMEOUT\'; reject(er);\n                    }, 8000))\n                ]);\n                if (!snap.exists()) {\n                    await auth.signOut().catch(() => {});\n                    fail(\'ล็อกอิน Firebase สำเร็จ แต่ไม่พบ users/\' + cred.user.uid + \' ใน Realtime Database\');\n                    return;\n                }\n                const userData = snap.val() || {};\n                show(\'3/4 ตรวจสอบสิทธิ์: username=\' + (userData.username || \'-\') + \' | role=\' + (userData.role || \'-\') + \' | status=\' + (userData.status || \'-\'));\n                if (userData.status !== \'approved\') {\n                    await auth.signOut().catch(() => {});\n                    fail(\'บัญชีนี้ยังไม่ได้รับอนุมัติ (status=\' + (userData.status || \'ไม่มี\') + \')\');\n                    return;\n                }\n                if (userData.role !== \'admin\' && makeUserKey(username) === \'admin\') {\n                    await auth.signOut().catch(() => {});\n                    fail(\'บัญชี Firebase นี้ไม่ใช่ Admin (role=\' + (userData.role || \'ไม่มี\') + \')\');\n                    return;\n                }\n                if (rememberMe) localStorage.setItem(\'saved_username\', username); else localStorage.removeItem(\'saved_username\');\n                currentUserData = userData;\n                currentUserKey = cred.user.uid;\n                localStorage.setItem(\'logged_user\', userData.username || username);\n                localStorage.setItem(\'logged_user_key\', cred.user.uid);\n                localStorage.setItem(\'logged_role\', userData.role || \'user\');\n                show(\'4/4 เข้าสู่ Dashboard สำเร็จ\');\n                document.getElementById(\'authContainer\').style.display = \'none\';\n                document.getElementById(\'mainDashboard\').style.display = \'block\';\n                document.getElementById(\'userBadge\').innerText = `👤 ${userData.username || cred.user.email}${userData.role === \'admin\' ? \' • 🛡️ Admin\' : \'\'}`;\n                const adminPanel = document.getElementById(\'adminPanel\');\n                const adminMenu = document.getElementById(\'adminPanelMenuBtn\');\n                if (userData.role === \'admin\') {\n                    if (adminPanel) adminPanel.style.display = \'block\';\n                    startAdminRealtimeListeners();\n                    document.querySelectorAll(\'[data-admin-only]\').forEach(el => el.style.display = \'\');\n                    if (adminMenu) adminMenu.style.display = \'inline-block\';\n                    loadAdminUsers().catch(err => console.warn(\'[SKYNET] admin list:\', err));\n                }\n                createUserSession(cred.user.uid, userData).catch(err => console.warn(\'[SKYNET] session sync:\', err));\n                if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = \'🔑 เข้าสู่ระบบ\'; }\n            } catch (err) {\n                console.error(\'[SKYNET] Login error\', err);\n                let msg = \'เข้าสู่ระบบไม่สำเร็จ\';\n                if (err.code === \'auth/invalid-credential\') msg = \'Email หรือรหัสผ่านไม่ถูกต้อง (Firebase: auth/invalid-credential)\';\n                else if (err.code === \'auth/wrong-password\') msg = \'รหัสผ่านไม่ถูกต้อง\';\n                else if (err.code === \'auth/user-not-found\') msg = \'ไม่พบบัญชี \' + authEmail + \' ใน Firebase Authentication\';\n                else if (err.code === \'auth/too-many-requests\') msg = \'Firebase ล็อกการลอง Login ชั่วคราว เพราะลองหลายครั้งเกินไป\';\n                else if (err.code === \'auth/operation-not-allowed\') msg = \'Firebase ยังไม่ได้เปิด Email/Password Authentication\';\n                else if (err.code === \'auth/network-request-failed\') msg = \'เชื่อมต่อ Firebase ไม่สำเร็จ\';\n                else if (err.code === \'auth/api-key-not-valid\' || /api-key-not-valid/i.test(err.message || \'\')) msg = \'Firebase API Key จาก Render ไม่ถูกต้อง — ตรวจ FIREBASE_WEB_CONFIG_JSON บน Render\';\n                else if (err.code === \'auth/requests-from-referer-https://iahcatan.github.io-are-blocked\') msg = \'Firebase API Key ยังบล็อก iahcatan.github.io\';\n                else if (err.code === \'AUTH_TIMEOUT\') msg = \'Firebase Authentication ไม่ตอบภายใน 8 วินาที — ปัญหาอยู่ที่การเชื่อมต่อ/API Key ไม่ใช่ฐานข้อมูล\';\n                else if (err.code === \'DB_TIMEOUT\') msg = \'Authentication ผ่านแล้ว แต่ Realtime Database ไม่ตอบภายใน 8 วินาที\';\n                else if (err.message) msg += \': \' + err.message;\n                fail(msg + \'\\n\\nบัญชีที่ใช้: \' + authEmail);\n            }\n        });\n\n        async function logout(){await endUserSession(true);checkAuthSession();}\n\n        function openChangeCodeModal(){\n            const modalEl=document.getElementById(\'changeCodeModal\');\n            const modal=bootstrap.Modal.getOrCreateInstance(modalEl);\n            document.getElementById(\'newRegCodeInput\').value=currentRegCode; modal.show();\n        }\n\n        document.getElementById(\'changeCodeForm\').addEventListener(\'submit\',async(e)=>{\n            e.preventDefault();\n            if(!await requireAdmin())return;\n            const newCode=document.getElementById(\'newRegCodeInput\').value.trim();\n            if(newCode){await settingsRef.child(\'register_code\').set(newCode);alert(TRANSLATIONS[currentLang].codeChangedSuccess);const modal=bootstrap.Modal.getInstance(document.getElementById(\'changeCodeModal\'));if(modal)modal.hide();}\n        });\n\n        // --- Admin User Management ---\n        async function requireAdmin(){\n            const uid=auth.currentUser && auth.currentUser.uid;\n            if(!uid)return false;\n            const snap=await usersRef.child(uid).once(\'value\'); const user=snap.val();\n            if(!user||user.role!==\'admin\'||user.status!==\'approved\'){alert(TRANSLATIONS[currentLang].adminOnly);return false;}\n            currentUserKey=uid; currentUserData=user; return true;\n        }\n\n        async function loadAdminUsers() {\n            const uid = auth.currentUser && auth.currentUser.uid;\n            if (!uid) return;\n            currentUserKey = uid;\n\n            const adminSnap = await withTimeout(usersRef.child(uid).once(\'value\'), 8000, \'ADMIN_SELF_READ_TIMEOUT\');\n            const adminData = adminSnap.val();\n            if (!adminData || adminData.role !== \'admin\' || adminData.status !== \'approved\') {\n                const panel=document.getElementById(\'adminPanel\'); if(panel) panel.style.display=\'none\';\n                return;\n            }\n            currentUserData = adminData;\n\n            const [usersSnap, sessionsSnap] = await Promise.all([\n                withTimeout(usersRef.once(\'value\'), 8000, \'ADMIN_USERS_READ_TIMEOUT\'),\n                withTimeout(sessionsRef.once(\'value\'), 8000, \'ADMIN_SESSIONS_READ_TIMEOUT\')\n            ]);\n            const users = usersSnap.val() || {};\n            const sessions = sessionsSnap.val() || {};\n            const nowMs = Date.now();\n            const statusEl = document.getElementById(\'adminDataStatus\');\n            if (statusEl) {\n                statusEl.className = \'small text-success mt-1\';\n                statusEl.textContent = `🟢 Firebase Users: ${Object.keys(users).length} | Sessions: ${Object.keys(sessions).length} | อัปเดต ${new Date().toLocaleTimeString(\'th-TH\')}`;\n            }\n\n            const userRows = Object.entries(users).map(([key, user]) => {\n                const userSessions = Object.values(sessions).filter(s => s && s.userKey === key);\n                const latestSession = userSessions.slice().sort((a,b) =>\n                    new Date(b.lastSeenAt || b.loginAt || 0) - new Date(a.lastSeenAt || a.loginAt || 0)\n                )[0];\n                const lastSeen = user.lastSeenAt || (latestSession && latestSession.lastSeenAt);\n                const userOnline = user.online === true || user.online === \'true\';\n                const userSeenMs = new Date(user.lastSeenAt || 0).getTime();\n                const userHeartbeatActive = userOnline && Number.isFinite(userSeenMs) && (nowMs - userSeenMs) >= 0 && (nowMs - userSeenMs) <= ACTIVE_SESSION_TIMEOUT_MS;\n                const sessionActive = userSessions.some(session => {\n                    if (!session || session.active === false || !session.lastSeenAt) return false;\n                    const seenMs = new Date(session.lastSeenAt).getTime();\n                    return Number.isFinite(seenMs) && (nowMs - seenMs) >= 0 && (nowMs - seenMs) <= ACTIVE_SESSION_TIMEOUT_MS;\n                });\n                const active = userHeartbeatActive || sessionActive;\n                return {key, user, active, lastSeen};\n            });\n\n            const pendingCount = userRows.filter(x => x.user.status === \'pending\').length;\n            const activeCount = userRows.filter(x => x.active && x.user.status === \'approved\').length;\n            document.getElementById(\'adminPendingCount\').innerText = pendingCount;\n            document.getElementById(\'adminActiveCount\').innerText = activeCount;\n            document.getElementById(\'adminTotalCount\').innerText = userRows.length;\n\n            const tbody = document.getElementById(\'adminUsersBody\');\n            tbody.innerHTML = \'\';\n            userRows.sort((a,b) => {\n                const order={pending:0,approved:1,rejected:2,disabled:3};\n                return (order[a.user.status] ?? 9)-(order[b.user.status] ?? 9) ||\n                    (new Date(b.lastSeen || b.user.createdAt || 0)-new Date(a.lastSeen || a.user.createdAt || 0));\n            });\n            userRows.forEach(({key,user,active,lastSeen}) => {\n                const statusBadge = user.role === \'admin\'\n                    ? \'<span class="badge bg-danger">🛡️ ADMIN</span>\'\n                    : user.status === \'pending\'\n                        ? \'<span class="badge bg-warning text-dark">⏳ Pending</span>\'\n                        : user.status === \'approved\'\n                            ? `<span class="badge ${active ? \'bg-success\' : \'bg-primary\'}">${active ? \'🟢 Active\' : \'✅ Approved\'}</span>`\n                            : user.status === \'rejected\'\n                                ? \'<span class="badge bg-danger">❌ Rejected</span>\'\n                                : \'<span class="badge bg-secondary">⛔ Disabled</span>\';\n                let actionHtml=\'-\';\n                if(user.role!==\'admin\'){\n                    if(user.status===\'pending\') actionHtml=`<div class="d-flex gap-1 flex-wrap"><button class="btn btn-sm btn-success" onclick="setUserStatus(\'${key}\',\'approved\')">✅ ${TRANSLATIONS[currentLang].adminApprove}</button><button class="btn btn-sm btn-danger" onclick="setUserStatus(\'${key}\',\'rejected\')">❌ ${TRANSLATIONS[currentLang].adminReject}</button></div>`;\n                    else if(user.status===\'approved\') actionHtml=`<button class="btn btn-sm btn-outline-warning" onclick="setUserStatus(\'${key}\',\'disabled\')">⛔ ${TRANSLATIONS[currentLang].adminDisable}</button>`;\n                    else actionHtml=`<button class="btn btn-sm btn-outline-success" onclick="setUserStatus(\'${key}\',\'approved\')">♻️ ${TRANSLATIONS[currentLang].adminActivate}</button>`;\n                }\n                const tr=document.createElement(\'tr\');\n                tr.innerHTML=`<td><div class="fw-bold text-info">${escapeHtml(user.username||key)}</div><small class="text-white-50">${user.role===\'admin\'?\'Admin\':\'User\'}</small></td><td>${statusBadge}</td><td><small>${formatAdminDate(user.createdAt)}</small></td><td><small>${formatAdminDate(user.lastLoginAt)}</small></td><td><small>${formatAdminDate(lastSeen)}</small></td><td>${Number(user.loginCount)||0}</td><td>${actionHtml}</td>`;\n                tbody.appendChild(tr);\n            });\n            if(!userRows.length) tbody.innerHTML=`<tr><td colspan="8" class="text-center text-muted py-3">${TRANSLATIONS[currentLang].adminNoUsers}</td></tr>`;\n        }\n\n        function startAdminRealtimeListeners(){\n            if(adminUsersListenerAttached || adminSessionsListenerAttached) return;\n            const refresh=()=>{\n                if(currentUserData && currentUserData.role===\'admin\' && auth.currentUser){\n                    loadAdminUsers().catch(err=>{\n                        console.error(\'[SKYNET] Admin realtime refresh:\',err);\n                        const el=document.getElementById(\'adminDataStatus\');\n                        if(el){el.className=\'small text-danger mt-1\';el.textContent=`❌ อ่านข้อมูลผู้ใช้ไม่สำเร็จ: ${err.code || err.message || err}`;}\n                    });\n                }\n            };\n            usersRef.on(\'value\', refresh);\n            sessionsRef.on(\'value\', refresh);\n            adminUsersListenerAttached=true;\n            adminSessionsListenerAttached=true;\n            if(adminRefreshTimer) clearInterval(adminRefreshTimer);\n            adminRefreshTimer=setInterval(refresh,15000);\n        }\n\n        function refreshAttendanceIndividualSelectors() {\n            const members = window.__SKYNET_ATTENDANCE_INDIVIDUAL_MEMBERS__ || {};\n            const memberSelect = document.getElementById(\'attendanceIndividualMemberSelect\');\n            const monthSelect = document.getElementById(\'attendanceIndividualMonthSelect\');\n            if (memberSelect) {\n                const oldMember = memberSelect.value || \'\';\n                const rows = Object.values(members).sort((a,b)=>String(a.name).localeCompare(String(b.name)));\n                memberSelect.innerHTML = `<option value="">${escapeHtml(TRANSLATIONS[currentLang].attendanceSelectMember || \'Select member\')}</option>` + rows.map(r => `<option value="${escapeHtml(r.uid)}">${escapeHtml(r.name)}</option>`).join(\'\');\n                if (oldMember && members[oldMember]) memberSelect.value = oldMember;\n            }\n            if (monthSelect) {\n                const oldMonth = monthSelect.value || \'all\';\n                const months = new Set([\'all\']);\n                Object.values(members).forEach(m => (m.entries || []).forEach(e => { if (e.monthKey) months.add(e.monthKey); }));\n                const sorted = Array.from(months).filter(x=>x!==\'all\').sort().reverse();\n                monthSelect.innerHTML = `<option value="all">${escapeHtml(TRANSLATIONS[currentLang].attendanceAllMonths || \'All months\')}</option>` + sorted.map(m => `<option value="${m}">${m}</option>`).join(\'\');\n                monthSelect.value = (oldMonth === \'all\' || sorted.includes(oldMonth)) ? oldMonth : \'all\';\n            }\n        }\n\n        function renderAttendanceIndividual() {\n            const members = window.__SKYNET_ATTENDANCE_INDIVIDUAL_MEMBERS__ || {};\n            const memberSelect = document.getElementById(\'attendanceIndividualMemberSelect\');\n            const monthSelect = document.getElementById(\'attendanceIndividualMonthSelect\');\n            const body = document.getElementById(\'attendanceIndividualBody\');\n            const nameEl = document.getElementById(\'attendanceIndividualNameValue\');\n            const checksEl = document.getElementById(\'attendanceIndividualCheckinsValue\');\n            const raidsEl = document.getElementById(\'attendanceIndividualRaidsValue\');\n            const lastEl = document.getElementById(\'attendanceIndividualLastValue\');\n            if (!body || !nameEl || !checksEl || !raidsEl || !lastEl) return;\n            const uid = memberSelect ? String(memberSelect.value || \'\') : \'\';\n            const month = monthSelect ? String(monthSelect.value || \'all\') : \'all\';\n            const member = uid ? members[uid] : null;\n            if (uid) console.debug(\'[SKYNET] Attendance individual lookup\', {uid: uid, found: !!member, entries: member ? (member.entries || []).length : 0, month: month});\n            if (!member) {\n                nameEl.textContent = \'-\'; checksEl.textContent=\'0\'; raidsEl.textContent=\'0\'; lastEl.textContent=\'-\';\n                body.innerHTML=\'<tr><td colspan="5" class="text-center text-muted">-</td></tr>\';\n                return;\n            }\n            const entries = (member.entries || []).filter(e => month === \'all\' || e.monthKey === month).slice().sort((a,b)=>String(b.checkedAt).localeCompare(String(a.checkedAt)));\n            nameEl.textContent = member.name || uid;\n            checksEl.textContent = String(entries.length);\n            raidsEl.textContent = String(new Set(entries.map(e=>e.activityId || `${e.activityDate}|${e.bossName}|${e.attackTime}`)).size);\n            lastEl.textContent = entries[0] && entries[0].checkedAt ? new Date(entries[0].checkedAt).toLocaleString(\'th-TH\',{hour12:false}) : \'-\';\n            body.innerHTML = entries.length ? entries.map(e => {\n                const checkedAt = e.checkedAt ? new Date(e.checkedAt).toLocaleString(\'th-TH\',{hour12:false}) : \'-\';\n                const status = e.status === \'checked_in\' ? (currentLang===\'en\'?\'Checked in\':currentLang===\'ko\'?\'출석\':\'เช็คชื่อแล้ว\') : escapeHtml(e.status || \'-\');\n                return `<tr><td>${escapeHtml(e.activityDate)}</td><td class="fw-bold text-warning">${escapeHtml(e.bossName)}</td><td>${escapeHtml(e.attackTime)}</td><td>${escapeHtml(checkedAt)}</td><td>${status}</td></tr>`;\n            }).join(\'\') : \'<tr><td colspan="5" class="text-center text-muted">-</td></tr>\';\n        }\n\n        document.getElementById(\'attendanceIndividualMemberSelect\')?.addEventListener(\'change\', renderAttendanceIndividual);\n        document.getElementById(\'attendanceIndividualMonthSelect\')?.addEventListener(\'change\', renderAttendanceIndividual);\n\n        function toggleAttendanceHistoryPanel(forceOpen=null) {\n            const content = document.getElementById(\'attendanceHistoryPanelContent\');\n            const btn = document.getElementById(\'attendanceHistoryCollapseBtn\');\n            if (!content) return;\n            const shouldOpen = forceOpen === null ? content.style.display !== \'block\' : !!forceOpen;\n            content.style.display = shouldOpen ? \'block\' : \'none\';\n            if (btn) {\n                const key = shouldOpen ? \'attendanceCollapseClose\' : \'attendanceCollapseOpen\';\n                btn.textContent = (TRANSLATIONS[currentLang] && TRANSLATIONS[currentLang][key]) || (shouldOpen ? \'▲ ปิด\' : \'▼ เปิด\');\n            }\n            localStorage.setItem(\'attendance_history_open\', shouldOpen ? \'1\' : \'0\');\n        }\n\n        function toggleAttendanceIndividualPanel(forceOpen=null) {\n            const content = document.getElementById(\'attendanceIndividualPanelContent\');\n            const btn = document.getElementById(\'attendanceIndividualCollapseBtn\');\n            if (!content) return;\n            const shouldOpen = forceOpen === null ? content.style.display !== \'block\' : !!forceOpen;\n            content.style.display = shouldOpen ? \'block\' : \'none\';\n            if (btn) {\n                const key = shouldOpen ? \'attendanceCollapseClose\' : \'attendanceCollapseOpen\';\n                btn.textContent = (TRANSLATIONS[currentLang] && TRANSLATIONS[currentLang][key]) || (shouldOpen ? \'▲ ปิด\' : \'▼ เปิด\');\n            }\n            localStorage.setItem(\'attendance_individual_open\', shouldOpen ? \'1\' : \'0\');\n        }\n\n        function escapeHtml(value) {\n            return String(value ?? \'\').replace(/[&<>"\']/g, ch => ({\n                \'&\': \'&amp;\', \'<\': \'&lt;\', \'>\': \'&gt;\', \'"\': \'&quot;\', "\'": \'&#039;\'\n            }[ch]));\n        }\n\n        async function setUserStatus(userKey, newStatus) {\n            if (!currentUserKey) return;\n\n            const adminSnap = await withTimeout(usersRef.child(currentUserKey).once(\'value\'), 8000, \'ADMIN_SELF_READ_TIMEOUT\');\n            const adminData = adminSnap.val();\n            if (!adminData || adminData.role !== \'admin\' || adminData.status !== \'approved\') {\n                alert(TRANSLATIONS[currentLang].adminOnly);\n                return;\n            }\n\n            const targetSnap = await usersRef.child(userKey).once(\'value\');\n            if (!targetSnap.exists()) return;\n            const target = targetSnap.val();\n\n            if (target.role === \'admin\') {\n                alert(TRANSLATIONS[currentLang].adminCannotChangeAdmin);\n                return;\n            }\n\n            const now = new Date().toISOString();\n            const updates = { status: newStatus };\n\n            if (newStatus === \'approved\') {\n                updates.approvedAt = now;\n                updates.approvedBy = adminData.username;\n                updates.rejectedAt = null;\n                updates.rejectedBy = null;\n            } else if (newStatus === \'rejected\') {\n                updates.rejectedAt = now;\n                updates.rejectedBy = adminData.username;\n            }\n\n            await usersRef.child(userKey).update(updates);\n\n            // หากถูกปิดการใช้งาน ให้ปิด session ที่กำลัง active ของ user นั้นด้วย\n            if (newStatus === \'disabled\' || newStatus === \'rejected\') {\n                const sessionsSnap = await sessionsRef.once(\'value\');\n                const sessions = sessionsSnap.val() || {};\n                const sessionUpdates = {};\n                Object.entries(sessions).forEach(([sessionId, session]) => {\n                    if (session && session.userKey === userKey && session.active !== false) {\n                        sessionUpdates[`${sessionId}/active`] = false;\n                        sessionUpdates[`${sessionId}/logoutAt`] = now;\n                    }\n                });\n                if (Object.keys(sessionUpdates).length) {\n                    await sessionsRef.update(sessionUpdates);\n                }\n            }\n\n            await loadAdminUsers();\n        }\n\n        async function requireApprovedUser(){\n            const uid=auth.currentUser && auth.currentUser.uid;\n            if(!uid){await checkAuthSession();alert(TRANSLATIONS[currentLang].errAccountNotApproved);return false;}\n            const snap=await usersRef.child(uid).once(\'value\'); const user=snap.val();\n            if(!user||user.status!==\'approved\'){await endUserSession(true);await checkAuthSession();alert(TRANSLATIONS[currentLang].errAccountNotApproved);return false;}\n            currentUserKey=uid;currentUserData=user;return true;\n        }\n\n        // Firebase Authentication session listener\n        auth.onAuthStateChanged(async (firebaseUser) => {\n            if (firebaseUser) {\n                await checkAuthSession(firebaseUser);\n            } else {\n                const dashboard=document.getElementById(\'mainDashboard\');\n                if(dashboard && dashboard.style.display===\'block\') await checkAuthSession(null);\n            }\n        });\n\n        // --- 4. ระบบ Real-time Sync จาก Firebase ---\n\n        let userNameCache = {};\n        let attendanceRealtimeListenerAttached = false;\n        let attendanceRealtimeTimer = null;\n        let attendanceRealtimeAbortController = null;\n        let attendanceRealtimeBusy = false;\n        let attendanceRealtimeUnauthorized = false;\n        usersRef.on(\'value\', (snapshot) => {\n            const users = snapshot.val() || {};\n            userNameCache = {};\n            Object.entries(users).forEach(([uid, u]) => {\n                if (u && typeof u === \'object\') userNameCache[uid] = u.username || u.email || uid;\n            });\n        });\n\n        bossRef.on(\'value\', (snapshot) => {\n            const rawData = snapshot.val() || {};\n            activeBosses = {};\n            \n            Object.keys(rawData).forEach(bossName => {\n                const item = rawData[bossName];\n                let spawnMs = item.spawnTimeMs;\n                if (!spawnMs && item.spawn_time) {\n                    spawnMs = new Date(item.spawn_time).getTime();\n                }\n                const cdSec = BOSS_DATABASE[bossName] ? BOSS_DATABASE[bossName].cd : 0;\n                let killMs = item.killTimeMs || (spawnMs - (cdSec * 1000));\n                \n                activeBosses[bossName] = {\n                    spawnTimeMs: spawnMs,\n                    killTimeMs: killMs,\n                    noticeMinutes: item.noticeMinutes || (BOSS_DATABASE[bossName] ? BOSS_DATABASE[bossName].notice : 5),\n                    notifiedNotice: item.notified_advance || item.notifiedNotice || false,\n                    notifiedSpawn: item.notifiedSpawn || false,\n                    killDate: item.killDate || (item.killTimeMs ? new Date(item.killTimeMs).toLocaleDateString(\'th-TH\') : \'\'),\n                    recordedBy: (item.recordedBy && item.recordedBy !== \'Unknown\' ? item.recordedBy : (item.recordedByUserId && userNameCache[item.recordedByUserId]) || item.recorded_by || item.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || \'ไม่ระบุ\')\n                };\n            });\n            renderTable();\n            toggleAttendancePanel(localStorage.getItem(\'attendance_panel_open\') === \'1\');\n            toggleAttendanceHistoryPanel(localStorage.getItem(\'attendance_history_open\') === \'1\');\n            toggleAttendanceMemberMonthlyPanel(localStorage.getItem(\'attendance_member_monthly_open\') === \'1\');\n            toggleAttendanceIndividualPanel(localStorage.getItem(\'attendance_individual_open\') === \'1\');\n        });\n\n        // --- UI & Control Functions ---\n        function applyKillDateLanguage() {\n            const langData = TRANSLATIONS[currentLang];\n            if (!langData) return;\n            const label = document.querySelector(\'label[data-i18n="labelKillDate"]\');\n            const input = document.getElementById(\'killDate\');\n            const hint = document.querySelector(\'[data-i18n="hintKillDate"]\');\n            if (label && langData.labelKillDate) label.textContent = langData.labelKillDate;\n            if (input && langData.phKillDate) input.placeholder = langData.phKillDate;\n            if (hint && langData.hintKillDate) hint.textContent = langData.hintKillDate;\n        }\n\n        function renderAttendanceDashboard(rootData) {\n            try {\n                const all = [];\n                const monthlyMembers = {};\n                const individualMembers = {};\n                const nowMs = Date.now();\n                const unique = new Set();\n                let totalCheckins = 0;\n                Object.entries(rootData || {}).forEach(([guildId, acts]) => {\n                    if (!acts || typeof acts !== \'object\') return;\n                    Object.entries(acts).forEach(([activityId, a]) => {\n                        if (!a || typeof a !== \'object\') return;\n                        const participants = a.participants && typeof a.participants === \'object\' ? Object.values(a.participants) : [];\n                        const checked = participants.filter(p => p && p.status === \'checked_in\');\n                        const attackAt = String(a.attack_at || a.created_at || \'\');\n                        const monthKey = /^\\d{4}-\\d{2}/.test(attackAt) ? attackAt.slice(0,7) : \'\';\n                        all.push({ guildId, activityId, a, checked, attackMs: Date.parse(attackAt) || 0, monthKey });\n                        checked.forEach(p => {\n                            totalCheckins++;\n                            const uid=String(p.user_id||\'\');\n                            if(uid) unique.add(uid);\n                            if(!uid || !monthKey) return;\n                            const bucket = monthlyMembers[monthKey] || (monthlyMembers[monthKey]={});\n                            const row = bucket[uid] || (bucket[uid]={uid, name:p.display_name||p.username||uid, checkins:0, raids:0, lastCheckin:\'\'});\n                            row.checkins += 1;\n                            row.raids += 1;\n                            const checkedAt=String(p.checked_in_at||\'\');\n                            if(checkedAt && (!row.lastCheckin || checkedAt > row.lastCheckin)) row.lastCheckin=checkedAt;\n\n                            const individual = individualMembers[uid] || (individualMembers[uid]={uid, name:p.display_name||p.username||uid, entries:[]});\n                            individual.entries.push({\n                                monthKey,\n                                bossName:String(a.boss_name||\'-\'),\n                                activityDate:String(a.activity_date||\'-\'),\n                                attackTime:String(a.attack_time||\'-\'),\n                                checkedAt,\n                                status:String(p.status||\'-\'),\n                                activityId:String(activityId||\'\')\n                            });\n                        });\n                    });\n                });\n                all.sort((x,y)=>(x.attackMs||0)-(y.attackMs||0));\n                document.getElementById(\'attendanceTotalRaidsCount\').innerText = all.length;\n                document.getElementById(\'attendanceUniqueMembersCount\').innerText = unique.size;\n                document.getElementById(\'attendanceTotalCheckinsCount\').innerText = totalCheckins;\n\n                const current = all.filter(x => x.a.status !== \'closed\');\n                const history = all.filter(x => x.a.status === \'closed\').slice().reverse().slice(0,100);\n                const currentBody = document.getElementById(\'attendanceCurrentBody\');\n                currentBody.innerHTML = current.length ? current.map(x => {\n                    const a=x.a; const status=a.status===\'open\' ? (currentLang===\'ko\'?\'진행 중\':currentLang===\'en\'?\'OPEN\':\'เปิด\') : (currentLang===\'ko\'?\'예정\':currentLang===\'en\'?\'SCHEDULED\':\'กำหนดการ\');\n                    return `<tr><td class="fw-bold text-warning">${escapeHtml(String(a.boss_name||\'-\'))}</td><td>${escapeHtml(String(a.activity_date||\'-\'))}</td><td>${escapeHtml(String(a.attack_time||\'-\'))}</td><td>${escapeHtml(String(a.checkin_open||\'-\'))} – ${escapeHtml(String(a.checkin_close||\'-\'))}</td><td>${x.checked.length}</td><td>${status}</td></tr>`;\n                }).join(\'\') : \'<tr><td colspan="6" class="text-center text-muted">-</td></tr>\';\n                const historyBody = document.getElementById(\'attendanceHistoryBody\');\n                historyBody.innerHTML = history.length ? history.map(x => {\n                    const a=x.a;\n                    return `<tr><td class="fw-bold text-warning">${escapeHtml(String(a.boss_name||\'-\'))}</td><td>${escapeHtml(String(a.activity_date||\'-\'))}</td><td>${escapeHtml(String(a.attack_time||\'-\'))}</td><td>${x.checked.length}</td><td>${escapeHtml(String(a.created_by_name||a.created_by||\'-\'))}</td><td>${currentLang===\'ko\'?\'마감\':currentLang===\'en\'?\'CLOSED\':\'ปิดแล้ว\'}</td></tr>`;\n                }).join(\'\') : \'<tr><td colspan="6" class="text-center text-muted">-</td></tr>\';\n\n                const months = Object.keys(monthlyMembers).sort().reverse();\n                const monthlyBody = document.getElementById(\'attendanceMonthlyBody\');\n                monthlyBody.innerHTML = months.length ? months.map(m => {\n                    const members = Object.values(monthlyMembers[m]);\n                    const raids = all.filter(x => x.monthKey === m).length;\n                    const checks = members.reduce((n,row)=>n+row.checkins,0);\n                    return `<tr><td>${m}</td><td>${raids}</td><td>${members.length}</td><td>${checks}</td></tr>`;\n                }).join(\'\') : \'<tr><td colspan="4" class="text-center text-muted">-</td></tr>\';\n\n                const monthSelect = document.getElementById(\'attendanceMemberMonthSelect\');\n                const oldValue = monthSelect ? monthSelect.value : \'all\';\n                if (monthSelect) {\n                    monthSelect.innerHTML = `<option value="all">${escapeHtml(TRANSLATIONS[currentLang].attendanceAllMonths || \'All months\')}</option>` + months.map(m => `<option value="${m}">${m}</option>`).join(\'\');\n                    monthSelect.value = (oldValue === \'all\' || months.includes(oldValue)) ? oldValue : \'all\';\n                }\n                window.__SKYNET_ATTENDANCE_MONTHLY_MEMBERS__ = monthlyMembers;\n                window.__SKYNET_ATTENDANCE_INDIVIDUAL_MEMBERS__ = individualMembers;\n                renderAttendanceMemberMonthly(monthSelect ? monthSelect.value : \'all\');\n                refreshAttendanceIndividualSelectors();\n                renderAttendanceIndividual();\n                toggleAttendanceMemberMonthlyPanel(localStorage.getItem(\'attendance_member_monthly_open\') === \'1\');\n                toggleAttendanceHistoryPanel(localStorage.getItem(\'attendance_history_open\') === \'1\');\n                toggleAttendanceIndividualPanel(localStorage.getItem(\'attendance_individual_open\') === \'1\');\n            } catch (e) { console.warn(\'[SKYNET] attendance dashboard render:\', e); }\n        }\n\n        function renderAttendanceMemberMonthly(selectedMonth=\'all\') {\n            const body=document.getElementById(\'attendanceMemberMonthlyBody\');\n            if(!body) return;\n            const source=window.__SKYNET_ATTENDANCE_MONTHLY_MEMBERS__ || {};\n            const merged={};\n            const buckets=selectedMonth===\'all\' ? Object.values(source) : [source[selectedMonth] || {}];\n            buckets.forEach(bucket => Object.values(bucket).forEach(row => {\n                const uid=String(row.uid||\'\');\n                if(!uid) return;\n                const out=merged[uid] || (merged[uid]={uid,name:row.name||uid,checkins:0,raids:0,lastCheckin:\'\'});\n                out.checkins += Number(row.checkins||0);\n                out.raids += Number(row.raids||0);\n                if(row.lastCheckin && (!out.lastCheckin || row.lastCheckin > out.lastCheckin)) out.lastCheckin=row.lastCheckin;\n            }));\n            const rows=Object.values(merged).sort((a,b)=>b.checkins-a.checkins || String(a.name).localeCompare(String(b.name)));\n            body.innerHTML=rows.length ? rows.map((r,i)=>{\n                const last=r.lastCheckin ? new Date(r.lastCheckin).toLocaleString(\'th-TH\',{hour12:false}) : \'-\';\n                return `<tr><td>${i+1}</td><td>${escapeHtml(r.name)}</td><td><strong>${r.checkins}</strong></td><td>${r.raids}</td><td>${escapeHtml(last)}</td></tr>`;\n            }).join(\'\') : \'<tr><td colspan="5" class="text-center text-muted">-</td></tr>\';\n        }\n\n        function escapeHtml(value) {\n            return String(value).replace(/[&<>\'"]/g, ch => ({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[ch]));\n        }\n\n        async function fetchAttendanceServerSnapshot(){\n            if (!auth.currentUser || attendanceRealtimeUnauthorized) return;\n            if (attendanceRealtimeBusy) return;\n            attendanceRealtimeBusy = true;\n            try {\n                const idToken = await auth.currentUser.getIdToken(false);\n                const apiOrigin = window.SKYNET_API_ORIGIN || \'https://bosstimer-ry18.onrender.com\';\n                const response = await fetch(`${apiOrigin}/api/attendance-data`, {\n                    method: \'GET\',\n                    headers: { \'Authorization\': `Bearer ${idToken}` },\n                    cache: \'no-store\'\n                });\n                const result = await response.json().catch(() => ({}));\n                if (!response.ok || !result.success) {\n                    const err = new Error(result.error || `HTTP ${response.status}`);\n                    err.status = response.status;\n                    throw err;\n                }\n                window.__SKYNET_ATTENDANCE_ROOT__ = result.raid_attendance || {};\n                if (result.individual_members && typeof result.individual_members === \'object\') {\n                    window.__SKYNET_ATTENDANCE_INDIVIDUAL_MEMBERS__ = result.individual_members;\n                }\n                renderAttendanceDashboard(window.__SKYNET_ATTENDANCE_ROOT__);\n                if (result.individual_members && typeof result.individual_members === \'object\') {\n                    refreshAttendanceIndividualSelectors();\n                    renderAttendanceIndividual();\n                }\n                window.__SKYNET_MONTHLY_REPORTS__ = result.monthly_reports || {};\n                const status = document.getElementById(\'attendanceRealtimeStatus\');\n                if (status) {\n                    status.className = \'badge bg-success status-badge\';\n                    status.textContent = TRANSLATIONS[currentLang].attendanceRealtime || \'🟢 Real-time\';\n                    status.title = \'Server-authoritative sync\';\n                }\n                attendanceRealtimeUnauthorized = false;\n            } catch (err) {\n                console.error(\'[SKYNET] Attendance server sync:\', err);\n                const status = document.getElementById(\'attendanceRealtimeStatus\');\n                if (status) {\n                    status.className = \'badge bg-danger status-badge\';\n                    status.textContent = err.status === 401 || err.status === 403 ? \'🔐 Attendance Auth Error\' : \'🔴 Attendance Sync Error\';\n                }\n                if (err.status === 401 || err.status === 403) attendanceRealtimeUnauthorized = true;\n            } finally {\n                attendanceRealtimeBusy = false;\n            }\n        }\n\n        document.getElementById(\'attendanceMemberMonthSelect\')?.addEventListener(\'change\', (e) => {\n            renderAttendanceMemberMonthly(e.target.value || \'all\');\n        });\n\n        function stopAttendanceRealtimeListener(){\n            if (attendanceRealtimeTimer) {\n                clearTimeout(attendanceRealtimeTimer);\n                attendanceRealtimeTimer = null;\n            }\n            if (attendanceRealtimeAbortController) {\n                attendanceRealtimeAbortController.abort();\n                attendanceRealtimeAbortController = null;\n            }\n            attendanceRealtimeListenerAttached = false;\n            attendanceRealtimeBusy = false;\n        }\n\n        function applyAttendanceServerResult(result){\n            window.__SKYNET_ATTENDANCE_ROOT__ = result.raid_attendance || {};\n            if (result.individual_members && typeof result.individual_members === \'object\') {\n                window.__SKYNET_ATTENDANCE_INDIVIDUAL_MEMBERS__ = result.individual_members;\n            }\n            window.__SKYNET_MONTHLY_REPORTS__ = result.monthly_reports || {};\n            renderAttendanceDashboard(window.__SKYNET_ATTENDANCE_ROOT__);\n            if (result.individual_members && typeof result.individual_members === \'object\') {\n                refreshAttendanceIndividualSelectors();\n                renderAttendanceIndividual();\n            }\n            const status = document.getElementById(\'attendanceRealtimeStatus\');\n            if (status) {\n                status.className = \'badge bg-success status-badge\';\n                status.textContent = TRANSLATIONS[currentLang].attendanceRealtime || \'🟢 Real-time\';\n                status.title = `Event sync v${result.attendance_version || 0}`;\n            }\n        }\n\n        async function fetchAttendanceServerSnapshot(){\n            if (!auth.currentUser || attendanceRealtimeUnauthorized || attendanceRealtimeBusy) return null;\n            attendanceRealtimeBusy = true;\n            try {\n                const idToken = await auth.currentUser.getIdToken(false);\n                const apiOrigin = window.SKYNET_API_ORIGIN || \'https://bosstimer-ry18.onrender.com\';\n                const response = await fetch(`${apiOrigin}/api/attendance-data`, {\n                    method: \'GET\',\n                    headers: { \'Authorization\': `Bearer ${idToken}` },\n                    cache: \'no-store\'\n                });\n                const result = await response.json().catch(() => ({}));\n                if (!response.ok || !result.success) {\n                    const err = new Error(result.error || `HTTP ${response.status}`);\n                    err.status = response.status;\n                    throw err;\n                }\n                applyAttendanceServerResult(result);\n                attendanceRealtimeUnauthorized = false;\n                return result;\n            } catch (err) {\n                console.error(\'[SKYNET] Attendance initial sync:\', err);\n                const status = document.getElementById(\'attendanceRealtimeStatus\');\n                if (status) {\n                    status.className = \'badge bg-danger status-badge\';\n                    status.textContent = err.status === 401 || err.status === 403 ? \'🔐 Attendance Auth Error\' : \'🔴 Attendance Sync Error\';\n                }\n                if (err.status === 401 || err.status === 403) attendanceRealtimeUnauthorized = true;\n                return null;\n            } finally {\n                attendanceRealtimeBusy = false;\n            }\n        }\n\n        async function startAttendanceRealtimeStream(){\n            if (!auth.currentUser || attendanceRealtimeUnauthorized) return;\n            attendanceRealtimeAbortController = new AbortController();\n            try {\n                const idToken = await auth.currentUser.getIdToken(false);\n                const apiOrigin = window.SKYNET_API_ORIGIN || \'https://bosstimer-ry18.onrender.com\';\n                const response = await fetch(`${apiOrigin}/api/attendance-stream`, {\n                    method: \'GET\',\n                    headers: { \'Authorization\': `Bearer ${idToken}`, \'Accept\': \'text/event-stream\' },\n                    cache: \'no-store\',\n                    signal: attendanceRealtimeAbortController.signal\n                });\n                if (!response.ok || !response.body) {\n                    const err = new Error(`Attendance stream HTTP ${response.status}`);\n                    err.status = response.status;\n                    throw err;\n                }\n                const reader = response.body.getReader();\n                const decoder = new TextDecoder();\n                let buffer = \'\';\n                while (true) {\n                    const {value, done} = await reader.read();\n                    if (done) break;\n                    buffer += decoder.decode(value, {stream:true});\n                    const frames = buffer.split(\'\\n\\n\');\n                    buffer = frames.pop() || \'\';\n                    for (const frame of frames) {\n                        const dataLine = frame.split(\'\\n\').find(line => line.startsWith(\'data: \'));\n                        if (!dataLine) continue;\n                        try {\n                            const result = JSON.parse(dataLine.slice(6));\n                            if (result && result.success) applyAttendanceServerResult(result);\n                        } catch (parseErr) {\n                            console.warn(\'[SKYNET] Attendance stream parse:\', parseErr);\n                        }\n                    }\n                }\n            } catch (err) {\n                if (err && err.name === \'AbortError\') return;\n                console.error(\'[SKYNET] Attendance event stream:\', err);\n                const status = document.getElementById(\'attendanceRealtimeStatus\');\n                if (status) {\n                    status.className = \'badge bg-danger status-badge\';\n                    status.textContent = err.status === 401 || err.status === 403 ? \'🔐 Attendance Auth Error\' : \'🔴 Attendance Stream Error\';\n                }\n                if (auth.currentUser && !attendanceRealtimeUnauthorized && attendanceRealtimeListenerAttached) {\n                    attendanceRealtimeTimer = setTimeout(() => startAttendanceRealtimeStream(), 5000);\n                }\n            } finally {\n                attendanceRealtimeAbortController = null;\n            }\n        }\n\n        function startAttendanceRealtimeListener(){\n            if (attendanceRealtimeListenerAttached || !auth.currentUser) return;\n            attendanceRealtimeListenerAttached = true;\n            attendanceRealtimeUnauthorized = false;\n            fetchAttendanceServerSnapshot().then(() => {\n                if (attendanceRealtimeListenerAttached && !attendanceRealtimeUnauthorized) startAttendanceRealtimeStream();\n            });\n        }\n\n        function toggleAttendancePanel(forceOpen=null) {\n            const content = document.getElementById(\'attendancePanelContent\');\n            const btn = document.getElementById(\'attendanceCollapseBtn\');\n            if (!content) return;\n            const shouldOpen = forceOpen === null ? content.style.display !== \'block\' : !!forceOpen;\n            content.style.display = shouldOpen ? \'block\' : \'none\';\n            if (btn) {\n                const key = shouldOpen ? \'attendanceCollapseClose\' : \'attendanceCollapseOpen\';\n                btn.textContent = (TRANSLATIONS[currentLang] && TRANSLATIONS[currentLang][key]) || (shouldOpen ? \'▲ ปิด\' : \'▼ เปิด\');\n            }\n            localStorage.setItem(\'attendance_panel_open\', shouldOpen ? \'1\' : \'0\');\n        }\n\n        function toggleAttendanceMemberMonthlyPanel(forceOpen=null) {\n            const content = document.getElementById(\'attendanceMemberMonthlyPanelContent\');\n            const btn = document.getElementById(\'attendanceMemberMonthlyCollapseBtn\');\n            if (!content) return;\n            const shouldOpen = forceOpen === null ? content.style.display !== \'block\' : !!forceOpen;\n            content.style.display = shouldOpen ? \'block\' : \'none\';\n            if (btn) {\n                const key = shouldOpen ? \'attendanceCollapseClose\' : \'attendanceCollapseOpen\';\n                btn.textContent = (TRANSLATIONS[currentLang] && TRANSLATIONS[currentLang][key]) || (shouldOpen ? \'▲ ปิด\' : \'▼ เปิด\');\n            }\n            localStorage.setItem(\'attendance_member_monthly_open\', shouldOpen ? \'1\' : \'0\');\n        }\n\n        function applyLanguage() {\n            const langData = TRANSLATIONS[currentLang];\n            document.querySelectorAll(\'[data-i18n]\').forEach(el => {\n                const key = el.getAttribute(\'data-i18n\');\n                if (langData[key]) el.innerText = langData[key];\n            });\n            document.querySelectorAll(\'[data-i18n-ph]\').forEach(el => {\n                const key = el.getAttribute(\'data-i18n-ph\');\n                if (langData[key]) el.placeholder = langData[key];\n            });\n            \n            updateNotifyButtonUI();\n            applyKillDateLanguage();\n            renderTable();\n        }\n\n        function changeLanguage(lang) {\n            currentLang = lang;\n            localStorage.setItem(\'app_lang\', lang);\n\n            const authLangSelect = document.getElementById(\'authLangSelect\');\n            const langSelect = document.getElementById(\'langSelect\');\n            if (authLangSelect) authLangSelect.value = lang;\n            if (langSelect) langSelect.value = lang;\n\n            applyLanguage();\n        }\n\n        function changeTimezone(tz) {\n            currentTz = tz;\n            localStorage.setItem(\'app_tz\', tz);\n            renderTable();\n        }\n\n        function updateNotifyButtonUI() {\n            const notifyBtn = document.getElementById(\'notifyToggleBtn\');\n            const langData = TRANSLATIONS[currentLang];\n            \n            if (isNotifyEnabled) {\n                notifyBtn.className = "btn btn-outline-success btn-sm fw-bold";\n                notifyBtn.setAttribute(\'data-i18n\', \'disableNotify\');\n                notifyBtn.innerText = langData.disableNotify;\n            } else {\n                notifyBtn.className = "btn btn-warning btn-sm fw-bold";\n                notifyBtn.setAttribute(\'data-i18n\', \'enableNotify\');\n                notifyBtn.innerText = langData.enableNotify;\n            }\n        }\n\n        // --- Audio System ---\n        let audioCtx = null;\n        function playAlertSound(type) {\n            if (!isNotifyEnabled) return;\n            \n            try {\n                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();\n                if (audioCtx.state === \'suspended\') audioCtx.resume();\n\n                const osc = audioCtx.createOscillator();\n                const gain = audioCtx.createGain();\n                osc.connect(gain);\n                gain.connect(audioCtx.destination);\n\n                if (type === \'notice\') {\n                    osc.type = \'sine\';\n                    osc.frequency.setValueAtTime(587.33, audioCtx.currentTime);\n                    gain.gain.setValueAtTime(0.2, audioCtx.currentTime);\n                    osc.start();\n                    osc.stop(audioCtx.currentTime + 0.15);\n                    setTimeout(() => {\n                        const osc2 = audioCtx.createOscillator();\n                        const gain2 = audioCtx.createGain();\n                        osc2.connect(gain2);\n                        gain2.connect(audioCtx.destination);\n                        osc2.type = \'sine\';\n                        osc2.frequency.setValueAtTime(880, audioCtx.currentTime);\n                        gain2.gain.setValueAtTime(0.2, audioCtx.currentTime);\n                        osc2.start();\n                        osc2.stop(audioCtx.currentTime + 0.2);\n                    }, 200);\n                } else if (type === \'spawn\') {\n                    osc.type = \'sawtooth\';\n                    osc.frequency.setValueAtTime(880, audioCtx.currentTime);\n                    osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.4);\n                    gain.gain.setValueAtTime(0.3, audioCtx.currentTime);\n                    osc.start();\n                    osc.stop(audioCtx.currentTime + 0.4);\n                }\n            } catch (e) {\n                console.log("Audio play error:", e);\n            }\n        }\n\n        function toggleNotifications() {\n            const langData = TRANSLATIONS[currentLang];\n            \n            if (isNotifyEnabled) {\n                isNotifyEnabled = false;\n                localStorage.setItem(\'notify_enabled\', \'false\');\n                updateNotifyButtonUI();\n                return;\n            }\n\n            if (!audioCtx) {\n                audioCtx = new (window.AudioContext || window.webkitAudioContext)();\n            }\n            if (audioCtx.state === \'suspended\') {\n                audioCtx.resume();\n            }\n\n            if ("Notification" in window) {\n                Notification.requestPermission().then(permission => {\n                    if (permission === "granted") {\n                        isNotifyEnabled = true;\n                        localStorage.setItem(\'notify_enabled\', \'true\');\n                        updateNotifyButtonUI();\n                        playAlertSound(\'notice\');\n                        new Notification(langData.notifyReadyTitle, { body: langData.notifyReadyBody });\n                    } else {\n                        alert(langData.grantNotifyPrompt);\n                    }\n                });\n            } else {\n                alert(langData.noNotifySupport);\n            }\n        }\n\n        function sendBrowserNotification(title, body) {\n            if (!isNotifyEnabled) return;\n            if ("Notification" in window && Notification.permission === "granted") {\n                new Notification(title, { body: body, requireInteraction: true });\n            }\n        }\n\n        function format24h(dateObj) {\n            if (!dateObj || isNaN(dateObj.getTime())) return "00:00:00";\n            const options = { hour: \'2-digit\', minute: \'2-digit\', second: \'2-digit\', hour12: false, hourCycle: \'h23\' };\n            if (currentTz && currentTz !== \'auto\') options.timeZone = currentTz;\n\n            const formatter = new Intl.DateTimeFormat(\'en-GB\', options);\n            const parts = formatter.formatToParts(dateObj);\n            let h = \'00\', m = \'00\', s = \'00\';\n            for (const part of parts) {\n                if (part.type === \'hour\') h = part.value;\n                if (part.type === \'minute\') m = part.value;\n                if (part.type === \'second\') s = part.value;\n            }\n            return `${h}:${m}:${s}`;\n        }\n\n        function parseInputTime(timeStr) {\n            if (!timeStr) return new Date();\n            let h, m;\n            if (timeStr.includes(\':\')) {\n                const parts = timeStr.split(\':\');\n                h = parseInt(parts[0], 10);\n                m = parseInt(parts[1], 10);\n            } else if (timeStr.length === 3) {\n                h = parseInt(timeStr.substring(0, 1), 10);\n                m = parseInt(timeStr.substring(1, 3), 10);\n            } else if (timeStr.length === 4) {\n                h = parseInt(timeStr.substring(0, 2), 10);\n                m = parseInt(timeStr.substring(2, 4), 10);\n            } else {\n                return null;\n            }\n\n            if (isNaN(h) || isNaN(m) || h < 0 || h > 23 || m < 0 || m > 59) return null;\n\n            const now = new Date();\n            let d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0);\n            \n            if (d.getTime() > now.getTime() + 60000) {\n                d.setDate(d.getDate() - 1);\n            }\n            return d;\n        }\n\n        function parseInputDate(dateStr) {\n            const raw = (dateStr || \'\').trim();\n            const now = new Date();\n            if (!raw) return { year: now.getFullYear(), month: now.getMonth()+1, day: now.getDate() };\n            const m = raw.match(/^(\\d{1,2})\\/(\\d{1,2})\\/(\\d{4})$/);\n            if (!m) return null;\n            const day = parseInt(m[1],10), month = parseInt(m[2],10), year = parseInt(m[3],10);\n            const test = new Date(year, month-1, day);\n            if (test.getFullYear() !== year || test.getMonth() !== month-1 || test.getDate() !== day) return null;\n            return { year, month, day };\n        }\n\n        document.getElementById(\'bossForm\').addEventListener(\'submit\', async (e) => {\n            e.preventDefault();\n            if (!await requireApprovedUser()) return;\n            const bossInput = document.getElementById(\'bossSelect\').value.trim();\n            const timeInput = document.getElementById(\'killTime\').value.trim();\n            const dateInput = document.getElementById(\'killDate\').value.trim();\n            const noticeMin = parseInt(document.getElementById(\'noticeMinutes\').value, 10) || 5;\n            const spTimeMin = parseInt(document.getElementById(\'spTime\').value, 10) || 0;\n            if (!bossInput) return;\n            const parsedDate = parseInputDate(dateInput);\n            if (!parsedDate) { alert(\'❌ วันที่ไม่ถูกต้อง กรุณาใช้รูปแบบ DD/MM/YYYY เช่น 29/08/2026\'); return; }\n            if (timeInput && !parseInputTime(timeInput)) { alert(TRANSLATIONS[currentLang].invalidTimeAlert); return; }\n\n            try {\n                const idToken = await auth.currentUser.getIdToken(true);\n                const apiOrigin = window.SKYNET_API_ORIGIN || \'https://bosstimer-ry18.onrender.com\';\n                const response = await fetch(`${apiOrigin}/api/record-boss`, {\n                    method: \'POST\',\n                    headers: {\'Content-Type\':\'application/json\',\'Authorization\':`Bearer ${idToken}`},\n                    body: JSON.stringify({ bossName: bossInput, killTime: timeInput, killDate: dateInput, noticeMinutes: noticeMin, spTimeMinutes: spTimeMin })\n                });\n                const result = await response.json().catch(() => ({}));\n                if (!response.ok || !result.success) throw new Error(result.error || `HTTP ${response.status}`);\n                document.getElementById(\'bossForm\').reset();\n                document.getElementById(\'noticeMinutes\').value = 5;\n                if (result.bossName) {\n                    activeBosses[result.bossName] = {\n                        spawnTimeMs: Number(result.spawnTimeMs), killTimeMs: Number(result.killTimeMs),\n                        killDate: result.killDate || `${parsedDate.year}-${String(parsedDate.month).padStart(2,\'0\')}-${String(parsedDate.day).padStart(2,\'0\')}`,\n                        noticeMinutes: noticeMin,\n                        notifiedNotice: !!result.alreadyPassed, notifiedSpawn: !!result.alreadyPassed,\n                        recordedBy: result.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || \'ไม่ระบุ\',\n                        recordedByDisplayName: result.recordedByDisplayName || result.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || \'ไม่ระบุ\',\n                        recordedByUserId: result.recordedByUserId || auth.currentUser?.uid || \'\'\n                    };\n                    renderTable();\n                }\n                console.log(`✅ Dashboard boss recorded: ${result.bossName} | by ${result.recordedBy} | confirmation=${result.confirmationRequestId} | voice=${result.confirmationSuccess}`);\n                if (result.confirmationSuccess === false) alert(\'⚠️ บันทึกบอสสำเร็จ แต่ Bot ยังยืนยัน Voice ไม่สำเร็จ กรุณาตรวจห้อง /setvoice และ Render Log\');\n            } catch (err) {\n                console.error(\'Dashboard boss record failed:\', err);\n                alert(`❌ บันทึกเวลาบอสไม่สำเร็จ\\n${err.message || err}`);\n            }\n        });\n\n        async function deleteBoss(bossName) {\n            if (!await requireApprovedUser()) return;\n            if (!bossName) throw new Error(\'ไม่พบชื่อบอสที่ต้องการลบ\');\n            const idToken = await auth.currentUser.getIdToken(true);\n            const apiOrigin = window.SKYNET_API_ORIGIN || \'https://bosstimer-ry18.onrender.com\';\n            const response = await fetch(`${apiOrigin}/api/delete-boss`, {\n                method: \'POST\',\n                headers: {\'Content-Type\':\'application/json\',\'Authorization\':`Bearer ${idToken}`},\n                body: JSON.stringify({ bossName })\n            });\n            const result = await response.json().catch(() => ({}));\n            if (!response.ok || !result.success) throw new Error(result.error || `HTTP ${response.status}`);\n            const resolvedName = result.bossName || bossName;\n            delete activeBosses[resolvedName];\n            delete activeBosses[bossName];\n            renderTable();\n            console.log(`✅ Dashboard boss deleted: ${resolvedName}`);\n        }\n\n        document.getElementById(\'clearAllBtn\').addEventListener(\'click\', async () => {\n            if (!await requireApprovedUser()) return;\n            if (confirm(TRANSLATIONS[currentLang].confirmClear)) {\n                await bossRef.remove();\n            }\n        });\n\n        function renderTable() {\n            const tbody = document.getElementById(\'bossTableBody\');\n            tbody.innerHTML = \'\';\n            const langData = TRANSLATIONS[currentLang];\n            const sortedBosses = Object.keys(activeBosses).sort((a, b) => activeBosses[a].spawnTimeMs - activeBosses[b].spawnTimeMs);\n\n            if (sortedBosses.length === 0) {\n                tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted py-3">${langData.emptyMsg}</td></tr>`;\n                return;\n            }\n\n            sortedBosses.forEach(bossName => {\n                const data = activeBosses[bossName];\n                const tr = document.createElement(\'tr\');\n                const killTimeStr = format24h(new Date(data.killTimeMs));\n                const spawnTimeStr = format24h(new Date(data.spawnTimeMs));\n                \n                tr.innerHTML = `\n                    <td class="fw-bold text-warning">${bossName}</td>\n                    <td>${(data.killDate || new Date(data.killTimeMs).toLocaleDateString(\'th-TH\')).replace(/^(\\d{1,2})\\/(\\d{1,2})\\/(\\d{4})$/, \'$1/$2/$3\')}</td>\n                    <td>${killTimeStr}</td>\n                    <td class="text-info">${spawnTimeStr}</td>\n                    <td id="cd-${bossName}" class="fw-bold">--:--:--</td>\n                    <td>${data.noticeMinutes} ${langData.minUnit}</td>\n                    <td><span class="badge bg-secondary">${data.recordedBy}</span></td>\n                    <td>\n                        <button class="btn btn-sm btn-danger" onclick=\'deleteBoss(${JSON.stringify(bossName)})\'>${langData.btnDelete}</button>\n                    </td>\n                `;\n                tbody.appendChild(tr);\n            });\n            updateCountdowns();\n        }\n\n        function updateCountdowns() {\n            const nowMs = new Date().getTime();\n            document.getElementById(\'liveClockDisplay\').innerText = format24h(new Date());\n\n            Object.keys(activeBosses).forEach(bossName => {\n                const data = activeBosses[bossName];\n                const diffMs = data.spawnTimeMs - nowMs;\n                const cdCell = document.getElementById(`cd-${bossName}`);\n                \n                if (!cdCell) return;\n\n                if (diffMs <= 0) {\n                    cdCell.innerHTML = `<span class="text-success">${TRANSLATIONS[currentLang].spawned}</span>`;\n                    if (!data.notifiedSpawn) {\n                        playAlertSound(\'spawn\');\n                        const title = (TRANSLATIONS[currentLang].spawnNotifyTitle || "⚔️ {boss} Spawned!").replace(\'{boss}\', bossName);\n                        const body = (TRANSLATIONS[currentLang].spawnNotifyBody || "Boss {boss} has spawned!").replace(\'{boss}\', bossName);\n                        sendBrowserNotification(title, body);\n                        bossRef.child(bossName).update({ notifiedSpawn: true });\n                    }\n                } else {\n                    const totalSec = Math.floor(diffMs / 1000);\n                    const h = Math.floor(totalSec / 3600);\n                    const m = Math.floor((totalSec % 3600) / 60);\n                    const s = totalSec % 60;\n                    \n                    cdCell.innerText = `${h.toString().padStart(2, \'0\')}:${m.toString().padStart(2, \'0\')}:${s.toString().padStart(2, \'0\')}`;\n                    \n                    const noticeMs = data.noticeMinutes * 60 * 1000;\n                    if (diffMs <= noticeMs && diffMs > noticeMs - 5000 && !data.notifiedNotice) {\n                         playAlertSound(\'notice\');\n                         const title = (TRANSLATIONS[currentLang].noticeNotifyTitle || "⏳ {boss} Spawning Soon!").replace(\'{boss}\', bossName);\n                         const body = (TRANSLATIONS[currentLang].noticeNotifyBody || "Boss {boss} will spawn in {min} mins").replace(\'{boss}\', bossName).replace(\'{min}\', data.noticeMinutes);\n                         sendBrowserNotification(title, body);\n                         bossRef.child(bossName).update({ notifiedNotice: true });\n                    }\n                }\n            });\n        }\n\n        function initApp() {\n            const dataList = document.getElementById(\'bossOptions\');\n            const allNames = new Set();\n            Object.keys(BOSS_DATABASE).sort().forEach(boss => {\n                allNames.add(boss);\n                const option = document.createElement(\'option\');\n                option.value = boss;\n                dataList.appendChild(option);\n            });\n            db.ref(\'custom_bosses\').on(\'value\', (snap) => {\n                const custom = snap.val() || {};\n                Object.keys(custom).sort().forEach(boss => {\n                    if (allNames.has(boss)) return;\n                    allNames.add(boss);\n                    const option = document.createElement(\'option\');\n                    option.value = boss;\n                    dataList.appendChild(option);\n                });\n            });\n\n            document.getElementById(\'langSelect\').value = currentLang;\n            const authLangSelect = document.getElementById(\'authLangSelect\');\n            if (authLangSelect) authLangSelect.value = currentLang;\n            \n            document.getElementById(\'tzSelect\').value = currentTz;\n            \n            applyLanguage();\n            toggleAttendancePanel(localStorage.getItem(\'attendance_panel_open\') === \'1\');\n            checkAuthSession();\n\n            setInterval(updateCountdowns, 1000);\n            setInterval(() => {\n                if (currentUserData && currentUserData.role === \'admin\') loadAdminUsers().catch(err => console.warn(\'[SKYNET] admin interval:\', err));\n            }, 30000);\n        }\n\n        function toggleAdminPanel(forceOpen=null) {\n            const content=document.getElementById(\'adminPanelContent\');\n            const btn=document.getElementById(\'adminCollapseBtn\');\n            if(!content) return;\n            const shouldOpen = forceOpen === null ? content.style.display !== \'block\' : !!forceOpen;\n            content.style.display = shouldOpen ? \'block\' : \'none\';\n            if(btn) btn.innerText = shouldOpen ? \'▲ ปิด\' : \'▼ เปิด\';\n            localStorage.setItem(\'admin_panel_open\', shouldOpen ? \'1\' : \'0\');\n            if(shouldOpen) loadAdminUsers().catch(()=>{});\n        }\n\n        // V8 FIX: HTML uses inline onclick handlers, while the dashboard is wrapped in an IIFE.\n        // Publish the UI functions explicitly so every button remains callable.\n        Object.assign(window, {\n            openBotSettingsModal,\n            scrollToAdminPanel,\n            openChangeCodeModal,\n            logout,\n            loadAdminUsers,\n            startAdminRealtimeListeners,\n            setUserStatus,\n            changeLanguage,\n            changeTimezone,\n            toggleNotifications,\n            deleteBoss,\n            requireApprovedUser,\n            requireAdmin,\n            toggleAdminPanel,\n            toggleAttendancePanel,\n            toggleAttendanceHistoryPanel,\n            toggleAttendanceMemberMonthlyPanel,\n            toggleAttendanceIndividualPanel\n        });\n\n        // Keep delete failures visible to the user.\n        window.deleteBoss = async function (bossName) {\n            try {\n                await deleteBoss(bossName);\n            } catch (err) {\n                console.error(\'[SKYNET] Delete boss failed:\', err);\n                alert(\'ลบบอสไม่สำเร็จ: \' + (err && err.message ? err.message : err));\n            }\n        };\n\n        window.addEventListener(\'beforeunload\', () => {\n            const sessionId = localStorage.getItem(\'logged_session_id\');\n            if (sessionId) {\n                sessionsRef.child(sessionId).update({\n                    lastSeenAt: new Date().toISOString(),\n                    active: false,\n                    logoutAt: new Date().toISOString()\n                });\n            }\n        });\n\n        window.addEventListener(\'DOMContentLoaded\', initApp);\n        })();\n    </script>\n</body>\n</html>'
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

    web_config = os.environ.get("FIREBASE_WEB_CONFIG_JSON", "").strip()
    try:
        parsed_web_config = json.loads(web_config) if web_config else {}
        if not isinstance(parsed_web_config, dict):
            parsed_web_config = {}
    except Exception:
        parsed_web_config = {}
    return render_template_string(
        HTML_TEMPLATE,
        bosses=boss_list,
        tts_th=tts_th_enabled,
        tts_en=tts_en_enabled,
        tts_ko=tts_ko_enabled,
        firebase_web_config_json=json.dumps(parsed_web_config, ensure_ascii=False)
    )

@app.route('/api/firebase-config.js')
def firebase_config_js():
    """Expose only the Firebase Web SDK config to the browser.
    The config is read from FIREBASE_WEB_CONFIG_JSON; this is public client config,
    not the Firebase Admin service-account secret.
    """
    raw = os.environ.get('FIREBASE_WEB_CONFIG_JSON', '').strip()
    try:
        cfg = json.loads(raw) if raw else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    cfg.setdefault('projectId', 'skynet-3ad44')
    cfg.setdefault('authDomain', 'skynet-3ad44.firebaseapp.com')
    cfg.setdefault('databaseURL', 'https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app')
    payload = json.dumps(cfg, ensure_ascii=False).replace('</', '<\\/')
    response = Response(f'window.SKYNET_FIREBASE_CONFIG = {payload};', mimetype='application/javascript')
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'no-store'
    return response




_attendance_snapshot_last_log_summary = None

# V88: event-driven Attendance cache. Firebase is observed once on the server;
# Dashboard clients receive an update only when raid_attendance changes.
_attendance_cache_lock = threading.Lock()
_attendance_cache_payload = None
_attendance_cache_version = 0
_attendance_stream_clients = set()
_attendance_stream_clients_lock = threading.Lock()


def _build_attendance_payload(root, reports):
    if not isinstance(root, dict):
        root = {}
    if not isinstance(reports, dict):
        reports = {}
    individual_members = {}
    for guild_id, activities in root.items():
        if not isinstance(activities, dict):
            continue
        for activity_id, activity in activities.items():
            if not isinstance(activity, dict):
                continue
            participants = activity.get('participants') or {}
            if not isinstance(participants, dict):
                continue
            attack_at = str(activity.get('attack_at') or activity.get('created_at') or '')
            month_key = attack_at[:7] if len(attack_at) >= 7 and attack_at[4] == '-' and attack_at[7] == '-' else ''
            for participant_key, participant in participants.items():
                if not isinstance(participant, dict):
                    continue
                status = str(participant.get('status') or '').strip().lower()
                if status != 'checked_in':
                    continue
                uid = str(participant.get('user_id') or participant_key or '').strip()
                if not uid:
                    continue
                name = str(participant.get('display_name') or participant.get('username') or uid)
                checked_at = str(participant.get('checked_in_at') or participant.get('checkedAt') or '')
                member = individual_members.setdefault(uid, {'uid': uid, 'name': name, 'entries': []})
                if member.get('name') in ('', uid) and name:
                    member['name'] = name
                member['entries'].append({
                    'monthKey': month_key,
                    'bossName': str(activity.get('boss_name') or '-'),
                    'activityDate': str(activity.get('activity_date') or '-'),
                    'attackTime': str(activity.get('attack_time') or '-'),
                    'checkedAt': checked_at,
                    'status': status,
                    'activityId': str(activity_id),
                    'guildId': str(guild_id),
                })
    for member in individual_members.values():
        member['entries'].sort(key=lambda e: str(e.get('checkedAt') or ''), reverse=True)
    summary = {
        'members': len(individual_members),
        'entries': sum(len(m.get('entries', [])) for m in individual_members.values()),
        'activities': sum(1 for guild_data in root.values() if isinstance(guild_data, dict) for _ in guild_data),
    }
    return {
        'success': True,
        'raid_attendance': root,
        'monthly_reports': reports,
        'individual_members': individual_members,
        'server_time': datetime.now(TZ_THAI).isoformat(),
        'attendance_version': 0,
        'summary': summary,
    }, summary


def _publish_attendance_snapshot(root, reports, *, force=False):
    global _attendance_cache_payload, _attendance_cache_version, _attendance_snapshot_last_log_summary
    payload, summary = _build_attendance_payload(root, reports)
    signature = json.dumps({
        'raid_attendance': payload.get('raid_attendance', {}),
        'monthly_reports': payload.get('monthly_reports', {}),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with _attendance_cache_lock:
        previous_signature = getattr(_publish_attendance_snapshot, '_signature', None)
        if not force and previous_signature == signature and _attendance_cache_payload is not None:
            return False
        _publish_attendance_snapshot._signature = signature
        _attendance_cache_version += 1
        payload['attendance_version'] = _attendance_cache_version
        payload['server_time'] = datetime.now(TZ_THAI).isoformat()
        _attendance_cache_payload = payload
        changed = summary != _attendance_snapshot_last_log_summary
        _attendance_snapshot_last_log_summary = summary
    if changed or force:
        print(
            f"📊 Attendance snapshot changed | members={summary['members']} | entries={summary['entries']} | activities={summary['activities']} | version={payload['attendance_version']}",
            flush=True,
        )
    with _attendance_stream_clients_lock:
        clients = list(_attendance_stream_clients)
    for client_queue in clients:
        try:
            client_queue.put_nowait(payload)
        except Exception:
            pass
    return True


def _get_attendance_cache():
    with _attendance_cache_lock:
        if _attendance_cache_payload is None:
            return None
        return json.loads(json.dumps(_attendance_cache_payload, ensure_ascii=False))


def start_attendance_firebase_listener():
    """One Firebase SSE listener for Attendance; rebuild only on actual Firebase events."""
    def listener(event):
        try:
            root = db.reference('raid_attendance').get() or {}
            reports = db.reference('monthly_reports').get() or {}
            _publish_attendance_snapshot(root, reports)
        except Exception as exc:
            print(f"❌ Firebase Attendance Listener ผิดพลาด: {exc}", flush=True)
    try:
        root = db.reference('raid_attendance').get() or {}
        reports = db.reference('monthly_reports').get() or {}
        _publish_attendance_snapshot(root, reports, force=True)
        db.reference('raid_attendance').listen(listener)
        print("🟢 Firebase Attendance Listener พร้อมทำงานแบบ event-driven", flush=True)
    except Exception as exc:
        print(f"❌ ไม่สามารถเปิด Firebase Attendance Listener ได้: {exc}", flush=True)


@app.route('/api/attendance-data', methods=['GET', 'OPTIONS'])
def attendance_data_api():
    """Return current cached Attendance snapshot; Firebase is not read per request."""
    def _api_json(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        origin = request.headers.get('Origin', '')
        allowed = {
            'https://iahcatan.github.io',
            'https://bosstimer-ry18.onrender.com',
            'http://localhost:5000',
        }
        response.headers['Access-Control-Allow-Origin'] = origin if origin in allowed else 'https://iahcatan.github.io'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Cache-Control'] = 'no-store'
        return response
    if request.method == 'OPTIONS':
        response = _api_json({'success': True})
        response.headers['Access-Control-Max-Age'] = '600'
        return response, 204
    try:
        auth_header = request.headers.get('Authorization', '').strip()
        if not auth_header.lower().startswith('bearer '):
            return _api_json({'success': False, 'error': 'Missing Firebase ID token'}), 401
        id_token = auth_header.split(' ', 1)[1].strip()
        decoded = firebase_auth.verify_id_token(id_token)
        uid = str(decoded.get('uid') or '').strip()
        if not uid:
            return _api_json({'success': False, 'error': 'Invalid Firebase ID token'}), 401
        profile = db.reference(f'users/{uid}').get() or {}
        if not isinstance(profile, dict) or profile.get('status') != 'approved':
            return _api_json({'success': False, 'error': 'Account is not approved'}), 403
        cached = _get_attendance_cache()
        if cached is None:
            root = db.reference('raid_attendance').get() or {}
            reports = db.reference('monthly_reports').get() or {}
            _publish_attendance_snapshot(root, reports, force=True)
            cached = _get_attendance_cache()
        return _api_json(cached or {'success': True, 'raid_attendance': {}, 'monthly_reports': {}, 'individual_members': {}, 'attendance_version': 0})
    except Exception as exc:
        print(f"❌ /api/attendance-data failed: {exc}", flush=True)
        traceback.print_exc()
        return _api_json({'success': False, 'error': str(exc)}), 500


@app.route('/api/attendance-stream', methods=['GET', 'OPTIONS'])
def attendance_stream_api():
    """Authenticated SSE stream. Payloads are emitted only when Attendance changes."""
    origin = request.headers.get('Origin', '')
    allowed = {'https://iahcatan.github.io', 'https://bosstimer-ry18.onrender.com', 'http://localhost:5000'}
    if request.method == 'OPTIONS':
        response = Response(status=204)
        response.headers['Access-Control-Allow-Origin'] = origin if origin in allowed else 'https://iahcatan.github.io'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Access-Control-Max-Age'] = '600'
        return response
    auth_header = request.headers.get('Authorization', '').strip()
    if not auth_header.lower().startswith('bearer '):
        return jsonify({'success': False, 'error': 'Missing Firebase ID token'}), 401
    try:
        id_token = auth_header.split(' ', 1)[1].strip()
        decoded = firebase_auth.verify_id_token(id_token)
        uid = str(decoded.get('uid') or '').strip()
        if not uid:
            return jsonify({'success': False, 'error': 'Invalid Firebase ID token'}), 401
        profile = db.reference(f'users/{uid}').get() or {}
        if not isinstance(profile, dict) or profile.get('status') != 'approved':
            return jsonify({'success': False, 'error': 'Account is not approved'}), 403
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 401

    client_queue = queue.Queue(maxsize=4)
    with _attendance_stream_clients_lock:
        _attendance_stream_clients.add(client_queue)
    current = _get_attendance_cache()

    def generate():
        try:
            if current is not None:
                yield f"data: {json.dumps(current, ensure_ascii=False, separators=(',', ':'))}\n\n"
            while True:
                try:
                    payload = client_queue.get(timeout=25)
                    yield f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _attendance_stream_clients_lock:
                _attendance_stream_clients.discard(client_queue)
    response = Response(generate(), mimetype='text/event-stream')
    origin = request.headers.get('Origin', '')
    allowed = {'https://iahcatan.github.io', 'https://bosstimer-ry18.onrender.com', 'http://localhost:5000'}
    response.headers['Access-Control-Allow-Origin'] = origin if origin in allowed else 'https://iahcatan.github.io'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
    response.headers['Cache-Control'] = 'no-cache, no-transform'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Vary'] = 'Origin'
    print('🟢 Attendance SSE response ready | Waitress-safe headers only', flush=True)
    return response


@app.route('/api/delete-boss', methods=['POST', 'OPTIONS'])
def delete_boss_api():
    """Delete one Boss Timer record from Dashboard using Firebase Admin SDK."""
    def _api_json(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        response.headers['Access-Control-Allow-Origin'] = 'https://iahcatan.github.io'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Type'
        return response
    if request.method == 'OPTIONS':
        response = _api_json({'success': True})
        response.headers['Access-Control-Max-Age'] = '600'
        return response, 204
    try:
        payload = request.get_json(silent=True) or {}
        auth_header = request.headers.get('Authorization', '').strip()
        if not auth_header.lower().startswith('bearer '):
            return _api_json({'success': False, 'error': 'Missing Firebase ID token'}), 401
        id_token = auth_header.split(' ', 1)[1].strip()
        decoded = firebase_auth.verify_id_token(id_token)
        uid = str(decoded.get('uid') or '').strip()
        if not uid:
            return _api_json({'success': False, 'error': 'Invalid Firebase ID token'}), 401
        profile = db.reference(f'users/{uid}').get() or {}
        if not isinstance(profile, dict) or profile.get('status') != 'approved':
            return _api_json({'success': False, 'error': 'Account is not approved'}), 403
        boss_name = str(payload.get('bossName') or '').strip()
        if not boss_name:
            return _api_json({'success': False, 'error': 'Boss name is required'}), 400
        # Do not depend on the in-memory boss_schedule cache here. A Firebase SSE listener can
        # temporarily disconnect (for example SSL EOF) while the REST/Admin SDK remains usable.
        # Read the authoritative root and resolve the exact Firebase key before deleting.
        root = None
        try:
            root = db.reference('boss_schedule').get() or {}
        except Exception as read_exc:
            print(f"⚠️ DASHBOARD DELETE READ FAILED | boss={boss_name} | {read_exc}", flush=True)
            raise
        matched_key = None
        if isinstance(root, dict):
            for key in root.keys():
                if str(key).casefold() == boss_name.casefold():
                    matched_key = key
                    break
        if matched_key is None:
            return _api_json({'success': False, 'error': f'ไม่พบบอส `{boss_name}` ใน boss_schedule'}), 404
        target_key = matched_key
        print(f"🗑️ DASHBOARD DELETE REQUEST | boss={target_key} | uid={uid}", flush=True)
        # Mirror the proven /delboss mutation semantics while pausing the root listener
        # so a stale SSE snapshot cannot reinsert this record during deletion.
        global is_updating_from_bot
        is_updating_from_bot = True
        try:
            db.reference(f'boss_schedule/{target_key}').delete()
            with schedule_lock:
                boss_schedule.pop(target_key, None)
            remaining = {}
            with schedule_lock:
                for name, data in boss_schedule.items():
                    try:
                        remaining[name] = _schedule_record_to_firebase(name, data)
                    except Exception as build_exc:
                        print(f"⚠️ DASHBOARD DELETE skip serialization | boss={name}: {build_exc}", flush=True)
            db.reference('boss_schedule').set(remaining)
        finally:
            is_updating_from_bot = False
        print(f"✅ DASHBOARD DELETE COMPLETE | boss={target_key} | uid={uid}", flush=True)
        return _api_json({'success': True, 'bossName': target_key})
    except Exception as exc:
        print(f"❌ /api/delete-boss failed: {exc}", flush=True)
        traceback.print_exc()
        return _api_json({'success': False, 'error': str(exc)}), 500


@app.route('/api/record-boss', methods=['POST', 'OPTIONS'])
def record_boss_api():
    if request.method == 'OPTIONS':
        response = jsonify({'success': True})
        response.headers['Access-Control-Allow-Origin'] = 'https://iahcatan.github.io'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Access-Control-Max-Age'] = '600'
        return response, 204
    """Authenticated Dashboard boss recording endpoint.
    Saves one canonical boss_schedule record using Firebase Admin SDK and triggers
    the one-shot Voice confirmation from the same server process.
    """
    def _api_json(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        response.headers['Access-Control-Allow-Origin'] = 'https://iahcatan.github.io'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Type'
        return response

    try:
        payload = request.get_json(silent=True) or {}
        auth_header = request.headers.get('Authorization', '').strip()
        if not auth_header.lower().startswith('bearer '):
            return _api_json({'success': False, 'error': 'Missing Firebase ID token'}), 401
        id_token = auth_header.split(' ', 1)[1].strip()
        decoded = firebase_auth.verify_id_token(id_token)
        uid = str(decoded.get('uid') or '').strip()
        if not uid:
            return _api_json({'success': False, 'error': 'Invalid Firebase ID token'}), 401

        profile = db.reference(f'users/{uid}').get() or {}
        if not isinstance(profile, dict):
            return _api_json({'success': False, 'error': 'User profile not found'}), 403
        if profile.get('status') != 'approved':
            return _api_json({'success': False, 'error': 'Account is not approved'}), 403

        boss_name = str(payload.get('bossName') or '').strip()
        time_input = str(payload.get('killTime') or '').strip()
        date_input = str(payload.get('killDate') or '').strip()
        try:
            notice_min = max(1, int(payload.get('noticeMinutes') or 5))
        except (TypeError, ValueError):
            notice_min = 5
        try:
            sp_time_min = max(0, int(payload.get('spTimeMinutes') or 0))
        except (TypeError, ValueError):
            sp_time_min = 0

        if not boss_name:
            return _api_json({'success': False, 'error': 'Boss name is required'}), 400

        canonical_name = get_boss_canonical_name(boss_name)
        # Dashboard uses BOSS_DATABASE for custom definitions, but the server must
        # use the persisted BOSS_RESPAWN_TIMES so /add boss survives deploys.
        respawn = get_boss_respawn_time(canonical_name)
        now = datetime.now(TZ_THAI)
        try:
            if date_input:
                date_match = re.fullmatch(r'\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*', date_input)
                if not date_match:
                    raise ValueError('Invalid date format')
                day, month, year = map(int, date_match.groups())
                selected_date = datetime(year, month, day, tzinfo=TZ_THAI)
                if time_input:
                    parsed_time = parse_time_input(time_input, now)
                    boss_died_at = datetime(year, month, day, parsed_time.hour, parsed_time.minute, parsed_time.second, tzinfo=TZ_THAI)
                else:
                    boss_died_at = datetime(year, month, day, now.hour, now.minute, now.second, tzinfo=TZ_THAI)
            else:
                boss_died_at = parse_time_input(time_input, now)
        except ValueError:
            return _api_json({'success': False, 'error': 'Invalid date/time format. Use DD/MM/YYYY and HH:MM'}), 400

        next_spawn = boss_died_at + respawn + timedelta(minutes=sp_time_min)
        spawn_ms = int(next_spawn.timestamp() * 1000)
        kill_ms = int(boss_died_at.timestamp() * 1000)
        already_passed = spawn_ms <= int(time.time() * 1000)
        username = str(profile.get('username') or decoded.get('email') or uid).strip()
        if not username or username.lower() in {'unknown','unknow','undefined','null'}:
            username = str(decoded.get('email') or uid).strip() or 'สมาชิก'
        print(f"📥 DASHBOARD RECORD REQUEST | boss={boss_name} | uid={uid} | username={username}")
        request_id = uuid.uuid4().hex
        requested_at = datetime.now(TZ_THAI).isoformat()

        record = {
            'killTimeMs': kill_ms,
            'killDate': boss_died_at.strftime('%Y-%m-%d'),
            'spawnTimeMs': spawn_ms,
            'spawn_time': next_spawn.isoformat(),
            'noticeMinutes': notice_min,
            'recordedBy': username,
            'recordedByDisplayName': username,
            'recordedByUserId': uid,
            'confirmationRequestId': request_id,
            'confirmationRequestedAt': requested_at,
            'confirmationStatus': 'pending',
            'confirmationSource': 'dashboard',
            'notifiedNotice': already_passed,
            'notifiedSpawn': already_passed,
            'voiceNoticeSent': already_passed,
            'voiceSpawnSent': already_passed,
            'channelId': payload.get('channelId')
        }
        if record['channelId'] is None:
            record.pop('channelId')

        ref = db.reference(f'boss_schedule/{canonical_name}')
        ref.set(record)
        print(f"✅ Dashboard Firebase save complete | boss={canonical_name} | request={request_id} | user={username}")
        with schedule_lock:
            boss_schedule[canonical_name] = {
                'spawn_time': next_spawn,
                'killTimeMs': kill_ms,
                'channel_id': payload.get('channelId'),
                'notified_advance': already_passed,
                'notified_spawn': already_passed,
                'voice_notice_sent': already_passed,
                'voice_spawn_sent': already_passed,
                'noticeMinutes': notice_min,
                'recorded_by': username,
                'recordedByDisplayName': username,
                'recordedByUserId': uid,
                'confirmationRequestId': request_id,
                'confirmationRequestedAt': requested_at,
                'confirmationStatus': 'pending',
                'confirmationSource': 'dashboard'
            }

        # V57 runtime split: the Render web service must never touch Discord Gateway/REST.
        # It only persists the pending confirmation to Firebase. The external bot runtime
        # observes boss_schedule/{boss} and performs the existing Voice/TTS confirmation.
        confirmation_result = False
        confirmation_pending = False
        if SKYNET_RUNTIME_ROLE == "web":
            confirmation_pending = True
            confirmation_result = True  # Save accepted; external bot will process it.
            print(
                f"⏳ Dashboard Voice confirmation delegated to external bot runtime | "
                f"boss={canonical_name} | request={request_id}",
                flush=True,
            )
        else:
            # Bot runtime keeps the original immediate queue behavior. Firebase listener
            # remains the safety net and preserves the existing confirmation flow.
            try:
                confirmation_result = queue_voice_confirmation(
                    canonical_name, dict(boss_schedule[canonical_name]), source='dashboard', wait=True, timeout=180
                )
                if confirmation_result is None:
                    confirmation_pending = True
                    confirmation_result = True  # UI compatibility: save accepted, Voice pending.
            except Exception as exc:
                print(f"⚠️ Dashboard confirmation queue failed: {exc}")

        print(
            f"📣 Dashboard voice confirmation result | boss={canonical_name} "
            f"| success={bool(confirmation_result)} | pending={confirmation_pending}"
        )

        return _api_json({
            'success': True,
            'bossName': canonical_name,
            'recordedBy': username,
            'recordedByDisplayName': username,
            'recordedByUserId': uid,
            'killTimeMs': kill_ms,
            'spawnTimeMs': spawn_ms,
            'spawnTime': next_spawn.isoformat(),
            'confirmationRequestId': request_id,
            'confirmationSuccess': bool(confirmation_result),
            'confirmationPending': confirmation_pending,
            'confirmationStatus': 'pending' if confirmation_pending else ('sent' if confirmation_result else 'failed'),
            'alreadyPassed': already_passed
        })
    except Exception as exc:
        print(f"❌ /api/record-boss failed: {exc}")
        traceback.print_exc()
        return _api_json({'success': False, 'error': str(exc)}), 500

@app.route('/api/record-boss/health', methods=['GET'])
def record_boss_health():
    return jsonify({
        'success': True,
        'bot_ready': bool(is_bot_ready),
        'event_loop_ready': bool(bot_event_loop is not None),
        'guild_count': len(bot.guilds),
        'voice_config_servers': len(voice_config),
        'voice_targets': sum(len(cfg.get('channels', {})) for cfg in voice_config.values() if isinstance(cfg, dict)),
        'runtime_role': SKYNET_RUNTIME_ROLE,
        'discord_transport': 'disabled' if SKYNET_RUNTIME_ROLE == 'web' else 'direct',
    })

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

    if SKYNET_RUNTIME_ROLE == "web":
        try:
            key = {"th": "tts_th_enabled", "en": "tts_en_enabled", "ko": "tts_ko_enabled"}[lang]
            db.reference("bot_settings").update({key: bool(enabled)})
        except Exception as exc:
            print(f"⚠️ Web runtime TTS setting persist failed: {exc!r}", flush=True)
            return jsonify({"success": False, "error": "Failed to persist TTS setting"}), 500
    elif is_bot_ready and bot.loop and bot.loop.is_running():
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
DEFAULT_TARGET_ROLE_NAMES = ["Eternal", "Meaw", "Anti"]
env_target_role_names = os.environ.get("TARGET_ROLE_NAMES", "")
TARGET_ROLE_NAMES = [x.strip() for x in env_target_role_names.split(",") if x.strip()] if env_target_role_names else DEFAULT_TARGET_ROLE_NAMES

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
# V71/V72: Persistent Discord text-notification target channels.
# Shape: {guild_id: {channel_id: {guild_id, channel_id, channel_name, enabled, ...}}}
notification_channels = {}

attendance_config = {}  # guild_id -> {summary_channel_id, ...}

# Bosses that must NOT create automatic Attendance activities.
# Comparison is case-insensitive and ignores surrounding whitespace.
AUTO_ATTENDANCE_EXCLUDED_BOSSES = {
    name.casefold() for name in (
        "Elemental Queen",
        "Tank",
        "Swirl Flame",
        "Maelstrom",
        "Twister",
        "Chief Magief",
        "Apapa",
        "Corrupt Forest Keeper",
        "Recluse",
        "Blackskull",
        "Sleepy Kooii",
        "Awaken Kooii",
        "Eeheehee",
        "Ooheeheek",
        "Oohehe",
        "Guardian Imp",
        "Blackjuno",
        "Blacksky",
        "Red Fox",
        "7tailfox",
        "777Tailfox",
        "Sunrise Flower",
        "Magma Senior Thief",
        "Bbinikjoe",
        "Bigmouse",
        "Poison Root Flower",
        "Contaminated Queen Bee",
        "Rotten Pudding",
        "Swamp Flower Monster",
        "Glucose",
        "Overload",
        "Shaaack",
        "Suuuk",
        "Sususuk",
        "sandgrave",
        "Elder Beholder",
    )
}

def is_auto_attendance_excluded_boss(boss_name: str) -> bool:
    return str(boss_name or "").strip().casefold() in AUTO_ATTENDANCE_EXCLUDED_BOSSES
attendance_lifecycle_lock = asyncio.Lock()
custom_bosses = {}
last_voice_connect_attempt = {}
last_channel_fetch_attempt = {}
# Prevent overlapping boss notification passes (e.g. scheduled loop + manual /kill check).
boss_notification_pass_lock = asyncio.Lock()

# V58: minimal background text REST queue. Voice/TTS remains independent.
PENDING_BOSS_REST_MAX = 100
pending_boss_rest_notifications = deque(maxlen=PENDING_BOSS_REST_MAX)
pending_boss_rest_lock = threading.Lock()
pending_boss_rest_keys = set()

# =========================================================
# 🛡️ V22: GLOBAL DISCORD REST GUARD
# =========================================================
# One central guard is used by Boss/BF/Library/Live Embed/Audit and command
# follow-up REST calls.  The guard never retries a failed request immediately.
# It records Discord Retry-After, serializes REST calls, spaces requests, and
# temporarily suppresses non-essential REST calls while Discord is blocking us.
# Voice/TTS is deliberately NOT routed through this guard.

class DiscordRESTCooldown(Exception):
    """Internal signal: a non-essential REST call was skipped during cooldown."""


discord_rest_rate_limited_until = 0.0
discord_rest_backoff_seconds = 60.0
discord_rest_last_429_log = 0.0
discord_rest_last_error_log = 0.0
discord_rest_next_call_at = 0.0
discord_rest_min_interval = max(0.0, float(os.environ.get("DISCORD_REST_MIN_INTERVAL", "0.20")))
discord_rest_guard_lock = asyncio.Lock()

# V58: separate non-essential background REST from the foreground command lane.
# Background boss text notifications and audit logs are queued and rate-limited independently.
# Voice/TTS never enters this lane.
discord_background_rest_next_call_at = 0.0
discord_background_rest_min_interval = max(1.0, float(os.environ.get("DISCORD_BACKGROUND_REST_MIN_INTERVAL", "5.0")))
discord_background_rest_recovery_grace_seconds = max(
    60.0, float(os.environ.get("DISCORD_BACKGROUND_REST_RECOVERY_GRACE", "300"))
)
discord_background_rest_suppressed_until = 0.0
discord_background_rest_quarantined = False
# Hold all non-essential background Discord REST until the first successful
# guild command sync after READY. This closes the startup race where on_ready
# notification tasks can begin before start.py completes tree.sync().
discord_background_rest_startup_hold = True

discord_background_rest_context_prefixes = (
    "boss-notify:",
    "audit:",
    "bf:",
    "library-boss",
    "live:",
)

# 🔎 V51 diagnostics: distinguish Discord-provided timers from our own local
# safety backoff. A local backoff is NOT presented as the real Discord block time.
discord_block_started_at = 0.0
discord_block_last_seen_at = 0.0
discord_block_server_until = 0.0
discord_block_server_retry_after = 0.0
discord_block_scope = ""
discord_block_global = False
discord_block_kind = ""
discord_block_source = ""
discord_block_last_log_at = 0.0
discord_block_temp_restriction = False
discord_block_next_probe_mono = 0.0
discord_block_local_retry_seconds = 60.0
discord_block_recovery_grace_seconds = max(5.0, float(os.environ.get("DISCORD_BLOCK_RECOVERY_GRACE", "15")))
discord_block_recovery_until_mono = 0.0

# V53: Persist the REST restriction state across process/Gateway restarts.
# Firebase is the durable source on Render; SQLite is kept as a local fallback
# for same-container restarts. This state is only written on block/clear events,
# never on the 15-second diagnostics heartbeat.
DISCORD_REST_BLOCK_FIREBASE_PATH = "app_settings/discord_rest_block"
discord_block_persist_lock = asyncio.Lock()
discord_block_persist_task = None
discord_block_persist_dirty = False

discord_block_lock = threading.Lock()


def _discord_rest_rate_limit_remaining() -> float:
    return max(0.0, discord_rest_rate_limited_until - time.monotonic())


def _extract_discord_rate_limit_metadata(exc: Exception) -> dict:
    """Extract only evidence actually returned by Discord."""
    retry_after = 0.0
    reset_after = 0.0
    scope = ""
    is_global = False
    message = ""
    headers = {}
    response = getattr(exc, "response", None)
    try:
        headers = getattr(response, "headers", None) or {}
        raw_retry = headers.get("Retry-After") or headers.get("retry-after")
        if raw_retry is not None:
            retry_after = max(0.0, float(raw_retry))
    except (TypeError, ValueError):
        pass
    try:
        raw_reset = headers.get("X-RateLimit-Reset-After") or headers.get("x-ratelimit-reset-after")
        if raw_reset is not None:
            reset_after = max(0.0, float(raw_reset))
    except (TypeError, ValueError):
        pass
    try:
        scope = str(headers.get("X-RateLimit-Scope") or headers.get("x-ratelimit-scope") or "")
        is_global = str(headers.get("X-RateLimit-Global") or headers.get("x-ratelimit-global") or "").lower() == "true"
    except Exception:
        pass
    try:
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            if retry_after <= 0 and data.get("retry_after") is not None:
                retry_after = max(0.0, float(data.get("retry_after") or 0))
            if data.get("message") is not None:
                message = str(data.get("message"))
            if bool(data.get("global")):
                is_global = True
    except (TypeError, ValueError):
        pass
    if not message:
        try:
            message = str(exc)
        except Exception:
            message = ""
    return {
        "retry_after": retry_after,
        "reset_after": reset_after,
        "scope": scope,
        "global": is_global,
        "message": message,
    }


def _extract_discord_retry_after(exc: Exception) -> float:
    return float(_extract_discord_rate_limit_metadata(exc).get("retry_after") or 0.0)


def _discord_block_state_snapshot() -> dict:
    """Return a JSON-safe snapshot of the current Discord REST restriction state."""
    now_wall = time.time()
    now_mono = time.monotonic()
    with discord_block_lock:
        started = float(discord_block_started_at or 0.0)
        last_seen = float(discord_block_last_seen_at or 0.0)
        server_until = float(discord_block_server_until or 0.0)
        server_retry_after = float(discord_block_server_retry_after or 0.0)
        scope = str(discord_block_scope or "")
        is_global = bool(discord_block_global)
        kind = str(discord_block_kind or "")
        source = str(discord_block_source or "")
        temp_restriction = bool(discord_block_temp_restriction)
        next_probe_mono = float(discord_block_next_probe_mono or 0.0)
        local_retry = float(discord_block_local_retry_seconds or 60.0)

    next_probe_remaining = max(0.0, next_probe_mono - now_mono) if next_probe_mono > 0 else 0.0
    # If the 429 evidence already contains an authoritative server expiry but the
    # local probe deadline has not been computed yet, persist a safe recovery point
    # derived from that server expiry. This closes the tiny record-before-apply gap.
    if next_probe_remaining <= 0 and temp_restriction and server_until > now_wall:
        next_probe_remaining = max(0.0, (server_until - now_wall) + discord_block_recovery_grace_seconds)
    elif next_probe_remaining <= 0 and temp_restriction and server_until <= 0:
        # Interaction 429s may provide no usable expiry header. Persist the
        # current local safety retry so a process restart still keeps the gate closed.
        next_probe_remaining = max(60.0, local_retry)
    # Persist wall-clock time, not monotonic time, because monotonic clocks reset
    # when the Python process restarts.
    next_probe_at = now_wall + next_probe_remaining if next_probe_remaining > 0 else 0.0
    return {
        "version": 1,
        "updated_at": now_wall,
        "started_at": started,
        "last_seen_at": last_seen,
        "server_until": server_until,
        "server_retry_after": server_retry_after,
        "next_probe_at": next_probe_at,
        "scope": scope,
        "global": is_global,
        "kind": kind,
        "source": source,
        "temp_restriction": temp_restriction,
        "local_retry_seconds": local_retry,
        "updated_at": time.time(),
    }


def _schedule_persist_discord_block_state(reason: str):
    """Persist the latest restriction snapshot without losing later state changes."""
    global discord_block_persist_task, discord_block_persist_dirty
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if discord_block_persist_task and not discord_block_persist_task.done():
        # A persist is already in flight. Mark it dirty so the final/latest state
        # is written again after the current write completes.
        discord_block_persist_dirty = True
        return
    discord_block_persist_dirty = False
    discord_block_persist_task = loop.create_task(
        _persist_discord_block_state_async(reason=reason),
        name="discord-rest-block-persist",
    )


async def _persist_discord_block_state_async(*, reason: str):
    """Persist the latest complete restriction state to SQLite + durable Firebase."""
    global discord_block_persist_task, discord_block_persist_dirty

    try:
        while True:
            # Snapshot only after all fields for the current event have been updated.
            state = _discord_block_state_snapshot()
            if state.get("started_at", 0.0) <= 0:
                return

            async with discord_block_persist_lock:
                # SQLite is the local fallback for process restarts.
                try:
                    set_db_value("discord_rest_block", state)
                except Exception as exc:
                    print(f"⚠️ Discord REST block SQLite persist failed: {exc!r}", flush=True)

                # Firebase is the durable cross-restart/deploy copy.
                firebase_saved = False
                last_exc = None
                for attempt in range(1, 4):
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                db.reference(DISCORD_REST_BLOCK_FIREBASE_PATH).set,
                                state,
                            ),
                            timeout=5.0,
                        )
                        firebase_saved = True
                        break
                    except Exception as exc:
                        last_exc = exc
                        if attempt < 3:
                            await asyncio.sleep(float(attempt))

                if not firebase_saved:
                    print(
                        f"⚠️ Discord REST block Firebase persist failed after 3 attempts | "
                        f"reason={reason}: {last_exc!r}",
                        flush=True,
                    )

            # If a later 429/observation changed the state while we were writing,
            # do one more write using the newest snapshot. This closes the V53
            # race where the first queued write could capture pre-_apply state.
            if discord_block_persist_dirty:
                discord_block_persist_dirty = False
                continue
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"⚠️ Discord REST block persistence failed | reason={reason}: {exc!r}", flush=True)
    finally:
        # Only clear the task pointer when no newer writer has replaced/marked it.
        if discord_block_persist_task is asyncio.current_task():
            discord_block_persist_task = None
            if discord_block_persist_dirty:
                discord_block_persist_dirty = False
                try:
                    loop = asyncio.get_running_loop()
                    discord_block_persist_task = loop.create_task(
                        _persist_discord_block_state_async(reason=f"followup:{reason}"),
                        name="discord-rest-block-persist",
                    )
                except RuntimeError:
                    discord_block_persist_task = None


async def _clear_persisted_discord_block_state(*, reason: str):
    """Remove the persisted restriction only after a confirmed success and only
    when no newer block has already been observed."""
    with discord_block_lock:
        # A new 429 may have happened after the success that scheduled this clear.
        # Never let an older clear delete the newer persisted restriction.
        if discord_block_started_at > 0:
            return
    try:
        async with discord_block_persist_lock:
            try:
                set_db_value("discord_rest_block", None)
            except Exception as exc:
                print(f"⚠️ Discord REST block SQLite clear failed: {exc!r}", flush=True)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        db.reference(DISCORD_REST_BLOCK_FIREBASE_PATH).delete,
                    ),
                    timeout=5.0,
                )
            except Exception as exc:
                print(f"⚠️ Discord REST block Firebase clear failed | reason={reason}: {exc!r}", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"⚠️ Discord REST block persistence clear failed | reason={reason}: {exc!r}", flush=True)


async def restore_persisted_discord_block_state():
    """Restore a still-active Discord REST restriction after a process restart."""
    global discord_block_started_at, discord_block_last_seen_at
    global discord_rest_rate_limited_until
    global discord_block_server_until, discord_block_server_retry_after
    global discord_block_scope, discord_block_global, discord_block_kind, discord_block_source
    global discord_block_temp_restriction, discord_block_next_probe_mono, discord_block_local_retry_seconds
    now_wall = time.time()
    state = None

    # Prefer durable Firebase state. If unavailable, use the local SQLite copy.
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(db.reference(DISCORD_REST_BLOCK_FIREBASE_PATH).get),
            timeout=5.0,
        )
        if isinstance(raw, dict) and raw.get("started_at"):
            state = raw
    except Exception as exc:
        print(f"⚠️ Discord REST block Firebase restore unavailable: {exc!r}", flush=True)

    if state is None:
        try:
            raw = get_db_value("discord_rest_block", None)
            if isinstance(raw, dict) and raw.get("started_at"):
                state = raw
        except Exception as exc:
            print(f"⚠️ Discord REST block SQLite restore unavailable: {exc!r}", flush=True)

    if not isinstance(state, dict):
        print("🟢 Discord REST startup gate: no persisted restriction found", flush=True)
        return False

    try:
        started = float(state.get("started_at") or 0.0)
        last_seen = float(state.get("last_seen_at") or started)
        server_until = float(state.get("server_until") or 0.0)
        server_retry_after = float(state.get("server_retry_after") or 0.0)
        next_probe_at = float(state.get("next_probe_at") or 0.0)
        scope = str(state.get("scope") or "")
        is_global = bool(state.get("global"))
        kind = str(state.get("kind") or "")
        source = str(state.get("source") or "")
        temp_restriction = bool(state.get("temp_restriction"))
        local_retry = max(60.0, float(state.get("local_retry_seconds") or 60.0))
        updated_at = float(state.get("updated_at") or last_seen or started)
    except (TypeError, ValueError) as exc:
        print(f"⚠️ Discord REST block persisted state invalid; ignoring it: {exc!r}", flush=True)
        return False

    # An old/stale record must never permanently suppress REST.
    # For temporary restrictions, Discord's supplied server expiry is authoritative.
    # When an old record has expired, keep only a short recovery grace window so
    # the first request after restart is not fired exactly on the boundary.
    server_remaining = max(0.0, server_until - now_wall) if server_until > 0 else 0.0
    persisted_probe_remaining = max(0.0, next_probe_at - now_wall) if next_probe_at > 0 else 0.0

    if temp_restriction and server_until > 0:
        if server_remaining > 0 and persisted_probe_remaining <= 0:
            # The server expiry is authoritative; add the existing recovery grace.
            persisted_probe_remaining = server_remaining + discord_block_recovery_grace_seconds
        elif server_remaining <= 0 and persisted_probe_remaining <= 0:
            persisted_probe_remaining = discord_block_recovery_grace_seconds
    elif temp_restriction and persisted_probe_remaining <= 0:
        # No server timer survived (for example an interaction 429). Use the
        # persisted local retry as the minimum startup hold instead of probing immediately.
        persisted_probe_remaining = max(60.0, local_retry)

    if server_until > 0 and server_remaining <= 0 and persisted_probe_remaining <= 0:
        grace = discord_block_recovery_grace_seconds if temp_restriction else 0.0
        persisted_probe_remaining = max(0.0, grace)

    if started <= 0 or (server_until <= 0 and persisted_probe_remaining <= 0 and not temp_restriction):
        print("🟢 Discord REST startup gate: persisted restriction already expired", flush=True)
        return False

    with discord_block_lock:
        discord_block_started_at = started
        discord_block_last_seen_at = last_seen
        discord_block_server_until = server_until
        discord_block_server_retry_after = server_retry_after
        discord_block_scope = scope
        discord_block_global = is_global
        discord_block_kind = kind or ("API_TEMPORARY_RESTRICTION" if temp_restriction else "RATE_LIMIT")
        discord_block_source = source or "PERSISTED"
        discord_block_temp_restriction = temp_restriction
        discord_block_local_retry_seconds = local_retry
        discord_block_next_probe_mono = time.monotonic() + max(0.0, persisted_probe_remaining)

    # Recreate the in-memory REST cooldown as well. Monotonic deadlines cannot be
    # persisted directly, so they are reconstructed from the wall-clock evidence.
    restored_pause = max(server_remaining, persisted_probe_remaining)
    if restored_pause > 0:
        discord_rest_rate_limited_until = max(
            discord_rest_rate_limited_until,
            time.monotonic() + restored_pause,
        )

    probe_remaining = max(0.0, persisted_probe_remaining)
    server_remaining = max(0.0, server_until - time.time()) if server_until > 0 else 0.0
    print(
        f"🛡️ Discord REST startup recovery gate RESTORED | kind={kind or '-'} | "
        f"source={source or 'PERSISTED'} | server_remaining={_format_duration(server_remaining) if server_remaining else 'expired/unknown'} | "
        f"gate_hold={_format_duration(probe_remaining) if probe_remaining > 0 else 'READY'} | "
        f"expiry={datetime.fromtimestamp(server_until, tz=TZ_THAI).strftime('%d/%m/%Y %H:%M:%S %Z') if server_until > 0 else 'UNKNOWN'} | "
        f"persisted_age={_format_duration(max(0.0, now_wall - updated_at))}",
        flush=True,
    )
    return True


async def wait_for_discord_rest_startup_gate(*, context: str):
    """Wait without making HTTP until a persisted REST restriction is safe to probe."""
    while True:
        now_mono = time.monotonic()
        with discord_block_lock:
            next_probe = discord_block_next_probe_mono
            block_active = discord_block_started_at > 0
        remaining = max(0.0, next_probe - now_mono) if block_active and next_probe > 0 else 0.0
        if remaining <= 0:
            return True
        print(
            f"⏳ Discord REST startup gate waiting | context={context} | remaining={remaining:.1f}s | no HTTP sent",
            flush=True,
        )
        await asyncio.sleep(min(15.0, max(1.0, remaining)))


def _record_discord_block_observed(exc: Exception, *, context: str, source: str = "REST") -> dict:
    """Record evidence of an API restriction without inventing an expiry time."""
    global discord_block_started_at, discord_block_last_seen_at
    global discord_block_server_until, discord_block_server_retry_after
    global discord_block_scope, discord_block_global, discord_block_kind, discord_block_source
    global discord_block_temp_restriction

    meta = _extract_discord_rate_limit_metadata(exc)
    now_wall = time.time()
    with discord_block_lock:
        if discord_block_started_at <= 0:
            discord_block_started_at = now_wall
        discord_block_last_seen_at = now_wall
        if meta["retry_after"] > 0:
            candidate_until = now_wall + meta["retry_after"]
            discord_block_server_until = max(discord_block_server_until, candidate_until)
            discord_block_server_retry_after = meta["retry_after"]
        elif meta["reset_after"] > 0:
            candidate_until = now_wall + meta["reset_after"]
            discord_block_server_until = max(discord_block_server_until, candidate_until)
            discord_block_server_retry_after = meta["reset_after"]
        discord_block_scope = meta["scope"] or discord_block_scope
        discord_block_global = bool(meta["global"] or discord_block_global)
        text = meta["message"].lower()
        if "blocked from accessing our api" in text or ("temporarily" in text and "api" in text):
            discord_block_kind = "API_TEMPORARY_RESTRICTION"
            discord_block_temp_restriction = True
        elif discord_block_global:
            discord_block_kind = "GLOBAL_RATE_LIMIT"
        elif meta["scope"]:
            discord_block_kind = f"RATE_LIMIT_{meta['scope'].upper()}"
        else:
            discord_block_kind = "RATE_LIMIT"
        discord_block_source = source

    return meta


def _format_duration(total_seconds: float) -> str:
    total = max(0, int(round(total_seconds)))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def _discord_block_diagnostic_line() -> str:
    now_wall = time.time()
    with discord_block_lock:
        started = discord_block_started_at
        last_seen = discord_block_last_seen_at
        server_until = discord_block_server_until
        next_probe_mono = discord_block_next_probe_mono
        temp_restriction = discord_block_temp_restriction
        scope = discord_block_scope or "-"
        is_global = discord_block_global
        kind = discord_block_kind or "-"
        source = discord_block_source or "-"
    if started <= 0:
        return "✅ Discord API restriction: NONE OBSERVED"
    elapsed = max(0.0, now_wall - started)
    now_mono = time.monotonic()
    server_remaining = max(0.0, server_until - now_wall) if server_until > 0 else 0.0
    next_probe_remaining = max(0.0, next_probe_mono - now_mono) if next_probe_mono > 0 else 0.0
    if server_until > now_wall:
        remaining_text = f"server-provided remaining={_format_duration(server_remaining)}"
        expiry_text = datetime.fromtimestamp(server_until, tz=TZ_THAI).strftime("%d/%m/%Y %H:%M:%S %Z")
        certainty = "EXACT TIMER FROM DISCORD"
    elif server_until > 0 and last_seen > 0 and now_wall >= server_until:
        remaining_text = (f"server timer expired; safe recovery hold remaining={_format_duration(next_probe_remaining)}" if temp_restriction and next_probe_remaining > 0 else "server timer expired; waiting for a successful request to confirm clear")
        expiry_text = datetime.fromtimestamp(server_until, tz=TZ_THAI).strftime("%d/%m/%Y %H:%M:%S %Z")
        certainty = "SERVER TIMER EXPIRED — CLEAR NOT YET CONFIRMED"
    else:
        remaining_text = "exact remaining UNKNOWN (Discord supplied no expiry timer)"
        expiry_text = "UNKNOWN"
        certainty = "NO SERVER EXPIRY PROVIDED"
    return (
        f"🚨 DISCORD API BLOCK STATUS | kind={kind} | source={source} | scope={scope} | global={is_global} | "
        f"started={datetime.fromtimestamp(started, tz=TZ_THAI).strftime('%d/%m/%Y %H:%M:%S %Z')} | "
        f"elapsed={_format_duration(elapsed)} | {remaining_text} | expiry={expiry_text} | "
        f"next_probe_in={_format_duration(next_probe_remaining) if next_probe_remaining > 0 else 'READY'} | {certainty}"
    )


def _clear_discord_block_after_success(*, context: str):
    global discord_block_started_at, discord_block_last_seen_at
    global discord_block_server_until, discord_block_server_retry_after
    global discord_block_scope, discord_block_global, discord_block_kind, discord_block_source
    global discord_block_temp_restriction, discord_block_next_probe_mono, discord_block_recovery_until_mono
    now_wall = time.time()
    with discord_block_lock:
        started = discord_block_started_at
        last_seen = discord_block_last_seen_at
        if started <= 0:
            return
        elapsed = max(0.0, now_wall - started)
        print(
            f"✅ DISCORD API BLOCK CLEARED | context={context} | duration={_format_duration(elapsed)} | "
            f"last_429_seen={datetime.fromtimestamp(last_seen, tz=TZ_THAI).strftime('%d/%m/%Y %H:%M:%S %Z') if last_seen else '-'}",
            flush=True,
        )
        discord_block_started_at = 0.0
        discord_block_last_seen_at = 0.0
        discord_block_server_until = 0.0
        discord_block_server_retry_after = 0.0
        discord_block_scope = ""
        discord_block_global = False
        discord_block_kind = ""
        discord_block_source = ""
        discord_block_temp_restriction = False
        discord_block_next_probe_mono = 0.0
        discord_block_recovery_until_mono = time.monotonic() + 30.0
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _clear_persisted_discord_block_state(reason=f"success:{context}"),
            name="discord-rest-block-clear",
        )
    except RuntimeError:
        pass


def _mark_discord_block_log(context: str, *, force: bool = False):
    global discord_block_last_log_at
    now_mono = time.monotonic()
    if not force and now_mono - discord_block_last_log_at < 15.0:
        return
    discord_block_last_log_at = now_mono
    print(_discord_block_diagnostic_line() + f" | context={context}", flush=True)


async def discord_block_diagnostics_loop():
    """Passive diagnostics only: no HTTP probes are sent."""
    while True:
        try:
            with discord_block_lock:
                active = discord_block_started_at > 0
            if active:
                _mark_discord_block_log("periodic", force=True)
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️ Discord block diagnostics loop error: {exc!r}", flush=True)
            await asyncio.sleep(15)


def _apply_discord_rest_429(exc: Exception, *, context: str) -> float:
    global discord_rest_rate_limited_until, discord_rest_backoff_seconds, discord_rest_last_429_log
    global discord_block_next_probe_mono, discord_block_local_retry_seconds
    meta = _record_discord_block_observed(exc, context=context, source="REST")
    server_retry = float(meta.get("retry_after") or 0.0)
    if server_retry <= 0:
        server_retry = float(meta.get("reset_after") or 0.0)
    is_temp_restriction = bool(discord_block_temp_restriction)
    now_mono = time.monotonic()
    if server_retry > 0:
        local_pause = server_retry
        probe_in = server_retry + (discord_block_recovery_grace_seconds if is_temp_restriction else 0.0)
        discord_block_local_retry_seconds = min(max(60.0, server_retry * 2.0), 900.0)
        discord_rest_backoff_seconds = discord_block_local_retry_seconds
    else:
        local_pause = discord_block_local_retry_seconds
        discord_block_local_retry_seconds = min(discord_block_local_retry_seconds * 2.0, 900.0)
        probe_in = local_pause
    discord_rest_rate_limited_until = max(discord_rest_rate_limited_until, now_mono + max(1.0, local_pause))
    discord_block_next_probe_mono = max(discord_block_next_probe_mono, now_mono + max(1.0, probe_in))
    if is_temp_restriction:
        _quarantine_background_rest(
            reason=f"discord-temp-restriction:{context}",
            duration=server_retry + discord_background_rest_recovery_grace_seconds,
        )
    _schedule_persist_discord_block_state(reason=f"429:{context}")
    now_log = time.monotonic()
    if now_log - discord_rest_last_429_log >= 5.0:
        discord_rest_last_429_log = now_log
        timer_note = f"server_timer={server_retry:.3f}s" if server_retry > 0 else f"server_timer=UNKNOWN | local_safety_pause={local_pause:.1f}s"
        print(f"⏸️ GLOBAL Discord REST 429 | context={context} | {timer_note} | next_real_request_in={max(0.0, discord_block_next_probe_mono - time.monotonic()):.1f}s | repeat probes suppressed", flush=True)
    _mark_discord_block_log(context, force=True)
    return local_pause


def _apply_discord_rest_error(exc: Exception, *, context: str) -> float:
    """Throttle repeated invalid/server requests even when Discord returns non-429 errors."""
    global discord_rest_rate_limited_until, discord_rest_last_error_log
    status = getattr(exc, "status", None)
    if status in (400, 401, 403, 404):
        delay = 60.0
    elif status is not None and int(status) >= 500:
        delay = 15.0
    else:
        delay = 10.0
    discord_rest_rate_limited_until = max(discord_rest_rate_limited_until, time.monotonic() + delay)
    now_mono = time.monotonic()
    if now_mono - discord_rest_last_error_log >= 10.0:
        discord_rest_last_error_log = now_mono
        print(
            f"⏸️ GLOBAL Discord REST error cooldown | context={context} | "
            f"status={status} | pause={delay:.1f}s",
            flush=True,
        )
    return delay


def _clear_discord_rest_backoff_after_success():
    global discord_rest_backoff_seconds
    discord_rest_backoff_seconds = 60.0


discord_rest_last_skip_logs = {}


def _log_rest_skip(context: str, remaining: float):
    """Log REST suppression at most once per context per 60 seconds."""
    now_mono = time.monotonic()
    last = float(discord_rest_last_skip_logs.get(context, 0.0))
    if now_mono - last >= 60.0:
        discord_rest_last_skip_logs[context] = now_mono
        print(
            f"⏭️ Discord REST skipped during global cooldown | context={context} | "
            f"remaining={remaining:.1f}s",
            flush=True,
        )


def _is_background_discord_rest_context(context: str) -> bool:
    """Classify non-essential Discord REST work without touching interaction/command lanes."""
    value = str(context or "").strip().lower()
    return any(value.startswith(prefix) for prefix in discord_background_rest_context_prefixes)


def _background_rest_remaining() -> float:
    return max(0.0, discord_background_rest_suppressed_until - time.monotonic())


def _log_background_rest_skip(context: str, remaining: float, reason: str = "quarantine"):
    key = f"bg:{reason}:{context}"
    now_mono = time.monotonic()
    last = float(discord_rest_last_skip_logs.get(key, 0.0))
    if now_mono - last >= 60.0:
        discord_rest_last_skip_logs[key] = now_mono
        print(
            f"⏭️ Background Discord REST skipped | context={context} | reason={reason} | remaining={remaining:.1f}s",
            flush=True,
        )


def _quarantine_background_rest(*, reason: str, duration: float):
    global discord_background_rest_suppressed_until, discord_background_rest_quarantined
    hold = max(1.0, float(duration))
    until = time.monotonic() + hold
    discord_background_rest_suppressed_until = max(discord_background_rest_suppressed_until, until)
    discord_background_rest_quarantined = True
    print(
        f"🛡️ Background Discord REST quarantine | reason={reason} | hold={hold:.1f}s | no background reprobe",
        flush=True,
    )


def _arm_background_rest_after_foreground_recovery():
    global discord_background_rest_suppressed_until, discord_background_rest_quarantined
    hold = discord_background_rest_recovery_grace_seconds
    discord_background_rest_suppressed_until = max(
        discord_background_rest_suppressed_until,
        time.monotonic() + hold,
    )
    discord_background_rest_quarantined = True
    print(
        f"🛡️ Background Discord REST recovery grace armed | hold={hold:.1f}s | foreground request succeeded",
        flush=True,
    )


async def guarded_discord_call(
    call_factory,
    *,
    context: str,
    wait_for_cooldown: bool = False,
    background: bool | None = None,
):
    """Run a Discord REST call through the central guard.

    V58 background policy:
    - boss/audit/BF/Library/Live REST is non-essential and separately throttled.
    - once Discord reports an API/IP temporary restriction, background REST is quarantined;
      it never probes the API just to see whether the block ended.
    - foreground command REST may still proceed according to Discord's own Retry-After rules.
    - Voice/TTS calls never enter this function.
    """
    if background is None:
        background = _is_background_discord_rest_context(context)

    if SKYNET_RUNTIME_ROLE == "web":
        print(f"⛔ Discord REST disabled in web runtime | context={context}", flush=True)
        return None

    global discord_rest_next_call_at, discord_background_rest_next_call_at
    global discord_background_rest_quarantined, discord_background_rest_startup_hold

    if background:
        if discord_background_rest_startup_hold:
            _log_background_rest_skip(
                context,
                0.0,
                reason="startup-command-sync-hold",
            )
            return None

        bg_remaining = _background_rest_remaining()
        if discord_background_rest_quarantined or bg_remaining > 0:
            if bg_remaining <= 0 and discord_background_rest_quarantined:
                # V58 is deliberately no-reprobe: release only after the explicit hold.
                discord_background_rest_quarantined = False
            else:
                _log_background_rest_skip(
                    context,
                    max(bg_remaining, 0.0),
                    reason="recovery-hold" if bg_remaining > 0 else "quarantine",
                )
                return None

        with discord_block_lock:
            temp_restriction = discord_block_temp_restriction
            next_probe_mono = discord_block_next_probe_mono
        if temp_restriction and next_probe_mono > time.monotonic():
            _log_background_rest_skip(
                context,
                max(0.0, next_probe_mono - time.monotonic()),
                reason="discord-block",
            )
            return None

        now_bg = time.monotonic()
        if discord_background_rest_next_call_at > now_bg:
            await asyncio.sleep(discord_background_rest_next_call_at - now_bg)
        discord_background_rest_next_call_at = time.monotonic() + discord_background_rest_min_interval

    now_mono = time.monotonic()
    with discord_block_lock:
        temp_restriction = discord_block_temp_restriction
        next_probe_mono = discord_block_next_probe_mono
    if temp_restriction and next_probe_mono > now_mono:
        if background:
            _log_background_rest_skip(context, max(0.0, next_probe_mono - now_mono), reason="discord-block")
        else:
            _log_rest_skip(context, max(0.0, next_probe_mono - now_mono))
        return None

    remaining = _discord_rest_rate_limit_remaining()
    if remaining > 0:
        if not wait_for_cooldown:
            _log_rest_skip(context, remaining)
            return None
        await asyncio.sleep(remaining)

    async with discord_rest_guard_lock:
        now_mono = time.monotonic()
        with discord_block_lock:
            temp_restriction = discord_block_temp_restriction
            next_probe_mono = discord_block_next_probe_mono
        if temp_restriction and next_probe_mono > now_mono:
            if background:
                _log_background_rest_skip(context, max(0.0, next_probe_mono - now_mono), reason="discord-block")
            else:
                _log_rest_skip(context, max(0.0, next_probe_mono - now_mono))
            return None

        if background:
            bg_remaining = _background_rest_remaining()
            if discord_background_rest_quarantined or bg_remaining > 0:
                if bg_remaining <= 0 and discord_background_rest_quarantined:
                    discord_background_rest_quarantined = False
                else:
                    _log_background_rest_skip(
                        context,
                        max(bg_remaining, 0.0),
                        reason="recovery-hold" if bg_remaining > 0 else "quarantine",
                    )
                    return None

        remaining = _discord_rest_rate_limit_remaining()
        if remaining > 0:
            if not wait_for_cooldown:
                if background:
                    _log_background_rest_skip(context, remaining, reason="rate-limit")
                else:
                    _log_rest_skip(context, remaining)
                return None
            await asyncio.sleep(remaining)

        now = time.monotonic()
        if discord_rest_next_call_at > now:
            await asyncio.sleep(discord_rest_next_call_at - now)
        if discord_rest_min_interval > 0:
            discord_rest_next_call_at = time.monotonic() + discord_rest_min_interval

        had_active_block = False
        with discord_block_lock:
            had_active_block = discord_block_started_at > 0

        try:
            result = await call_factory()
            _clear_discord_rest_backoff_after_success()
            _clear_discord_block_after_success(context=context)
            if not background and had_active_block:
                _arm_background_rest_after_foreground_recovery()
            return result
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                if background and discord_block_temp_restriction:
                    # The Discord-provided Retry-After is authoritative; add the existing
                    # recovery grace so the next background request cannot be an immediate probe.
                    server_retry = 0.0
                    try:
                        server_retry = float(getattr(exc, "retry_after", 0) or 0)
                    except (TypeError, ValueError):
                        server_retry = 0.0
                    _quarantine_background_rest(
                        reason=f"429:{context}",
                        duration=server_retry + discord_background_rest_recovery_grace_seconds,
                    )
                _apply_discord_rest_429(exc, context=context)
            else:
                _apply_discord_rest_error(exc, context=context)
            raise
        except Exception:
            raise


async def guarded_channel_send(channel, *, context: str, content=None, embed=None, view=None, background: bool | None = None):
    return await guarded_discord_call(
        lambda: channel.send(content=content, embed=embed, view=view),
        context=context,
        background=background,
    )


async def guarded_fetch_channel(channel_id: int, *, context: str, background: bool | None = None):
    return await guarded_discord_call(
        lambda: bot.fetch_channel(int(channel_id)),
        context=context,
        background=background,
    )


async def guarded_fetch_message(channel, message_id: int, *, context: str, background: bool | None = None):
    return await guarded_discord_call(
        lambda: channel.fetch_message(int(message_id)),
        context=context,
        background=background,
    )


async def guarded_message_edit(message, *, context: str, background: bool | None = None, **kwargs):
    return await guarded_discord_call(
        lambda: message.edit(**kwargs),
        context=context,
        background=background,
    )


async def guarded_context_send(ctx, *args, context: str, background: bool | None = None, **kwargs):
    return await guarded_discord_call(
        lambda: ctx.send(*args, **kwargs),
        context=context,
        background=background,
    )


# Interaction webhook traffic is intentionally isolated from the background REST
# circuit breaker.  A valid interaction token has its own webhook lane, and blocking
# it because a background Discord REST route is cooling down can make otherwise-
# successful slash commands appear to fail.  This lane still honors short Retry-After
# values and refuses long sleeps that would outlive the interaction/webhook window.
interaction_webhook_guard_lock = asyncio.Lock()
interaction_webhook_next_call_at = 0.0

async def _interaction_webhook_call(call_factory, *, context: str):
    global interaction_webhook_next_call_at
    async with interaction_webhook_guard_lock:
        now = time.monotonic()
        if interaction_webhook_next_call_at > now:
            await asyncio.sleep(interaction_webhook_next_call_at - now)
        try:
            result = await call_factory()
            interaction_webhook_next_call_at = time.monotonic() + max(0.1, discord_rest_min_interval)
            return result
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                retry_after = 0.0
                try:
                    retry_after = float(getattr(exc, "retry_after", 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                # Only honor a short webhook retry delay.  Waiting for a long
                # restriction here would exceed the lifetime/usefulness of the interaction.
                if 0 < retry_after <= 8.0:
                    interaction_webhook_next_call_at = time.monotonic() + retry_after
                    await asyncio.sleep(retry_after)
                    result = await call_factory()
                    interaction_webhook_next_call_at = time.monotonic() + max(0.1, discord_rest_min_interval)
                    return result
                print(
                    f"⚠️ Interaction webhook 429 | context={context} | retry_after={retry_after:.2f}s | "
                    "background REST cooldown ignored",
                    flush=True,
                )
            else:
                print(
                    f"⚠️ Interaction webhook failed | context={context} | status={getattr(exc, 'status', None)} | {exc}",
                    flush=True,
                )
            return None
        except Exception as exc:
            print(f"⚠️ Interaction webhook failed unexpectedly | context={context} | {exc!r}", flush=True)
            return None


async def guarded_interaction_followup_send(interaction: discord.Interaction, context: str, *args, **kwargs):
    """Send a follow-up only after a successful initial interaction response.

    A follow-up before the initial callback has been accepted is guaranteed to be
    unsafe for an interaction that Discord has already rejected with 429.  Skip it
    locally so a failed ACK cannot create an unnecessary second request.
    """
    try:
        if not interaction.response.is_done():
            print(
                f"⏭️ Interaction followup skipped because initial response was not accepted | context={context}",
                flush=True,
            )
            return None
    except Exception:
        pass
    return await _interaction_webhook_call(
        lambda: interaction.followup.send(*args, **kwargs),
        context=context,
    )


async def guarded_interaction_edit_original(interaction: discord.Interaction, context: str, *args, **kwargs):
    """Edit the original interaction response only when the initial callback succeeded."""
    try:
        if not interaction.response.is_done():
            print(
                f"⏭️ Interaction edit skipped because initial response was not accepted | context={context}",
                flush=True,
            )
            return None
    except Exception:
        pass
    return await _interaction_webhook_call(
        lambda: interaction.edit_original_response(*args, **kwargs),
        context=context,
    )


bot_event_loop = None

bf_notify_enabled = True
lib_notify_enabled = True
ppl_notify_enabled = True
tts_th_enabled = True
tts_en_enabled = True
tts_ko_enabled = True

# V72: Discord text notification languages are independent from TTS languages.
discord_notify_th_enabled = True
discord_notify_en_enabled = True
discord_notify_ko_enabled = True

vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
last_bf_notified_hour = -1
last_bf_text_notified_hour = -1
last_bf_voice_success_hour = -1
# Prevent repeated BF text API calls while Discord is rate-limiting.
bf_text_retry_after_ts = {}

# Serialize manual /notice commands to avoid bursty Discord API traffic.
NOTICE_COMMAND_LOCK = asyncio.Lock()
NOTICE_LAST_RUN_TS = 0.0
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

# One-shot Voice confirmation for newly recorded boss times.
_confirmation_seen_ids = set()
_confirmation_claim_lock = asyncio.Lock()
# Dashboard confirmations submitted while Discord is not READY are held here and
# drained automatically after on_ready completes.
_pending_voice_confirmations = {}

# ==========================================
# 📝 3. ระบบ Audit Log
# ==========================================
# 🔐 V50: Keep Audit Log durable while Discord REST is temporarily blocked.
# This queue never retries in a tight loop; it is drained by a 15-second background task
# only after the existing REST guard says requests are allowed again.
PENDING_AUDIT_MAX = 200
pending_audit_logs = deque(maxlen=PENDING_AUDIT_MAX)
pending_audit_lock = threading.Lock()


AUDIT_ACTION_TRANSLATIONS = {
    "ตั้งค่า TTS เสียง (/tts)":{"th":"ตั้งค่า TTS เสียง (/tts)","en":"TTS Voice Settings (/tts)","ko":"TTS 음성 설정 (/tts)"},
    "ตั้งค่าการแจ้งเตือน BF (/notify)":{"th":"ตั้งค่าการแจ้งเตือน BF (/notify)","en":"BF Notification Settings (/notify)","ko":"BF 알림 설정 (/notify)"},
    "ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)":{"th":"ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)","en":"Member Voice-Join Notification Settings (/ppl)","ko":"음성 채널 입장 알림 설정 (/ppl)"},
    "เปิดระบบทักทายคนพิเศษ (/vip)":{"th":"เปิดระบบทักทายคนพิเศษ (/vip)","en":"Enable VIP Greeting (/vip)","ko":"VIP 인사 기능 활성화 (/vip)"},
    "ปิดระบบทักทายคนพิเศษ (/vip)":{"th":"ปิดระบบทักทายคนพิเศษ (/vip)","en":"Disable VIP Greeting (/vip)","ko":"VIP 인사 기능 비활성화 (/vip)"},
    "เช็กเวลาบอสพร้อม TTS (!time)":{"th":"เช็กเวลาบอสพร้อม TTS (!time)","en":"Check Boss Times with TTS (!time)","ko":"TTS와 함께 보스 시간 확인 (!time)"},
    "เพิ่มบอส (/addboss)":{"th":"เพิ่มบอส (/addboss)","en":"Add Boss (/addboss)","ko":"보스 추가 (/addboss)"},
    "ลบบอส (/delboss)":{"th":"ลบบอส (/delboss)","en":"Delete Boss (/delboss)","ko":"보스 삭제 (/delboss)"},
    "สร้าง Live Embed (/setlive)":{"th":"สร้าง Live Embed (/setlive)","en":"Create Live Embed (/setlive)","ko":"Live Embed 생성 (/setlive)"},
    "ประกาศ Code / Item ของบอส":{"th":"ประกาศ Code / Item ของบอส","en":"Boss Code / Item Announcement","ko":"보스 코드 / 아이템 공지"},
    "ประกาศเช็คชื่อบอส":{"th":"ประกาศเช็คชื่อบอส","en":"Boss Attendance Announcement","ko":"보스 출석 공지"},
}

def _translate_audit_action(action: str, lang: str) -> str:
    entry=AUDIT_ACTION_TRANSLATIONS.get(str(action))
    return entry.get(lang, entry.get("th", str(action))) if entry else str(action)

def _translate_audit_details(action: str, details: str, lang: str) -> str:
    text=str(details or "-")
    if lang=="th": return text
    if action=="ตั้งค่า TTS เสียง (/tts)":
        m=re.match(r"ภาษา:\s*`([^`]+)`\s*\|\s*สถานะ:\s*`([^`]+)`",text)
        if m: return f"Language: `{m.group(1)}` | Status: `{m.group(2)}`" if lang=="en" else f"언어: `{m.group(1)}` | 상태: `{m.group(2)}`"
    if action in {"ตั้งค่าการแจ้งเตือน BF (/notify)","ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)"}:
        m=re.search(r"`([^`]+)`",text); status=m.group(1) if m else "-"
        return f"Changed status to: `{status}`" if lang=="en" else f"상태 변경: `{status}`"
    if action=="เปิดระบบทักทายคนพิเศษ (/vip)":
        m=re.search(r"คนพิเศษ:\s*`([^`]*)`\s*\n💬 ข้อความ:\s*(.*)$",text,re.S)
        if m: return f"👤 VIP User: `{m.group(1)}`\n💬 Message: {m.group(2)}" if lang=="en" else f"👤 VIP 사용자: `{m.group(1)}`\n💬 메시지: {m.group(2)}"
    if action=="ปิดระบบทักทายคนพิเศษ (/vip)": return "VIP information was cleared successfully." if lang=="en" else "VIP 정보가 성공적으로 삭제되었습니다."
    if action=="เช็กเวลาบอสพร้อม TTS (!time)": return "Boss times were calculated, sorted, and read aloud successfully." if lang=="en" else "보스 시간을 계산하고 정렬한 후 음성으로 안내했습니다."
    if action=="เพิ่มบอส (/addboss)": return text.replace("ไม่มีการสร้าง boss_schedule","No boss_schedule was created.") if lang=="en" else text.replace("ไม่มีการสร้าง boss_schedule","boss_schedule은 생성되지 않았습니다.")
    if action=="ลบบอส (/delboss)":
        m=re.search(r"ลบบอส:\s*`([^`]*)`",text)
        if m: return f"🗑️ Deleted boss: `{m.group(1)}`" if lang=="en" else f"🗑️ 삭제된 보스: `{m.group(1)}`"
    if action=="สร้าง Live Embed (/setlive)":
        return text.replace("ช่อง:","Channel:") if lang=="en" else text.replace("ช่อง:","채널:").replace("Message ID:","메시지 ID:")
    if action in {"ประกาศเช็คชื่อบอส", "ประกาศ Code / Item ของบอส"}:
        return text.replace("ผู้ประกาศ", "Announcer" if lang=="en" else "공지자").replace("ชื่อบอส", "Boss" if lang=="en" else "보스").replace("โค้ด (Code)", "Code").replace("ไอเทมดรอป", "Drop Item" if lang=="en" else "드롭 아이템")
    return text

def _build_audit_embed(user: discord.User, action: str, details: str, color: discord.Color, *, languages=None):
    enabled=list(languages or get_enabled_discord_notification_languages()) or ["th"]; primary=enabled[0]
    embed=discord.Embed(title=f"📝 Audit Log: {_translate_audit_action(action,primary)}",color=color,timestamp=datetime.now(TZ_THAI))
    actor={"th":"👤 ผู้ดำเนินการ","en":"👤 Actor","ko":"👤 수행자"}; detail={"th":"📋 รายละเอียด","en":"📋 Details","ko":"📋 상세 정보"}
    embed.add_field(name=actor[primary],value=f"{user.mention} (`{user.name}`)",inline=True)
    for lang in enabled:
        label={"th":"🇹🇭 ไทย","en":"🇺🇸 English","ko":"🇰🇷 한국어"}[lang]
        embed.add_field(name=f"{detail[lang]} • {label}",value=_translate_audit_details(action,details,lang),inline=False)
    embed.set_footer(text=f"User ID: {user.id}")
    return embed


def _queue_audit_log(guild_id: int, channel_id: int, action: str, user_id: int, user_name: str, details: str, color_value: int):
    item = {
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "action": str(action),
        "user_id": int(user_id),
        "user_name": str(user_name),
        "details": str(details),
        "color_value": int(color_value),
        "queued_at": time.time(),
    }
    with pending_audit_lock:
        pending_audit_logs.append(item)
        size = len(pending_audit_logs)
    print(
        f"⏸️ Audit Log queued during Discord REST cooldown | action={action} | queue={size}/{PENDING_AUDIT_MAX}",
        flush=True,
    )


async def _send_one_audit_log(item: dict) -> bool:
    try:
        await refresh_discord_notification_languages()
    except Exception:
        pass
    guild = bot.get_guild(int(item.get("guild_id", 0)))
    if guild is None:
        return False
    channel = guild.get_channel(int(item.get("channel_id", 0)))
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if channel is None:
        return False

    try:
        user_id = int(item.get("user_id", 0))
    except (TypeError, ValueError):
        user_id = 0
    display_name = str(item.get("user_name") or "unknown")
    user_obj = guild.get_member(user_id) or discord.Object(id=user_id)
    # Preserve the existing audit format. Mention uses the original member when cached;
    # otherwise fall back to a plain user ID label rather than fetching during a rate limit.
    if hasattr(user_obj, "mention"):
        user_mention = getattr(user_obj, "mention", f"<@{user_id}>")
    else:
        user_mention = f"<@{user_id}>"
    enabled=get_enabled_discord_notification_languages() or ["th"]
    primary=enabled[0]
    action=str(item.get("action","-")); details=str(item.get("details") or "-")
    embed=discord.Embed(title=f"📝 Audit Log: {_translate_audit_action(action,primary)}",color=discord.Color(int(item.get("color_value",0x5865F2))),timestamp=datetime.fromtimestamp(float(item.get("queued_at",time.time())),tz=TZ_THAI))
    actor_labels={"th":"👤 ผู้ดำเนินการ","en":"👤 Actor","ko":"👤 수행자"}; detail_labels={"th":"📋 รายละเอียด","en":"📋 Details","ko":"📋 상세 정보"}
    embed.add_field(name=actor_labels[primary],value=f"{user_mention} (`{display_name}`)",inline=True)
    for lang in enabled:
        label={"th":"🇹🇭 ไทย","en":"🇺🇸 English","ko":"🇰🇷 한국어"}[lang]
        embed.add_field(name=f"{detail_labels[lang]} • {label}",value=_translate_audit_details(action,details,lang),inline=False)
    embed.set_footer(text=f"User ID: {user_id}")
    try:
        result = await guarded_channel_send(channel, context=f"audit:{item.get('action', '-')}", embed=embed)
        return result is not None
    except Exception as exc:
        print(f"⚠️ ส่ง queued Audit Log ไม่สำเร็จ: {exc}", flush=True)
        return False


PENDING_CHANNEL_MAX = 100
pending_channel_messages = deque(maxlen=PENDING_CHANNEL_MAX)
pending_channel_lock = threading.Lock()


def _queue_channel_result(channel_id: int, *, content=None, embed=None, context: str = "command-result"):
    item = {
        "channel_id": int(channel_id),
        "content": content,
        "embed": embed,
        "context": str(context),
        "queued_at": time.time(),
    }
    with pending_channel_lock:
        pending_channel_messages.append(item)
        size = len(pending_channel_messages)
    print(
        f"⏸️ Discord channel result queued during REST cooldown | context={context} | queue={size}/{PENDING_CHANNEL_MAX}",
        flush=True,
    )


async def _flush_pending_channel_messages_once():
    if _discord_rest_rate_limit_remaining() > 0:
        return
    for _ in range(2):
        with pending_channel_lock:
            if not pending_channel_messages:
                return
            item = pending_channel_messages[0]
        channel = bot.get_channel(int(item.get("channel_id", 0)))
        if channel is None:
            with pending_channel_lock:
                if pending_channel_messages and pending_channel_messages[0] is item:
                    pending_channel_messages.popleft()
            continue
        try:
            result = await guarded_channel_send(
                channel,
                context=item.get("context", "command-result"),
                content=item.get("content"),
                embed=item.get("embed"),
            )
        except Exception as exc:
            print(f"⚠️ queued Discord channel result failed: {exc!r}", flush=True)
            return
        if result is None:
            return
        with pending_channel_lock:
            if pending_channel_messages and pending_channel_messages[0] is item:
                pending_channel_messages.popleft()
        await asyncio.sleep(max(0.5, discord_rest_min_interval))


async def flush_pending_command_outputs_once():
    await _flush_pending_channel_messages_once()
    await flush_pending_boss_rest_notifications_once()
    await flush_pending_audit_logs_once()


@tasks.loop(seconds=15)
async def flush_pending_command_outputs():
    try:
        await flush_pending_command_outputs_once()
    except Exception as exc:
        print(f"⚠️ queued command output worker failed safely: {exc!r}", flush=True)


async def flush_pending_audit_logs_once():
    # Never drain queued audit logs while Discord reports an API/IP temporary restriction.
    # The interaction callback lane and the normal REST lane can receive different 429
    # metadata, but an API temporary restriction is broader than a normal bucket cooldown.
    # Treat the process-wide block gate as authoritative here so this worker cannot
    # immediately re-probe the restricted API and extend the block.
    now_mono = time.monotonic()
    with discord_block_lock:
        temp_restriction = discord_block_temp_restriction
        next_probe_mono = discord_block_next_probe_mono
    if temp_restriction and next_probe_mono > now_mono:
        _log_rest_skip('audit-queue-blocked', max(0.0, next_probe_mono - now_mono))
        return
    if _discord_rest_rate_limit_remaining() > 0:
        return
    for _ in range(1):
        with pending_audit_lock:
            if not pending_audit_logs:
                return
            item = pending_audit_logs[0]
        ok = await _send_one_audit_log(item)
        if not ok:
            return
        with pending_audit_lock:
            if pending_audit_logs and pending_audit_logs[0] is item:
                pending_audit_logs.popleft()
        await asyncio.sleep(max(0.5, discord_rest_min_interval))


async def send_audit_log(guild: discord.Guild, user: discord.User, action: str, details: str, color: discord.Color):
    if not guild:
        return
    log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if not log_channel:
        print(f"⚠️ Audit Log channel not found | guild={guild.name} | channel={LOG_CHANNEL_NAME}", flush=True)
        return

    try:
        await refresh_discord_notification_languages()
    except Exception:
        pass
    embed = _build_audit_embed(user, action, details, color, languages=get_enabled_discord_notification_languages())
    try:
        result = await guarded_channel_send(log_channel, context=f"audit:{action}", embed=embed)
        if result is not None:
            return
        _queue_audit_log(
            guild.id,
            log_channel.id,
            action,
            getattr(user, "id", 0),
            getattr(user, "display_name", getattr(user, "name", "unknown")),
            details,
            int(color.value),
        )
    except Exception as exc:
        print(f"❌ ส่ง Audit Log ไม่สำเร็จ: {exc}", flush=True)
        _queue_audit_log(
            guild.id,
            log_channel.id,
            action,
            getattr(user, "id", 0),
            getattr(user, "display_name", getattr(user, "name", "unknown")),
            details,
            int(color.value),
        )

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
        "recordedByDisplayName": data.get("recordedByDisplayName") or data.get("recorded_by_display_name") or data.get("recordedBy") or data.get("recorded_by") or "-",
        "recordedByUserId": str(data.get("recordedByUserId") or data.get("recorded_by_user_id") or "").strip(),
        "confirmationRequestId": str(data.get("confirmationRequestId") or data.get("confirmation_request_id") or "").strip(),
        "confirmationRequestedAt": data.get("confirmationRequestedAt") or data.get("confirmation_requested_at") or None,
        "confirmationStatus": str(data.get("confirmationStatus") or data.get("confirmation_status") or "").strip(),
        "notifiedNotice": parse_bool(data.get("notifiedNotice", data.get("notified_advance", False))),
        "notifiedSpawn": parse_bool(data.get("notifiedSpawn", data.get("notified_spawn", False))),
        "voiceNoticeSent": parse_bool(data.get("voiceNoticeSent", data.get("voice_notice_sent", False))),
        "voiceSpawnSent": parse_bool(data.get("voiceSpawnSent", data.get("voice_spawn_sent", False))),
    }
    if parse_bool(data.get("is_library_boss_schedule"), False):
        record["is_library_boss_schedule"] = True
        record["library_slot"] = str(data.get("library_slot") or "")
        record["recurrence"] = str(data.get("recurrence") or "daily")
        record["autoAttendanceEligible"] = parse_bool(data.get("autoAttendanceEligible"), True)
        record["suppressBossNotifications"] = parse_bool(data.get("suppressBossNotifications"), True)
    if kill_ms is not None:
        record["killTimeMs"] = kill_ms
        record["killDate"] = (data.get("killDate") or data.get("kill_date") or kill_dt.strftime("%Y-%m-%d"))
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
        "killDate": record.get("killDate", ""),
        "channel_id": record.get("channelId"),
        "voice_channel_id": record.get("voiceChannelId"),
        "notified_advance": record.get("notifiedNotice", False),
        "notified_spawn": record.get("notifiedSpawn", False),
        "voice_notice_sent": record.get("voiceNoticeSent", False),
        "voice_spawn_sent": record.get("voiceSpawnSent", False),
        "noticeMinutes": record.get("noticeMinutes", 5),
        "recorded_by": record.get("recordedBy", "-"),
        "recordedByDisplayName": record.get("recordedByDisplayName", record.get("recordedBy", "-")),
        "recordedByUserId": record.get("recordedByUserId", ""),
        "confirmationRequestId": record.get("confirmationRequestId", ""),
        "confirmationRequestedAt": record.get("confirmationRequestedAt"),
        "confirmationStatus": record.get("confirmationStatus", ""),
        "is_library_boss_schedule": parse_bool(record.get("is_library_boss_schedule"), False),
        "library_slot": record.get("library_slot", ""),
        "recurrence": record.get("recurrence", ""),
        "autoAttendanceEligible": parse_bool(record.get("autoAttendanceEligible"), False),
        "suppressBossNotifications": parse_bool(record.get("suppressBossNotifications"), False),
    }

async def save_boss_data():
    global is_updating_from_bot
    with schedule_lock:
        firebase_data = {}
        for boss_name, data in boss_schedule.items():
            try:
                firebase_data[boss_name] = _schedule_record_to_firebase(boss_name, data)
            except Exception as e:
                print(f"⚠️ ข้ามข้อมูลบอส {boss_name}: {e}")
    try:
        is_updating_from_bot = True
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("boss_schedule").set, firebase_data),
            timeout=8
        )
    except Exception as e:
        print(f"❌ บันทึก boss_schedule ลง Firebase ไม่สำเร็จ: {e}")
    finally:
        is_updating_from_bot = False
    await asyncio.to_thread(set_db_value, "boss_schedule", firebase_data)
    await asyncio.to_thread(save_json_local, DATA_FILE, firebase_data)

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
            canonical = _canonical_library_boss_key(boss_name)
            if canonical == boss_name:
                canonical = get_boss_canonical_name(boss_name)
            internal = _firebase_to_internal(canonical, data)
            if internal:
                boss_schedule[canonical] = internal
    print(f"✅ โหลด boss_schedule จาก Firebase สำเร็จ {len(boss_schedule)} รายการ")


_confirmation_queue_ids = set()

def queue_voice_confirmation(boss_name: str, data: dict, source: str = 'unknown', wait: bool = False, timeout: float = 180.0):
    """Queue one voice confirmation on the Discord event loop.
    When the bot is not READY yet, retain the request as pending instead of
    falsely reporting Voice failure. The pending request is drained after on_ready.
    """
    request_id = str(data.get("confirmationRequestId") or "").strip()
    if not request_id:
        print(f"⚠️ Voice confirmation skipped: missing requestId | boss={boss_name} | source={source}")
        return False
    status = str(data.get("confirmationStatus") or "").strip()
    if status not in ("", "pending"):
        print(f"⏭️ Voice confirmation skipped: status={status} | boss={boss_name} | source={source}")
        return False
    if request_id in _confirmation_queue_ids:
        print(f"⏭️ Voice confirmation already queued: boss={boss_name} | request={request_id}")
        return True if not wait else False

    if bot_event_loop is None or bot_event_loop.is_closed() or not is_bot_ready:
        _pending_voice_confirmations[request_id] = (boss_name, dict(data), source)
        print(
            f"⏳ Voice confirmation pending: bot event loop not READY | boss={boss_name} "
            f"| source={source} | request={request_id}"
        )
        return None

    _confirmation_queue_ids.add(request_id)
    print(f"📢 Queue voice confirmation | source={source} | boss={boss_name} | request={request_id} | wait={wait}")
    future = asyncio.run_coroutine_threadsafe(_voice_confirm_boss_recording(boss_name, dict(data)), bot_event_loop)
    if not wait:
        # Release the in-memory dedup marker when the actual confirmation task
        # finishes.  V74 left this ID in the set forever for fire-and-forget
        # confirmations, so later retries of the same completed request emitted
        # the misleading "already queued" log indefinitely.
        def _release_confirmation_marker(_future):
            _confirmation_queue_ids.discard(request_id)
        future.add_done_callback(_release_confirmation_marker)
        return True
    try:
        result = future.result(timeout=float(timeout))
        if result is None:
            print(f"📣 Voice confirmation pending | boss={boss_name} | source={source} | room-not-occupied")
            return None
        print(f"📣 Voice confirmation finished | boss={boss_name} | source={source} | success={bool(result)}")
        return bool(result)
    except Exception as exc:
        print(f"❌ Voice confirmation wait failed | boss={boss_name} | source={source} | {exc}")
        try:
            future.cancel()
        except Exception:
            pass
        return False
    finally:
        _confirmation_queue_ids.discard(request_id)

async def _voice_confirm_boss_recording(boss_name: str, data: dict):
    """Speak one-shot confirmation for a newly recorded boss time in occupied Voice rooms only.

    A configured /setvoice room with no human occupants is not a Voice failure: the
    confirmation remains pending so a later Voice join can trigger it.
    """
    request_id = str(data.get("confirmationRequestId") or "").strip()
    requested_at = data.get("confirmationRequestedAt")
    status = str(data.get("confirmationStatus") or "").strip()
    if not request_id or status not in ("", "pending"):
        return False
    try:
        requested_ms = float(requested_at) if isinstance(requested_at, (int, float)) else datetime.fromisoformat(str(requested_at).replace("Z", "+00:00")).timestamp() * 1000
        if (time.time() * 1000 - requested_ms) > 30 * 60 * 1000:
            return False
    except Exception:
        pass

    spoken_name = get_boss_pronunciation(boss_name)
    recorded_by = str(data.get("recordedBy") or data.get("recorded_by") or "").strip()
    if not recorded_by or recorded_by.lower() in {"unknown", "unknow", "ไม่ระบุ"}:
        recorded_by = "สมาชิก"

    success = False
    has_configured_target = False
    has_occupied_target = False
    try:
        # Determine whether the configured /setvoice targets are actually occupied
        # before marking the durable confirmation request as processing.
        for guild in list(bot.guilds):
            configured_channels = get_configured_voice_channels(guild)
            if not configured_channels:
                print(f"⚠️ Boss confirmation skipped: no /setvoice targets | guild={guild.name}")
                continue
            has_configured_target = True
            for configured in configured_channels:
                humans = [m for m in configured.members if not m.bot]
                if not humans:
                    print(f"⏭️ Boss confirmation skipped: configured Voice is empty | guild={guild.name} | channel={configured.name}")
                    continue
                has_occupied_target = True

        # If /setvoice is configured but nobody is currently in those rooms, keep the
        # durable Firebase request pending instead of reporting a false Voice failure.
        if has_configured_target and not has_occupied_target:
            with schedule_lock:
                _confirmation_seen_ids.discard(request_id)
            print(
                f"⏳ Boss record confirmation pending: configured /setvoice rooms exist but are empty | "
                f"boss={boss_name} | request={request_id}",
                flush=True,
            )
            try:
                await asyncio.to_thread(
                    db.reference(f"boss_schedule/{boss_name}").update,
                    {"confirmationStatus": "pending"}
                )
            except Exception as exc:
                print(f"⚠️ Could not persist pending confirmation for {boss_name}: {exc}")
            return None

        if not has_configured_target:
            print(
                f"⚠️ Boss record confirmation failed: no /setvoice targets | "
                f"boss={boss_name} | request={request_id}",
                flush=True,
            )
            return False

        try:
            await asyncio.to_thread(
                db.reference(f"boss_schedule/{boss_name}").update,
                {"confirmationStatus": "processing"}
            )
        except Exception as exc:
            print(f"⚠️ Could not mark confirmation processing: {boss_name}: {exc}")

        await refresh_tts_settings_from_firebase()
        # User explicitly requested a confirmation after saving; use the Thai text
        # and the currently enabled languages for the same TTS policy.
        text_th = f"บันทึกเวลาบอส {spoken_name} สำเร็จแล้วค่ะ"
        text_en = f"Boss {boss_name} time saved successfully."
        text_ko = f"보스 {boss_name} 시간이 성공적으로 저장되었습니다."
        for guild in list(bot.guilds):
            configured_channels = get_configured_voice_channels(guild)
            for configured in configured_channels:
                humans = [m for m in configured.members if not m.bot]
                if not humans:
                    continue
                print(f"📢 Boss confirmation voice target | guild={guild.name} | channel={configured.name} | humans={len(humans)}")
                try:
                    ok = await asyncio.wait_for(
                        speak_in_guild(guild, text_th=text_th, text_en=text_en, text_ko=text_ko, target_channel=configured),
                        timeout=180
                    )
                    success = success or bool(ok)
                except Exception as exc:
                    print(f"❌ Boss record confirmation failed ({boss_name}/{guild.name}/{configured.name}): {exc}")

        print(f"✅ Boss record confirmation | boss={boss_name} | user={recorded_by} | success={success}")
    finally:
        try:
            final_status = "sent" if success else ("pending" if has_configured_target else "failed")
            await asyncio.to_thread(
                db.reference(f"boss_schedule/{boss_name}").update,
                {"confirmationStatus": final_status}
            )
            if final_status == "pending":
                with schedule_lock:
                    _confirmation_seen_ids.discard(request_id)
        except Exception as exc:
            print(f"⚠️ Could not persist confirmation status for {boss_name}: {exc}")
        _confirmation_queue_ids.discard(request_id)
    return bool(success)

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
                canonical = _canonical_library_boss_key(boss_name)
                if canonical == boss_name:
                    canonical = get_boss_canonical_name(boss_name)
                internal = _firebase_to_internal(canonical, data)
                if internal:
                    new_schedule[canonical] = internal
            with schedule_lock:
                previous = dict(boss_schedule)
                boss_schedule.clear()
                boss_schedule.update(new_schedule)

            # Trigger one-shot confirmation from the durable Firebase pending state.
            # Do not require the local cache's previous requestId to differ: the local
            # cache can already contain the same pending request after a dashboard save,
            # a reconnect, or a root-sync race.  The requestId itself is the idempotency
            # key, while confirmationStatus=pending is the durable work signal.
            for boss_name, item in new_schedule.items():
                req_id = str(item.get("confirmationRequestId") or "").strip()
                status = str(item.get("confirmationStatus") or "pending").strip().lower()
                if not req_id or status not in ("", "pending"):
                    continue
                if req_id in _confirmation_seen_ids or req_id in _confirmation_queue_ids:
                    continue

                queue_result = queue_voice_confirmation(
                    boss_name, item, source='firebase-listener'
                )
                # Only mark the request as seen after it has been accepted either
                # for immediate execution or for the READY/pending queue.  This keeps
                # a transient listener race from permanently swallowing the request.
                if queue_result is not False:
                    _confirmation_seen_ids.add(req_id)
            print(f"🔄 Firebase boss_schedule sync: {len(new_schedule)} รายการ")
        except Exception as e:
            print(f"❌ Firebase Listener boss_schedule ผิดพลาด: {e}")
    try:
        db.reference("boss_schedule").listen(listener)
        print("🟢 Firebase Listener พร้อมทำงานแบบ safe root-sync")
    except Exception as e:
        print(f"❌ ไม่สามารถเปิด Firebase Listener ได้: {e}")

async def save_voice_config():
    """บันทึกการตั้งค่าห้อง Voice แบบถาวรลง Firebase และ local SQLite/JSON"""
    with schedule_lock:
        data = {str(gid): dict(cfg) for gid, cfg in voice_config.items()}
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("voice_config").set, data),
            timeout=8
        )
    except Exception as e:
        print(f"⚠️ บันทึก voice_config ลง Firebase ไม่สำเร็จ: {e}")
    await asyncio.to_thread(set_db_value, "voice_config", data)
    await asyncio.to_thread(save_json_local, VOICE_CONFIG_FILE, data)

async def save_notification_channels():
    """Persist Discord text-notification target channels to Firebase and local storage."""
    with schedule_lock:
        data = {str(gid): dict(channels) for gid, channels in (notification_channels or {}).items()}
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("notification_channels").set, data), timeout=8
        )
        firebase_ok = True
    except Exception as e:
        firebase_ok = False
        print(f"⚠️ บันทึก notification_channels ลง Firebase ไม่สำเร็จ: {e}")
    await asyncio.to_thread(set_db_value, "notification_channels", data)
    await asyncio.to_thread(save_json_local, "notification_channels.json", data)
    return firebase_ok


async def load_notification_channels():
    """Load persistent Discord text-notification channels. Firebase is canonical."""
    global notification_channels
    data = None
    try:
        data = await asyncio.to_thread(db.reference("notification_channels").get)
    except Exception as e:
        print(f"⚠️ โหลด notification_channels จาก Firebase ไม่สำเร็จ: {e}")
    if not isinstance(data, dict) or not data:
        data = get_db_value("notification_channels", None)

    normalized = {}
    if isinstance(data, dict):
        for guild_id, guild_data in data.items():
            if not isinstance(guild_data, dict):
                continue
            bucket = {}
            # Accept the canonical channels map. Also tolerate a flat legacy shape.
            source = guild_data.get("channels") if isinstance(guild_data.get("channels"), dict) else guild_data
            for channel_id, cfg in source.items():
                if not isinstance(cfg, dict):
                    continue
                cid = cfg.get("channel_id", channel_id)
                try:
                    cid = int(cid)
                except (TypeError, ValueError):
                    continue
                enabled = parse_bool(cfg.get("enabled"), True)
                if not enabled:
                    continue
                gid = cfg.get("guild_id", guild_id)
                try:
                    gid = int(gid)
                except (TypeError, ValueError):
                    try:
                        gid = int(guild_id)
                    except (TypeError, ValueError):
                        continue
                bucket[str(cid)] = {
                    "guild_id": gid,
                    "channel_id": cid,
                    "channel_name": str(cfg.get("channel_name") or ""),
                    "enabled": True,
                    "updated_by": str(cfg.get("updated_by") or ""),
                    "updated_at": str(cfg.get("updated_at") or ""),
                }
            if bucket:
                normalized[str(guild_id)] = bucket
    notification_channels = normalized
    total = sum(len(v) for v in notification_channels.values())
    print(f"✅ load_notification_channels สำเร็จ ({total} enabled channel(s))")
    return notification_channels


def _notification_channel_ids_from_memory():
    """Return enabled persistent notification channel IDs, de-duplicated globally."""
    ids = []
    seen = set()
    with schedule_lock:
        snapshot = {str(gid): dict(chs) for gid, chs in (notification_channels or {}).items()}
    for _, channels in snapshot.items():
        for channel_id, cfg in channels.items():
            if not isinstance(cfg, dict) or not parse_bool(cfg.get("enabled"), True):
                continue
            try:
                cid = int(cfg.get("channel_id", channel_id))
            except (TypeError, ValueError):
                continue
            if cid not in seen:
                ids.append(cid)
                seen.add(cid)
    return ids


async def get_notification_channel_ids_from_database():
    """Fetch the canonical notification_channels root immediately before a Boss send."""
    global notification_channels
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(db.reference("notification_channels").get), timeout=8
        )
        if isinstance(data, dict):
            # Reuse the same normalization logic without adding another Firebase write.
            normalized = {}
            for guild_id, guild_data in data.items():
                if not isinstance(guild_data, dict):
                    continue
                source = guild_data.get("channels") if isinstance(guild_data.get("channels"), dict) else guild_data
                bucket = {}
                for channel_id, cfg in source.items():
                    if not isinstance(cfg, dict) or not parse_bool(cfg.get("enabled"), True):
                        continue
                    cid = cfg.get("channel_id", channel_id)
                    try:
                        cid = int(cid)
                    except (TypeError, ValueError):
                        continue
                    bucket[str(cid)] = {
                        "guild_id": int(cfg.get("guild_id", guild_id)),
                        "channel_id": cid,
                        "channel_name": str(cfg.get("channel_name") or ""),
                        "enabled": True,
                        "updated_by": str(cfg.get("updated_by") or ""),
                        "updated_at": str(cfg.get("updated_at") or ""),
                    }
                if bucket:
                    normalized[str(guild_id)] = bucket
            notification_channels = normalized
            return _notification_channel_ids_from_memory()
    except Exception as e:
        print(f"⚠️ อ่าน notification_channels สดจาก Firebase ไม่สำเร็จ; ใช้ cache: {e}")
    return _notification_channel_ids_from_memory()


async def refresh_discord_notification_languages():
    """Refresh only Discord notification language switches; do not reload TTS/BF/Voice settings."""
    global discord_notify_th_enabled, discord_notify_en_enabled, discord_notify_ko_enabled
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(db.reference("bot_settings").get), timeout=8
        )
        if isinstance(data, dict):
            discord_notify_th_enabled = parse_bool(data.get("discord_notify_th_enabled"), discord_notify_th_enabled)
            discord_notify_en_enabled = parse_bool(data.get("discord_notify_en_enabled"), discord_notify_en_enabled)
            discord_notify_ko_enabled = parse_bool(data.get("discord_notify_ko_enabled"), discord_notify_ko_enabled)
            return True
    except Exception as e:
        print(f"⚠️ อ่าน Discord notification languages สดจาก Firebase ไม่สำเร็จ; ใช้ค่าเดิม: {e}")
    return False


async def save_bot_settings():
    """Persist notification/TTS switches to Firebase and local SQLite."""
    data = {
        "bf_notify_enabled": bool(bf_notify_enabled),
        "lib_notify_enabled": bool(lib_notify_enabled),
        "ppl_notify_enabled": bool(ppl_notify_enabled),
        "tts_th_enabled": bool(tts_th_enabled),
        "tts_en_enabled": bool(tts_en_enabled),
        "tts_ko_enabled": bool(tts_ko_enabled),
        "discord_notify_th_enabled": bool(discord_notify_th_enabled),
        "discord_notify_en_enabled": bool(discord_notify_en_enabled),
        "discord_notify_ko_enabled": bool(discord_notify_ko_enabled),
    }
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("bot_settings").update, data), timeout=8
        )
    except Exception as e:
        print(f"⚠️ บันทึก bot_settings ลง Firebase ไม่สำเร็จ: {e}")
    await asyncio.to_thread(set_db_value, "bot_settings", data)
    await asyncio.to_thread(save_json_local, SETTINGS_FILE, data)

async def load_bot_settings():
    """Load notification/TTS switches. Firebase is canonical; local storage is fallback."""
    global bf_notify_enabled, lib_notify_enabled, ppl_notify_enabled
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    global discord_notify_th_enabled, discord_notify_en_enabled, discord_notify_ko_enabled
    data = None
    try:
        data = await asyncio.to_thread(db.reference("bot_settings").get)
    except Exception as e:
        print(f"⚠️ โหลด bot_settings จาก Firebase ไม่สำเร็จ: {e}")
    if not isinstance(data, dict) or not data:
        data = get_db_value("bot_settings", None)
    if isinstance(data, dict):
        bf_notify_enabled = parse_bool(data.get("bf_notify_enabled"), bf_notify_enabled)
        lib_notify_enabled = parse_bool(data.get("lib_notify_enabled"), lib_notify_enabled)
        ppl_notify_enabled = parse_bool(data.get("ppl_notify_enabled"), ppl_notify_enabled)
        tts_th_enabled = parse_bool(data.get("tts_th_enabled"), tts_th_enabled)
        tts_en_enabled = parse_bool(data.get("tts_en_enabled"), tts_en_enabled)
        tts_ko_enabled = parse_bool(data.get("tts_ko_enabled"), tts_ko_enabled)
        discord_notify_th_enabled = parse_bool(data.get("discord_notify_th_enabled"), discord_notify_th_enabled)
        discord_notify_en_enabled = parse_bool(data.get("discord_notify_en_enabled"), discord_notify_en_enabled)
        discord_notify_ko_enabled = parse_bool(data.get("discord_notify_ko_enabled"), discord_notify_ko_enabled)
    print("✅ load_bot_settings สำเร็จ")

async def load_custom_bosses():
    """Load custom boss definitions from Firebase/local fallback."""
    global custom_bosses
    data = None
    try:
        data = await asyncio.to_thread(db.reference("custom_bosses").get)
    except Exception as e:
        print(f"⚠️ โหลด custom_bosses จาก Firebase ไม่สำเร็จ: {e}")
    if not isinstance(data, dict) or not data:
        data = get_db_value("custom_bosses", None)
    custom_bosses = data if isinstance(data, dict) else {}
    loaded = 0
    for name, cfg in custom_bosses.items():
        if not isinstance(cfg, dict) or not str(name).strip():
            continue
        try:
            seconds = int(cfg.get("respawnSeconds", 0) or 0)
            if seconds <= 0:
                continue
            canonical = str(name).strip()
            BOSS_RESPAWN_TIMES[canonical] = timedelta(seconds=seconds)
            notice = max(1, int(cfg.get("noticeMinutes", 5) or 5))
            ADVANCE_NOTICE_SECONDS[canonical] = notice * 60
            ADVANCE_NOTICE_TEXT[canonical] = f"{notice} นาที"
            BOSS_CD_TEXT[canonical] = str(cfg.get("cdText") or "").strip() or f"{seconds // 3600} ชั่วโมง {(seconds % 3600) // 60} นาที {seconds % 60} วินาที"
            BOSS_PRONUNCIATION[canonical] = str(cfg.get("pronunciation") or canonical)
            loaded += 1
        except (TypeError, ValueError):
            continue
    print(f"✅ load_custom_bosses สำเร็จ ({loaded} custom bosses)")

async def save_custom_bosses_to_github():
    """Persist custom boss definitions durably in Firebase (canonical)."""
    data = {str(k): dict(v) for k, v in (custom_bosses or {}).items() if isinstance(v, dict)}
    if not data:
        print("ℹ️ custom_bosses ว่าง — ไม่เขียนทับข้อมูล Firebase เดิม")
        return True
    try:
        # Save each boss separately first, so one bad record cannot erase the others.
        for boss_name, cfg in data.items():
            await asyncio.wait_for(
                asyncio.to_thread(db.reference(f"custom_bosses/{boss_name}").set, cfg), timeout=8
            )
        # Then keep a complete root snapshot for compatibility.
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("custom_bosses").update, data), timeout=8
        )
        print(f"💾 custom_bosses saved to Firebase: {len(data)} boss(es)")
        ok = True
    except Exception as e:
        print(f"❌ บันทึก custom_bosses ลง Firebase ไม่สำเร็จ: {e}")
        ok = False
    await asyncio.to_thread(set_db_value, "custom_bosses", data)
    await asyncio.to_thread(save_json_local, CUSTOM_BOSSES_FILE, data)
    return ok

async def load_live_config():
    global live_message_config
    data = None
    try:
        data = await asyncio.to_thread(db.reference("live_message_config").get)
    except Exception as e:
        print(f"⚠️ โหลด live_message_config จาก Firebase ไม่สำเร็จ: {e}")
    if not isinstance(data, dict) or not data:
        data = get_db_value("live_message_config", None)
    live_message_config = data if isinstance(data, dict) else {}
    print("✅ load_live_config สำเร็จ")

async def save_live_config():
    data = dict(live_message_config or {})
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("live_message_config").set, data), timeout=8
        )
    except Exception as e:
        print(f"⚠️ บันทึก live_message_config ลง Firebase ไม่สำเร็จ: {e}")
    await asyncio.to_thread(set_db_value, "live_message_config", data)
    await asyncio.to_thread(save_json_local, LIVE_CONFIG_FILE, data)

async def load_vip_config():
    global vip_config
    data = None
    try:
        data = await asyncio.to_thread(db.reference("vip_config").get)
    except Exception as e:
        print(f"⚠️ โหลด vip_config จาก Firebase ไม่สำเร็จ: {e}")
    if not isinstance(data, dict) or not data:
        data = get_db_value("vip_config", None)
    if isinstance(data, dict):
        vip_config = {
            "enabled": parse_bool(data.get("enabled"), False),
            "user_id": int(data["user_id"]) if str(data.get("user_id", "")).isdigit() else None,
            "user_name": str(data.get("user_name", "")),
            "message": str(data.get("message", "")),
        }
    print("✅ load_vip_config สำเร็จ")

async def save_vip_config():
    data = dict(vip_config or {})
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("vip_config").set, data), timeout=8
        )
    except Exception as e:
        print(f"⚠️ บันทึก vip_config ลง Firebase ไม่สำเร็จ: {e}")
    await asyncio.to_thread(set_db_value, "vip_config", data)
    await asyncio.to_thread(save_json_local, VIP_CONFIG_FILE, data)

async def load_voice_config():
    """Load and normalize persisted Voice targets. Supports legacy single-channel records and new multi-channel records."""
    global voice_config
    data = None
    try:
        data = await asyncio.to_thread(db.reference("voice_config").get)
    except Exception as e:
        print(f"⚠️ โหลด voice_config จาก Firebase ไม่สำเร็จ: {e}")
    if not data:
        data = get_db_value("voice_config", None)

    normalized = {}
    if isinstance(data, dict):
        for gid, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            guild_id = int(gid) if str(gid).isdigit() else gid
            channels = {}

            # New schema: channels = {channel_id: {...}}
            raw_channels = cfg.get("channels")
            if isinstance(raw_channels, dict):
                for cid, cdata in raw_channels.items():
                    if not isinstance(cdata, dict):
                        continue
                    try:
                        channel_id = int(cdata.get("voice_channel_id") or cdata.get("voiceChannelId") or cid)
                    except (TypeError, ValueError):
                        continue
                    channels[str(channel_id)] = {
                        "voice_channel_id": channel_id,
                        "guild_id": guild_id,
                        "channel_name": cdata.get("channel_name", ""),
                        "enabled": parse_bool(cdata.get("enabled", True), True),
                        "updated_by": cdata.get("updated_by", cfg.get("updated_by", "")),
                        "updated_at": cdata.get("updated_at", cfg.get("updated_at", ""))
                    }

            # Legacy schema: one voice_channel_id at guild root. Keep it.
            if not channels:
                legacy_id = cfg.get("voice_channel_id") or cfg.get("voiceChannelId")
                if legacy_id:
                    try:
                        channel_id = int(legacy_id)
                        channels[str(channel_id)] = {
                            "voice_channel_id": channel_id,
                            "guild_id": guild_id,
                            "channel_name": cfg.get("channel_name", ""),
                            "enabled": parse_bool(cfg.get("enabled", True), True),
                            "updated_by": cfg.get("updated_by", ""),
                            "updated_at": cfg.get("updated_at", "")
                        }
                    except (TypeError, ValueError):
                        pass

            if channels:
                normalized[str(gid)] = {
                    "guild_id": guild_id,
                    "channels": channels,
                    "enabled": parse_bool(cfg.get("enabled", True), True),
                    "mode": cfg.get("mode", "on-demand"),
                    "updated_by": cfg.get("updated_by", ""),
                    "updated_at": cfg.get("updated_at", "")
                }

    voice_config = normalized
    total_targets = sum(len(cfg.get("channels", {})) for cfg in voice_config.values())
    print(f"🔊 โหลด voice_config สำเร็จ {total_targets} ห้อง / {len(voice_config)} เซิร์ฟเวอร์")


def get_configured_voice_channels(guild: discord.Guild):
    """Return all configured /setvoice channels for a guild (new + legacy schema)."""
    if not guild:
        return []
    cfg = voice_config.get(str(guild.id))
    if not cfg or not parse_bool(cfg.get("enabled", True), True):
        return []

    channels = []
    raw_channels = cfg.get("channels")
    if isinstance(raw_channels, dict):
        for cdata in raw_channels.values():
            if not isinstance(cdata, dict) or not parse_bool(cdata.get("enabled", True), True):
                continue
            channel_id = cdata.get("voice_channel_id")
            try:
                channel_id = int(channel_id) if channel_id is not None else None
            except (TypeError, ValueError):
                channel_id = None
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                channels.append(channel)
    else:
        channel_id = cfg.get("voice_channel_id") or cfg.get("voiceChannelId")
        try:
            channel_id = int(channel_id) if channel_id is not None else None
        except (TypeError, ValueError):
            channel_id = None
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                channels.append(channel)
    # Stable order and de-duplicate by channel id.
    seen = set()
    result = []
    for channel in channels:
        if channel.id not in seen:
            seen.add(channel.id)
            result.append(channel)
    return result


def get_configured_voice_channel(guild: discord.Guild):
    """Backward-compatible first configured channel."""
    channels = get_configured_voice_channels(guild)
    return channels[0] if channels else None

def get_occupied_voice_channels(guild: discord.Guild):
    """Return VoiceChannels that currently contain at least one human member."""
    if not guild:
        return []
    return [
        channel for channel in guild.voice_channels
        if any(not member.bot for member in channel.members)
    ]


async def ensure_configured_voice(guild: discord.Guild):
    """Legacy compatibility: configured Voice is ON-DEMAND, never persistent."""
    return None


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
async def refresh_tts_settings_from_firebase():
    """Read bot_settings directly from Firebase immediately before TTS generation.
    Firebase is the single source of truth; local SQLite/files are not used to decide
    which languages the Discord bot speaks.
    """
    global tts_th_enabled, tts_en_enabled, tts_ko_enabled
    try:
        data = await asyncio.to_thread(db.reference("bot_settings").get)
        if isinstance(data, dict):
            tts_th_enabled = parse_bool(data.get("tts_th_enabled"), False)
            tts_en_enabled = parse_bool(data.get("tts_en_enabled"), False)
            tts_ko_enabled = parse_bool(data.get("tts_ko_enabled"), False)
        else:
            tts_th_enabled = tts_en_enabled = tts_ko_enabled = False
    except Exception as e:
        print(f"❌ TTS settings refresh from Firebase failed: {e}")
        return False
    print(f"🔐 Effective TTS settings: TH={tts_th_enabled} EN={tts_en_enabled} KO={tts_ko_enabled}")
    return True

async def _tts_generate_files(text_th=None, text_en=None, text_ko=None, guild_id=0):
    """Generate TTS with live Firebase settings and bounded recovery.

    Keep the configured voice as the primary voice. If Edge TTS returns
    ``NoAudioReceived`` for that voice, make one bounded fallback attempt with
    another currently supported voice in the same language. This is intentionally
    limited to TTS generation and does not change Firebase, Boss, Voice, or command
    behavior.
    """
    await refresh_tts_settings_from_firebase()
    actual = []
    if tts_th_enabled and text_th:
        actual.append(("th", text_th, VOICE_THAI, "-20%", "+10Hz"))
    if tts_en_enabled and text_en:
        actual.append(("en", text_en, VOICE_ENG, "-10%", "+0Hz"))
    if tts_ko_enabled and text_ko:
        actual.append(("ko", text_ko, VOICE_KOR, "-10%", "+0Hz"))

    # Fallbacks are used only after the configured voice has exhausted its
    # normal bounded retries. They preserve the language and avoid retry storms.
    tts_voice_fallbacks = {
        "th": ["th-TH-AcharaNeural"],
        "en": ["en-US-JennyNeural"],
        "ko": ["ko-KR-JiMinNeural"],
    }

    files = []
    uid = uuid.uuid4().hex
    for lang, text, voice, rate, pitch in actual:
        filename = f"temp_tts_{lang}_{guild_id}_{uid}.mp3"
        success = False
        last_error = None

        # Phase 1: configured voice, unchanged from the existing 3-attempt policy.
        for attempt in range(1, 4):
            try:
                if os.path.exists(filename):
                    os.remove(filename)
                # First attempt keeps configured prosody; retries fall back to plain voice
                # because the upstream TTS service can intermittently reject rate/pitch.
                if attempt == 1:
                    communicator = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                else:
                    communicator = edge_tts.Communicate(text, voice)
                await communicator.save(filename)
                if os.path.exists(filename) and os.path.getsize(filename) > 256:
                    files.append((lang, filename))
                    print(f"🔊 TTS สร้างไฟล์สำเร็จ: {lang} ({guild_id}) voice={voice} attempt={attempt}")
                    success = True
                    break
                last_error = RuntimeError("No audio was received")
            except Exception as e:
                last_error = e
                print(f"⚠️ TTS attempt {attempt}/3 failed ({lang}) voice={voice}: {e}")
                if attempt < 3:
                    await asyncio.sleep(0.8 * attempt)

        if success:
            continue

        # Phase 2: one fallback voice, only for a no-audio/voice-rejection class
        # failure. Do not fan out across many voices because that can create an
        # upstream retry storm.
        error_text = str(last_error or "")
        no_audio_failure = bool(last_error) and (
            "No audio was received" in error_text
            or last_error.__class__.__name__ == "NoAudioReceived"
        )
        if no_audio_failure:
            for fallback_voice in tts_voice_fallbacks.get(lang, []):
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                    print(f"🔁 TTS fallback voice | lang={lang} | primary={voice} | fallback={fallback_voice}")
                    await asyncio.sleep(1.5)
                    communicator = edge_tts.Communicate(text, fallback_voice)
                    await communicator.save(filename)
                    if os.path.exists(filename) and os.path.getsize(filename) > 256:
                        files.append((lang, filename))
                        print(f"✅ TTS fallback สำเร็จ: {lang} ({guild_id}) voice={fallback_voice}")
                        success = True
                        break
                    last_error = RuntimeError("No audio was received")
                except Exception as e:
                    last_error = e
                    print(f"⚠️ TTS fallback failed ({lang}) voice={fallback_voice}: {e}")

        if not success:
            print(f"❌ สร้าง TTS ไม่สำเร็จ ({lang}) หลัง primary 3 attempts + bounded fallback: {last_error}")
    return files


async def _play_tts_in_channel(guild, channel, files):
    """Join one active voice channel, play all TTS files, then leave.

    Robustness rules:
    - Verify the configured room is occupied and the bot has Connect/Speak permissions.
    - Reuse an existing connection only when it is healthy; otherwise reconnect.
    - Retry Voice connection once after a short backoff.
    - Wait for the audio player callback, so a successful function return means audio was actually played.
    """
    if not isinstance(channel, discord.VoiceChannel):
        return False

    humans = [m for m in channel.members if not m.bot]
    print(f"🔊 Voice target: {guild.name} -> {channel.name} | humans={len(humans)}")
    if not humans:
        print(f"⏭️ Voice skip: {guild.name} -> {channel.name} is empty")
        return False

    me = guild.me
    if me is not None:
        perms = channel.permissions_for(me)
        print(f"🔐 Voice permissions {guild.name} -> {channel.name}: connect={perms.connect} speak={perms.speak}")
        if not perms.connect or not perms.speak:
            print(f"❌ Voice permission denied: {guild.name}/{channel.name}")
            return False

    vc = guild.voice_client
    connected_here = False
    try:
        # Remove a stale connection before starting a fresh on-demand session.
        if vc and vc.is_connected() and vc.channel and vc.channel.id != channel.id:
            try:
                print(f"🔄 Voice moving: {guild.name} -> {vc.channel.name} => {channel.name}")
                await vc.move_to(channel)
                connected_here = True
            except Exception as exc:
                print(f"⚠️ Voice move failed, reconnecting: {guild.name}/{channel.name}: {exc}")
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None

        if not vc or not vc.is_connected():
            last_exc = None
            for attempt in range(1, 3):
                try:
                    print(f"🔌 Voice connect attempt {attempt}/2: {guild.name} -> {channel.name}")
                    vc = await channel.connect(reconnect=True, timeout=20, self_deaf=False, self_mute=False)
                    if vc and vc.is_connected():
                        connected_here = True
                        print(f"✅ Voice connect success: {guild.name} -> {channel.name}")
                        break
                except Exception as exc:
                    last_exc = exc
                    print(f"⚠️ Voice connect attempt {attempt}/2 failed: {guild.name}/{channel.name}: {exc}")
                    await asyncio.sleep(1.2 * attempt)
            if not vc or not vc.is_connected():
                print(f"❌ Voice connect failed: {guild.name}/{channel.name}: {last_exc}")
                return False

        # Give the Discord voice websocket and UDP path time to become ready.
        await asyncio.sleep(0.8)
        print(f"🎙️ Voice ready for playback: {guild.name} -> {channel.name}")

        loop = asyncio.get_running_loop()
        played_any = False
        for index, (lang, filename) in enumerate(files, start=1):
            if not os.path.exists(filename) or os.path.getsize(filename) <= 256:
                print(f"⚠️ Skip empty/missing TTS file: {lang} -> {filename}")
                continue
            if not vc.is_connected():
                print(f"❌ Voice disconnected before playback: {guild.name}/{channel.name}")
                break

            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.25)

            done = asyncio.Event()
            playback_error = {"error": None}

            def after(error, event=done, err_holder=playback_error, lang_name=lang):
                err_holder["error"] = error
                if error:
                    print(f"❌ เล่น TTS {lang_name} ผิดพลาดใน {guild.name}: {error}")
                loop.call_soon_threadsafe(event.set)

            try:
                # Use FFmpeg -> Opus directly for Discord voice playback. This avoids
                # an extra PCM -> Opus encoding path and is more reliable on Render.
                source = discord.FFmpegOpusAudio(
                    filename,
                    executable=get_ffmpeg_path(),
                    before_options="-nostdin -hide_banner -loglevel error",
                    options="-vn -application lowdelay -frame_duration 20",
                    bitrate=128
                )
                print(f"▶️ กำลังเล่น TTS: {lang} | {guild.name} -> {channel.name}")
                vc.play(source, after=after)
                # Confirm discord.py actually transitioned into PLAYING. A callback
                # alone can fire even when the source stops immediately.
                playing_deadline = loop.time() + 5
                while not vc.is_playing() and loop.time() < playing_deadline:
                    if playback_error["error"] is not None:
                        break
                    await asyncio.sleep(0.1)
                if not vc.is_playing() and playback_error["error"] is None:
                    print(f"❌ TTS playback did not enter PLAYING state: {guild.name}/{channel.name}/{lang}")
                    try:
                        source.cleanup()
                    except Exception:
                        pass
                    continue
            except Exception as exc:
                print(f"❌ เริ่มเล่นเสียง TTS ไม่สำเร็จ: {guild.name}/{channel.name}/{lang}: {exc}")
                continue

            try:
                await asyncio.wait_for(done.wait(), timeout=90)
            except asyncio.TimeoutError:
                print(f"⏱️ TTS playback timeout: {guild.name}/{channel.name}/{lang}")
                try:
                    if vc.is_playing():
                        vc.stop()
                except Exception:
                    pass
                continue

            if playback_error["error"] is None:
                played_any = True
                print(f"✅ TTS playback complete: {lang} | {guild.name} -> {channel.name}")
            if index < len(files):
                await asyncio.sleep(0.35)

        return played_any
    except Exception as e:
        print(f"❌ Voice broadcast failed {guild.name}/{channel.name}: {e}")
        traceback.print_exc()
        return False
    finally:
        # On-demand mode: disconnect after playback.
        try:
            vc_now = guild.voice_client
            if vc_now and vc_now.is_connected():
                await vc_now.disconnect(force=True)
                print(f"🔌 TTS จบแล้ว ออกจาก Voice: {guild.name} -> {channel.name}")
        except Exception as e:
            print(f"⚠️ ออกจาก Voice ไม่สำเร็จ: {e}")


async def speak_in_guild(guild: discord.Guild, text_th=None, text_en=None, text_ko=None,
                         target_channel: discord.VoiceChannel = None):
    """
    ON-DEMAND MULTI-CHANNEL:
    - ถ้ามี target_channel ให้ประกาศห้องนั้น
    - ถ้าไม่ระบุ ให้ไล่ทุกห้อง Voice ที่มีสมาชิกจริงทีละห้อง
    - ไม่ค้าง connection หลังพูด
    - ไม่พึ่ง /setvoice เพื่อให้ /notice และ boss notification ทำงานได้
    """
    if not guild:
        return False

    if guild.id not in voice_locks:
        voice_locks[guild.id] = asyncio.Lock()

    async with voice_locks[guild.id]:
        channels = []
        if target_channel and isinstance(target_channel, discord.VoiceChannel):
            # V70: final occupancy check closes the race where a user leaves
            # between the scheduler filter and the Voice connection.
            if any(not m.bot for m in target_channel.members):
                channels = [target_channel]
            else:
                print(f"⏭️ Voice target became empty before connect: {guild.name} -> {target_channel.name}")
        else:
            channels = [
                ch for ch in guild.voice_channels
                if any(not m.bot for m in ch.members)
            ]

        if not channels:
            print(f"⏭️ ไม่มีห้อง Voice ที่มีสมาชิกสำหรับ TTS: {guild.name}")
            return False

        files = await _tts_generate_files(text_th, text_en, text_ko, guild.id)
        if not files:
            return False

        success = False
        try:
            for channel in channels:
                print(f"🔊 TTS -> {guild.name} -> {channel.name} | humans={len([m for m in channel.members if not m.bot])}")
                if await _play_tts_in_channel(guild, channel, files):
                    success = True
                await asyncio.sleep(0.4)
        finally:
            for _, filename in files:
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                except Exception:
                    pass
        return success

# ==========================================
# 🔊 Event แจ้งเตือน + ทักทายเมื่อมีคนเข้าห้องเสียง
# ==========================================
# ==========================================
# 🤖 Discord Bot object
# CRITICAL: must exist before any @bot.event / @bot.tree.command decorators.
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# V53: start.py owns command sync, so protect that call from a persisted REST block
# without changing the 17 registered commands or the sync strategy itself.
_original_tree_sync = bot.tree.sync
async def _guarded_tree_sync(*args, **kwargs):
    await wait_for_discord_rest_startup_gate(context="startup:tree-sync")
    try:
        result = await _original_tree_sync(*args, **kwargs)
        global discord_background_rest_startup_hold
        discord_background_rest_startup_hold = False
        print(
            "🟢 Background Discord REST startup hold released | command sync succeeded",
            flush=True,
        )
        # Tree sync is a confirmed successful foreground REST request, but it is
        # intentionally outside guarded_discord_call.  V59 therefore arms the
        # existing background recovery grace here too, so queued boss/audit REST
        # cannot immediately become the first post-block probe.
        had_active_block = False
        with discord_block_lock:
            had_active_block = discord_block_started_at > 0
        _clear_discord_block_after_success(context="startup:tree-sync")
        if had_active_block:
            _arm_background_rest_after_foreground_recovery()
        return result
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            _apply_discord_rest_429(exc, context="startup:tree-sync")
        raise

bot.tree.sync = _guarded_tree_sync

async def _retry_pending_voice_confirmations_for_guild(guild: discord.Guild):
    """Retry pending Dashboard confirmations when a human joins a configured Voice room."""
    if not guild or not is_bot_ready:
        return
    configured = get_configured_voice_channels(guild)
    if not configured or not any(any(not m.bot for m in ch.members) for ch in configured):
        return

    candidates = []
    with schedule_lock:
        for boss_name, item in boss_schedule.items():
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("confirmationRequestId") or "").strip()
            status = str(item.get("confirmationStatus") or "pending").strip().lower()
            if request_id and status == "pending":
                candidates.append((boss_name, dict(item)))

    for boss_name, item in candidates:
        try:
            queue_voice_confirmation(boss_name, item, source="voice-join", wait=False)
        except Exception as exc:
            print(f"⚠️ Pending Voice confirmation retry failed | boss={boss_name} | {exc}", flush=True)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    global ppl_notify_enabled, vip_config
    if member.bot: return

    if before.channel != after.channel and after.channel is not None:
        if not member.bot:
            asyncio.create_task(_retry_pending_voice_confirmations_for_guild(member.guild))
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
    global is_bot_ready, bot_event_loop
    bot_event_loop = asyncio.get_running_loop()
    if is_bot_ready:
        print("🔄 บอท Reconnect สำเร็จ (ข้ามการโหลดข้อมูลซ้ำ)")
        return
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🔊 ใช้ FFmpeg จากตำแหน่ง: {get_ffmpeg_path()}")

    init_db()
    await restore_persisted_discord_block_state()
    await load_bot_settings()
    print(f"🔐 Startup TTS settings: TH={tts_th_enabled} EN={tts_en_enabled} KO={tts_ko_enabled}")
    print(f"🔔 Startup notification settings: BF={bf_notify_enabled} LIB={lib_notify_enabled} PPL={ppl_notify_enabled}")
    await load_custom_bosses()
    await load_boss_data()
    await ensure_library_boss_schedule_records()
    await load_live_config()
    await load_vip_config()
    await load_voice_config()
    await load_notification_channels()
    await load_attendance_config()

    # Voice is ON-DEMAND: do not connect on startup. /setvoice only stores the target channel.
    print("🟢 Voice mode: ON-DEMAND GLOBAL (occupied-room announcements; connect only when speaking, disconnect after TTS)")

    with discord_block_lock:
        startup_gate_remaining = max(0.0, discord_block_next_probe_mono - time.monotonic()) if discord_block_started_at > 0 else 0.0
    print(
        f"🛡️ Discord REST startup gate status: {'BLOCKED' if startup_gate_remaining > 0 else 'READY'} | "
        f"hold={_format_duration(startup_gate_remaining) if startup_gate_remaining > 0 else '0m 00s'}",
        flush=True,
    )

    await asyncio.sleep(3)
    
    if not check_boss_notifications.is_running(): check_boss_notifications.start()
    if not check_bf_notifications.is_running(): check_bf_notifications.start()
    if not check_library_boss_notifications.is_running(): check_library_boss_notifications.start()
    if not update_live_embed.is_running(): update_live_embed.start()
    if not check_auto_disconnect.is_running(): check_auto_disconnect.start()
    if not flush_pending_command_outputs.is_running(): flush_pending_command_outputs.start()
    if not attendance_lifecycle_loop.is_running(): attendance_lifecycle_loop.start()
    if not attendance_monthly_report_loop.is_running(): attendance_monthly_report_loop.start()
    asyncio.create_task(restore_raid_attendance_views(), name="restore-raid-attendance-views")
    if not getattr(discord_block_diagnostics_loop, "_started", False):
        discord_block_diagnostics_loop._started = True
        asyncio.create_task(discord_block_diagnostics_loop(), name="discord-block-diagnostics")

    is_bot_ready = True
    loop = bot_event_loop
    threading.Thread(target=start_firebase_listener, args=(loop,), daemon=True).start()
    threading.Thread(target=start_attendance_firebase_listener, daemon=True).start()

    # Drain Dashboard confirmations that arrived while Gateway was unavailable.
    if _pending_voice_confirmations:
        pending = list(_pending_voice_confirmations.values())
        _pending_voice_confirmations.clear()
        print(f"🔁 Draining pending Voice confirmations after READY: {len(pending)}")
        for boss_name, item, source in pending:
            req_id = str(item.get("confirmationRequestId") or "").strip()
            if req_id:
                _confirmation_queue_ids.add(req_id)
            asyncio.create_task(_voice_confirm_boss_recording(boss_name, dict(item)))

# ==========================================
# ⏰ 7. Tasks เช็กเวลาเตือน + BF + Library Boss + Live Embed + Auto-Disconnect
# ==========================================
async def _get_retry_after_seconds(exc, default=30.0):
    """Best-effort extraction of Discord rate-limit retry delay."""
    for attr in ("retry_after",):
        try:
            value = float(getattr(exc, attr))
            if value >= 0:
                return min(max(value, 1.0), 900.0)
        except Exception:
            pass
    try:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            for key in ("Retry-After", "retry-after"):
                raw = headers.get(key)
                if raw is not None:
                    value = float(raw)
                    return min(max(value, 1.0), 900.0)
        data = getattr(exc, "response", None)
        payload = getattr(data, "data", None)
        if isinstance(payload, dict) and payload.get("retry_after") is not None:
            value = float(payload["retry_after"])
            return min(max(value, 1.0), 900.0)
    except Exception:
        pass
    return float(default)

async def _safe_interaction_ack(interaction: discord.Interaction, *, ephemeral=True):
    """Send exactly one time-critical initial Interaction ACK.

    Interaction callback endpoints are not part of the bot Global Rate Limit.  Therefore
    an ACK 429 must NOT open the shared REST circuit breaker.  We also do not retry a
    long Retry-After because Discord requires the initial response within 3 seconds.
    If Discord itself temporarily rejects the callback (for example during an API/IP
    restriction), the command continues its Voice work, but the Discord client may show
    "The application did not respond" until the restriction clears.
    """
    global discord_rest_rate_limited_until, discord_block_next_probe_mono
    try:
        if interaction.response.is_done():
            return True
    except Exception:
        pass

    try:
        # One immediate ACK attempt only.  Do not sleep for Retry-After and do not touch
        # the shared REST cooldown; this endpoint is intentionally isolated from it.
        await asyncio.wait_for(
            interaction.response.defer(ephemeral=ephemeral),
            timeout=1.75,
        )
        return True
    except asyncio.TimeoutError:
        print("⚠️ Interaction ACK timed out before Discord accepted the callback", flush=True)
        return False
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            retry_after = 0.0
            try:
                retry_after = float(getattr(exc, "retry_after", 0) or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None) or {}
            cf_ray = headers.get("CF-RAY") or headers.get("cf-ray") or "-"
            via = headers.get("Via") or headers.get("via") or "-"
            date_header = headers.get("Date") or headers.get("date") or "-"
            _record_discord_block_observed(exc, context="interaction-ack", source="INTERACTION")
            with discord_block_lock:
                if discord_block_temp_restriction:
                    server_retry = max(0.0, discord_block_server_until - time.time())
                    gate_hold = server_retry + discord_block_recovery_grace_seconds if server_retry > 0 else 60.0
                    discord_block_next_probe_mono = max(
                        discord_block_next_probe_mono,
                        time.monotonic() + gate_hold,
                    )
                    discord_rest_rate_limited_until = max(
                        discord_rest_rate_limited_until,
                        time.monotonic() + gate_hold,
                    )
            _schedule_persist_discord_block_state(reason="interaction-ack-429")
            _mark_discord_block_log("interaction-ack", force=True)
            print(
                f"⚠️ Interaction ACK rejected by Discord 429 | retry_after={retry_after:.2f}s | "
                f"cf_ray={cf_ray} | via={via} | date={date_header} | shared REST cooldown unchanged",
                flush=True,
            )
            return False
        print(f"❌ Interaction ACK failed: {exc}", flush=True)
        return False
    except Exception as exc:
        print(f"❌ Interaction ACK failed unexpectedly: {exc!r}", flush=True)
        return False


async def _safe_interaction_send_message(interaction: discord.Interaction, content=None, *, ephemeral=True, **kwargs):
    """Best-effort initial interaction response for short-lived acknowledgement lanes."""
    global discord_rest_rate_limited_until, discord_block_next_probe_mono
    try:
        if interaction.response.is_done():
            return False
    except Exception:
        pass
    try:
        await asyncio.wait_for(
            interaction.response.send_message(content, ephemeral=ephemeral, **kwargs),
            timeout=1.75,
        )
        return True
    except asyncio.TimeoutError:
        print("⚠️ Interaction initial response timed out before Discord accepted the callback", flush=True)
        return False
    except discord.HTTPException as exc:
        retry_after = 0.0
        try:
            retry_after = float(getattr(exc, "retry_after", 0) or 0)
        except (TypeError, ValueError):
            pass
        if getattr(exc, "status", None) == 429:
            # V57: interaction callback failures are logged in their own lane.
            # They must not create or extend the background REST circuit breaker.
            print(
                f"⚠️ Interaction initial response rejected by Discord 429 | retry_after={retry_after:.2f}s | "
                "REST circuit breaker unchanged | lane=INTERACTION_ONLY",
                flush=True,
            )
        else:
            print(f"⚠️ Interaction initial response failed: {exc}", flush=True)
        return False
    except Exception as exc:
        print(f"⚠️ Interaction initial response failed unexpectedly: {exc!r}", flush=True)
        return False


# ==========================================
# V36 RESTORED DEFINITIONS
# ==========================================

@bot.tree.command(name="panel", description="ส่งข้อความ Interactive Embed พร้อมปุ่มกด Quick Actions ในช่องนี้")
@has_allowed_role()
async def send_quick_panel(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
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
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed, view=view)
    embed_summary, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
    if tts_text_th and interaction.guild:
        asyncio.create_task(speak_in_guild(interaction.guild, text_th=tts_text_th, text_en=tts_text_en, text_ko=tts_text_ko))


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
    await _safe_interaction_ack(interaction, ephemeral=False)
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
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่า TTS เสียง (/tts)", f"ภาษา: `{lang.value.upper()}` | สถานะ: `{status.value.upper()}`", color)


@bot.tree.command(name="notify", description="เปิดหรือปิดระบบแจ้งเตือนสงคราม Battlefield (BF)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"), app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")])
@has_allowed_role()
async def toggle_notify(interaction: discord.Interaction, status: app_commands.Choice[str]):
    await _safe_interaction_ack(interaction, ephemeral=False)
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
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่าการแจ้งเตือน BF (/notify)", f"เปลี่ยนสถานะเป็น: `{status.value.upper()}`", color)


@bot.tree.command(name="ppl", description="เปิดหรือปิดระบบแจ้งเตือนเสียงต้อนรับสมาชิกเข้าห้องเสียง (ทั่วไป)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"), app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")])
@has_allowed_role()
async def toggle_ppl_notify(interaction: discord.Interaction, status: app_commands.Choice[str]):
    await _safe_interaction_ack(interaction, ephemeral=False)
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
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
    await send_audit_log(interaction.guild, interaction.user, "ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)", f"เปลี่ยนสถานะเป็น: `{status.value.upper()}`", color)


@bot.tree.command(name="vip", description="[Admin Only] เปิด/ปิดและตั้งค่าระบบทักทายคนพิเศษ")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) ระบบทักทายคนพิเศษ", user="เลือกสมาชิกคนพิเศษ", message="ข้อความพูดทักทายคนพิเศษ")
@app_commands.choices(status=[app_commands.Choice(name="เปิดระบบทักทายคนพิเศษ (on)", value="on"), app_commands.Choice(name="ปิดระบบทักทายคนพิเศษ (off)", value="off")])
@app_commands.checks.has_permissions(administrator=True)
async def toggle_vip_greet(interaction: discord.Interaction, status: app_commands.Choice[str], user: discord.Member = None, message: str = None):
    await _safe_interaction_ack(interaction, ephemeral=False)
    global vip_config
    if status.value == "on":
        if not user or not message:
            await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ **ข้อมูลไม่ครบถ้วน!** กรุณาระบุทั้ง **user** และ **message**", ephemeral=True)
            return
        vip_config = {"enabled": True, "user_id": user.id, "user_name": user.display_name, "message": message}
        await save_vip_config()
        embed = discord.Embed(title="🌟 เปิดใช้งานระบบทักทายคนพิเศษ (VIP)", description=f"🟢 **สถานะ:** เปิดใช้งาน\n👤 **คนพิเศษ:** {user.mention}\n💬 **คำทักทาย:** \"{message}\"", color=discord.Color.gold())
        await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "เปิดระบบทักทายคนพิเศษ (/vip)", f"👤 คนพิเศษ: `{user.display_name}`\n💬 ข้อความ: {message}", discord.Color.gold())
    else:
        vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""}
        await save_vip_config()
        embed = discord.Embed(title="⚙️ ปิดระบบทักทายคนพิเศษ (VIP)", description="🔴 **สถานะ:** ปิดใช้งานเรียบร้อยแล้ว", color=discord.Color.red())
        await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "ปิดระบบทักทายคนพิเศษ (/vip)", "ยกเลิกข้อมูลคนพิเศษเรียบร้อยแล้ว", discord.Color.red())


@bot.tree.command(name="set-notification", description="ตั้งค่าห้อง Text Channel สำหรับรับการแจ้งเตือน Boss (บันทึก Channel ID ลงฐานข้อมูล)")
@app_commands.describe(
    channel="ห้อง Text Channel ที่ต้องการรับการแจ้งเตือน",
    action="เพิ่ม/เปิด, ปิด/ลบ หรือแสดงรายการห้องที่ตั้งไว้"
)
@app_commands.choices(action=[
    app_commands.Choice(name="เพิ่ม/เปิด", value="add"),
    app_commands.Choice(name="ปิด/ลบ", value="remove"),
    app_commands.Choice(name="แสดงรายการ", value="list"),
    app_commands.Choice(name="ตั้งห้องสรุป Attendance", value="attendance_summary"),
])
@has_allowed_role()
async def set_notification_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    action: app_commands.Choice[str] = None,
):
    """Persist one or more Discord text-notification targets per guild."""
    try:
        await _safe_interaction_ack(interaction, ephemeral=True)
    except Exception as e:
        print(f"❌ /set-notification defer failed: {e}")
        return

    guild = interaction.guild
    if guild is None:
        await guarded_interaction_followup_send(
            interaction, "interaction-followup",
            "❌ คำสั่งนี้ใช้ได้เฉพาะใน Server เท่านั้น",
            ephemeral=True,
        )
        return

    action_value = (action.value if action else "add")
    guild_key = str(guild.id)

    if action_value == "attendance_summary":
        if not isinstance(interaction.user, discord.Member) or not is_guild_admin_or_owner(interaction.user):
            await guarded_interaction_followup_send(
                interaction, "interaction-followup",
                "❌ การตั้งห้องสรุป Attendance อนุญาตเฉพาะ Admin หรือ Server Owner",
                ephemeral=True,
            )
            return
        if channel is None:
            await guarded_interaction_followup_send(
                interaction, "interaction-followup",
                "❌ กรุณาเลือก Text Channel สำหรับสรุป Attendance",
                ephemeral=True,
            )
            return
        me = guild.me or guild.get_member(bot.user.id if bot.user else 0)
        if me is not None:
            perms = channel.permissions_for(me)
            missing = []
            if not perms.view_channel: missing.append("View Channel")
            if not perms.send_messages: missing.append("Send Messages")
            if not perms.embed_links: missing.append("Embed Links")
            if missing:
                await guarded_interaction_followup_send(
                    interaction, "interaction-followup",
                    "❌ บอทไม่มีสิทธิ์ในห้องสรุปนี้: " + ", ".join(missing),
                    ephemeral=True,
                )
                return
        with schedule_lock:
            previous_cfg = dict(attendance_config.get(guild_key, {}) or {})
            attendance_config[guild_key] = {
                "guild_id": guild.id,
                "summary_channel_id": int(channel.id),
                "channel_name": channel.name,
                "updated_by": str(interaction.user.id),
                "updated_at": datetime.now(TZ_THAI).isoformat(),
                "autoattendance_enabled": parse_bool(previous_cfg.get("autoattendance_enabled"), False),
                "autoattendance_updated_by": str(previous_cfg.get("autoattendance_updated_by") or ""),
                "autoattendance_updated_at": str(previous_cfg.get("autoattendance_updated_at") or ""),
            }
        await save_attendance_config()
        await guarded_interaction_followup_send(
            interaction, "interaction-followup",
            f"📊 ตั้งห้องสรุป Attendance เป็น **#{channel.name}** สำเร็จ",
            ephemeral=True,
        )
        return
    action_value = (action.value if action else "add")
    guild_key = str(guild.id)
    with schedule_lock:
        current = dict(notification_channels.get(guild_key, {}) or {})

    if action_value == "list":
        enabled = []
        for cfg in current.values():
            if not isinstance(cfg, dict) or not parse_bool(cfg.get("enabled"), True):
                continue
            cid = cfg.get("channel_id")
            if not cid:
                continue
            resolved = guild.get_channel(int(cid))
            label = resolved.mention if resolved else f"`{cid}`"
            name = resolved.name if isinstance(resolved, discord.TextChannel) else (cfg.get("channel_name") or "unknown")
            enabled.append(f"• {label} — **#{name}** (`{cid}`)")
        text = "\n".join(enabled) if enabled else "- ยังไม่มีห้อง Text Channel ที่เปิดใช้งาน"
        await guarded_interaction_followup_send(
            interaction, "interaction-followup",
            f"📋 **ห้องแจ้งเตือน Boss ที่ตั้งไว้ใน {guild.name}**\n{text}",
            ephemeral=True,
        )
        return

    if channel is None:
        await guarded_interaction_followup_send(
            interaction, "interaction-followup",
            "❌ กรุณาเลือก Text Channel เช่น `#boss-notify`",
            ephemeral=True,
        )
        return

    me = guild.me or guild.get_member(bot.user.id if bot.user else 0)
    if me is not None:
        perms = channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.send_messages:
            missing.append("Send Messages")
        if not perms.embed_links:
            missing.append("Embed Links")
        if missing:
            await guarded_interaction_followup_send(
                interaction, "interaction-followup",
                "❌ บอทไม่มีสิทธิ์ในห้องนี้: " + ", ".join(missing),
                ephemeral=True,
            )
            return

    now_iso = datetime.now(TZ_THAI).isoformat()
    cid_key = str(channel.id)
    if action_value == "remove":
        current.pop(cid_key, None)
        with schedule_lock:
            if current:
                notification_channels[guild_key] = current
            else:
                notification_channels.pop(guild_key, None)
        await save_notification_channels()
        await guarded_interaction_followup_send(
            interaction, "interaction-followup",
            f"🔕 ปิดห้องแจ้งเตือน **#{channel.name}** (`{channel.id}`) แล้ว",
            ephemeral=True,
        )
        print(f"🔕 /set-notification removed | guild={guild.name} | channel={channel.name} ({channel.id})")
        return

    current[cid_key] = {
        "guild_id": guild.id,
        "channel_id": int(channel.id),
        "channel_name": channel.name,
        "enabled": True,
        "updated_by": str(interaction.user.id),
        "updated_at": now_iso,
    }
    with schedule_lock:
        notification_channels[guild_key] = current
    await save_notification_channels()

    configured_count = sum(
        1 for cfg in current.values()
        if isinstance(cfg, dict) and parse_bool(cfg.get("enabled"), True)
    )
    await guarded_interaction_followup_send(
        interaction, "interaction-followup",
        f"🔔 ตั้งห้องแจ้งเตือน **#{channel.name}** (`{channel.id}`) สำเร็จ\n"
        f"📋 ห้องที่เปิดใช้งานใน Server นี้: **{configured_count} ห้อง**\n"
        f"✅ Channel ID ถูกบันทึกลง `notification_channels` ใน Firebase แล้ว",
        ephemeral=True,
    )
    print(f"🔔 /set-notification saved | guild={guild.name} | channel={channel.name} ({channel.id}) | total={configured_count}")


@bot.tree.command(name="setvoice", description="เพิ่มห้อง Voice สำหรับ Boss TTS (เข้าเฉพาะตอนแจ้งเตือน)")
@app_commands.describe(channel="ห้อง Voice ที่ต้องการให้บอทใช้ประกาศ (เว้นว่าง = ห้องที่คุณอยู่)")
@has_allowed_role()
async def set_voice(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    """Add one Voice channel to the guild's persistent /setvoice targets."""
    try:
        await _safe_interaction_ack(interaction, ephemeral=True)
    except Exception as e:
        print(f"❌ /setvoice defer failed: {e}")
        return

    try:
        target = channel
        if target is None:
            if not interaction.user.voice or not interaction.user.voice.channel:
                await guarded_interaction_followup_send(interaction, "interaction-followup", 
                    "❌ กรุณาเข้าห้อง Voice ก่อน หรือเลือกห้อง Voice ในคำสั่ง /setvoice",
                    ephemeral=True
                )
                return
            target = interaction.user.voice.channel

        guild_id = interaction.guild.id
        cfg = voice_config.get(str(guild_id), {})
        channels = dict(cfg.get("channels") or {}) if isinstance(cfg, dict) else {}

        # Migrate a legacy single-channel record in memory before adding.
        legacy_id = cfg.get("voice_channel_id") if isinstance(cfg, dict) else None
        if legacy_id and not channels:
            try:
                legacy_id = int(legacy_id)
                channels[str(legacy_id)] = {
                    "voice_channel_id": legacy_id,
                    "guild_id": guild_id,
                    "channel_name": cfg.get("channel_name", ""),
                    "enabled": True,
                    "updated_by": cfg.get("updated_by", ""),
                    "updated_at": cfg.get("updated_at", "")
                }
            except (TypeError, ValueError):
                pass

        now_iso = datetime.now(TZ_THAI).isoformat()
        channels[str(target.id)] = {
            "voice_channel_id": int(target.id),
            "guild_id": guild_id,
            "channel_name": target.name,
            "enabled": True,
            "updated_by": str(interaction.user.id),
            "updated_at": now_iso
        }
        voice_config[str(guild_id)] = {
            "guild_id": guild_id,
            "channels": channels,
            "enabled": True,
            "mode": "on-demand",
            "updated_by": str(interaction.user.id),
            "updated_at": now_iso
        }

        await asyncio.wait_for(save_voice_config(), timeout=10)

        # Never keep a persistent Voice connection. Disconnect any stale one.
        vc = interaction.guild.voice_client
        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as e:
                print(f"⚠️ /setvoice could not clear old Voice connection: {e}")

        configured_names = []
        for c in get_configured_voice_channels(interaction.guild):
            configured_names.append(f"• **{c.name}** (`{c.id}`)")
        targets_text = "\n".join(configured_names) if configured_names else "-"

        await guarded_interaction_followup_send(interaction, "interaction-followup", 
            f"🔊 เพิ่มห้อง Voice **{target.name}** สำเร็จ\n"
            f"\n📋 ห้องที่ตั้ง /setvoice ไว้ทั้งหมด ({len(configured_names)} ห้อง):\n{targets_text}\n"
            f"\n🟢 โหมด: **ON-DEMAND** — บอทจะเข้าเฉพาะห้องที่มีสมาชิกอยู่ตอนแจ้งเตือน แล้วออกหลังพูดจบ",
            ephemeral=True
        )
        print(f"🔊 /setvoice saved ON-DEMAND: {interaction.guild.name} -> {target.name} ({target.id}) | total={len(configured_names)}")
    except Exception as e:
        print(f"❌ /setvoice error: {e}")
        traceback.print_exc()
        try:
            await guarded_interaction_followup_send(interaction, "interaction-followup", f"❌ ตั้งค่าห้อง Voice ไม่สำเร็จ: `{e}`", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="join", description="ดึงบอทเข้าห้องเสียงที่คุณกำลังใช้งาน")
async def join_voice(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
    if not interaction.user.voice or not interaction.user.voice.channel:
        await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ คุณต้องเชื่อมต่ออยู่ในห้องเสียงก่อนใช้คำสั่งนี้!", ephemeral=True)
        return
    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    if guild.voice_client is not None: await guild.voice_client.move_to(voice_channel)
    else: await voice_channel.connect()
    embed = discord.Embed(title="🔊 เชื่อมต่อห้องเสียงสำเร็จ", description=f"บอทเข้าสู่ห้องเสียง **{voice_channel.name}** เรียบร้อยแล้ว!", color=discord.Color.green())
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)


@bot.tree.command(name="leave", description="สั่งให้บอทออกจากห้องเสียง")
async def leave_voice(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
    guild = interaction.guild
    if guild.voice_client:
        await guild.voice_client.disconnect()
    if str(guild.id) in voice_config:
        voice_config[str(guild.id)]["enabled"] = False
        await save_voice_config()
        await guarded_interaction_followup_send(interaction, "interaction-followup", "👋 ออกจากห้องเสียงแล้ว และปิดการเชื่อมต่อถาวรของ /setvoice ชั่วคราวแล้วครับ")
    else:
        await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ บอทไม่ได้อยู่ในห้องเสียงใดๆ ในขณะนี้", ephemeral=True)


@bot.tree.command(name="disconnect", description="ตัดการเชื่อมต่อเสียงและหยุดการเล่นเสียงของบอททันที")
async def disconnect_voice(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
    try:
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            if vc.is_playing(): vc.stop()
            await vc.disconnect()
            await guarded_interaction_followup_send(interaction, "interaction-followup", "⏹️ บอทหยุดการทำงานและออกจากห้องเสียงเรียบร้อยแล้ว!")
        else:
            await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ บอทไม่ได้อยู่ในห้องเสียงครับ", ephemeral=True)
    except Exception as e:
        await guarded_interaction_followup_send(interaction, "interaction-followup", f"⚠️ เกิดข้อผิดพลาด: `{e}`")


@tasks.loop(seconds=15)
async def check_bf_notifications():
    global last_bf_notified_hour, last_bf_text_notified_hour, last_bf_voice_success_hour, bf_notify_enabled
    if not bf_notify_enabled:
        return

    try:
        now = datetime.now(TZ_THAI)
        candidate = now.replace(minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(hours=1)
        while candidate.hour % 2 != 0:
            candidate += timedelta(hours=1)

        seconds_until_bf = (candidate - now).total_seconds()
        # Run from 3 minutes before the BF start through 15 seconds after.
        if not (-15 <= seconds_until_bf <= 180):
            return

        trigger_key = candidate.strftime('%Y-%m-%d-%H')
        next_bf_time = candidate.strftime('%H:%M')
        print(f"⏰ BF WARNING WINDOW | now={now.strftime('%Y-%m-%d %H:%M:%S')} | next={candidate.isoformat()} | seconds_until={seconds_until_bf:.1f}")

        for guild in bot.guilds:
            # Text notification is one-shot, but Voice is independently retried
            # until at least one configured occupied room succeeds.
            # Text warning is independently rate-limited. A failed API call must not
            # be retried every 15 seconds because that can worsen a global 429.
            retry_at = bf_text_retry_after_ts.get(trigger_key, 0.0)
            if last_bf_text_notified_hour != trigger_key and time.monotonic() >= retry_at:
                print(f"⏰ BF WARNING TRIGGER | guild={guild.name} | now={now.strftime('%H:%M:%S')} | next={next_bf_time}")
                mentions = []
                for role_id in BF_ROLE_IDS:
                    role = guild.get_role(role_id)
                    if role:
                        mentions.append(role.mention)
                mention_target = " ".join(mentions) if mentions else ""
                text_channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                if not text_channel:
                    text_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                if text_channel:
                    await refresh_discord_notification_languages()
                    enabled=get_enabled_discord_notification_languages() or ["th"]
                    primary=enabled[0]
                    title_map={"th":"⚔️ แจ้งเตือนสงคราม Battlefield (BF)!","en":"⚔️ Battlefield (BF) Alert!","ko":"⚔️ Battlefield (BF) 알림!"}
                    body_map={"th":f"สนามรบ **BF** กำลังจะเริ่มในอีก **3 นาที** (เวลา **{next_bf_time} น.**)!\nเตรียมตัวเข้าประจำที่ได้เลยครับ!","en":f"Battlefield **BF** will start in **3 minutes** (at **{next_bf_time}**)!\nPlease get ready.","ko":f"Battlefield **BF**가 **3분 후** 시작됩니다 (시간 **{next_bf_time}**)!\n준비해 주세요."}
                    embed=discord.Embed(title=title_map[primary],color=discord.Color.red())
                    for lang in enabled: embed.add_field(name={"th":"🇹🇭 ไทย","en":"🇺🇸 English","ko":"🇰🇷 한국어"}[lang],value=body_map[lang],inline=False)
                    # Reserve one attempt first. On 429, allow at most one later retry.
                    bf_text_retry_after_ts[trigger_key] = time.monotonic() + 30.0
                    try:
                        send_result = await guarded_channel_send(text_channel, context=f"bf:{guild.name}", content=mention_target or None, embed=embed)
                        if send_result is not None:
                            last_bf_text_notified_hour = trigger_key
                            bf_text_retry_after_ts.pop(trigger_key, None)
                            print(f"✅ BF text notification sent | guild={guild.name}")
                        else:
                            bf_text_retry_after_ts[trigger_key] = max(
                                bf_text_retry_after_ts.get(trigger_key, 0.0),
                                time.monotonic() + 60.0,
                            )
                            print(f"⏭️ BF text skipped by Global REST Guard | guild={guild.name}", flush=True)
                    except discord.HTTPException as exc:
                        if getattr(exc, "status", None) == 429:
                            wait_for = await _get_retry_after_seconds(exc, default=30.0)
                            bf_text_retry_after_ts[trigger_key] = time.monotonic() + min(wait_for, 120.0)
                            print(f"⚠️ BF text rate-limited | guild={guild.name} | retry in ~{wait_for:.1f}s")
                        else:
                            bf_text_retry_after_ts[trigger_key] = time.monotonic() + 60.0
                            print(f"❌ ส่งข้อความเตือน BF ไม่สำเร็จ: {guild.name}: {exc}")
                    except Exception as exc:
                        bf_text_retry_after_ts[trigger_key] = time.monotonic() + 60.0
                        print(f"❌ ส่งข้อความเตือน BF ไม่สำเร็จ: {guild.name}: {exc}")

            spoken_text_th = "Battlefield กำลังจะเริ่มในอีก 3 นาทีค่ะ"
            spoken_text_en = "Battlefield will start in 3 minutes."
            spoken_text_ko = "배틀필드가 3분 후에 시작됩니다."

            configured = get_configured_voice_channels(guild)
            occupied = [ch for ch in configured if any(not m.bot for m in ch.members)]
            print(f"⏰ BF VOICE TARGETS | guild={guild.name} | configured={len(configured)} | occupied={len(occupied)} | voice_success={last_bf_voice_success_hour == trigger_key}")
            if not occupied:
                print(f"⏭️ BF VOICE WAIT | guild={guild.name} | no occupied /setvoice rooms yet")
                continue
            if last_bf_voice_success_hour == trigger_key:
                continue

            results = []
            for room in occupied:
                try:
                    print(f"📢 BF VOICE START | guild={guild.name} | channel={room.name} | humans={len([m for m in room.members if not m.bot])}")
                    ok = False
                    last_exc = None
                    for attempt in range(1, 4):
                        # Refresh occupancy immediately before each attempt.
                        if not any(not m.bot for m in room.members):
                            print(f"⏭️ BF VOICE SKIP NOW EMPTY | guild={guild.name} | channel={room.name}")
                            break
                        try:
                            ok = await asyncio.wait_for(
                                speak_in_guild(
                                    guild,
                                    text_th=spoken_text_th,
                                    text_en=spoken_text_en,
                                    text_ko=spoken_text_ko,
                                    target_channel=room
                                ),
                                timeout=120
                            )
                            if ok:
                                break
                        except Exception as exc:
                            last_exc = exc
                            print(f"⚠️ BF VOICE RETRY {attempt}/3 | guild={guild.name} | channel={room.name} | {exc}")
                            await asyncio.sleep(min(2.0 * attempt, 5.0))
                    results.append(bool(ok))
                    if last_exc and not ok:
                        print(f"❌ BF VOICE ERROR FINAL | guild={guild.name}/{room.name} | {last_exc}")
                    print(f"📣 BF VOICE RESULT | guild={guild.name} | channel={room.name} | success={ok}")
                except Exception as exc:
                    results.append(False)
                    print(f"❌ BF VOICE ERROR | guild={guild.name}/{room.name} | {exc}")

            success_count = sum(1 for x in results if x)
            if success_count:
                last_bf_voice_success_hour = trigger_key
                # Only mark the whole BF notification as done once Voice has actually succeeded.
                last_bf_notified_hour = trigger_key
            print(f"✅ BF VOICE COMPLETE | guild={guild.name} | success={success_count}/{len(results)}")

        # If text has been sent for this BF but Voice did not succeed yet, keep the
        # text one-shot state while allowing voice retries on later loop iterations.
        if last_bf_notified_hour != trigger_key:
            # Text-only send was intentionally not globally latched here; each guild
            # independently gets one text send. Voice remains independently retryable.
            pass
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_bf_notifications': {e}")
        traceback.print_exc()


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
                        await refresh_discord_notification_languages()
                        enabled=get_enabled_discord_notification_languages() or ["th"]
                        primary=enabled[0]
                        title_map={"th":"⚔️ แจ้งเตือน Library Boss!","en":"⚔️ Library Boss Alert!","ko":"⚔️ Library Boss 알림!"}
                        body_map={"th":f"บอส **Library Boss** ถึงเวลาเตรียมตัวแล้ว! (เวลา **{time_str}**)!\nเตรียมตัวเข้าประจำที่ได้เลยครับ!","en":f"**Library Boss** is ready! (Time **{time_str}**)!\nPlease get ready.","ko":f"**Library Boss** 등장 시간입니다! (시간 **{time_str}**)!\n준비해 주세요."}
                        embed=discord.Embed(title=title_map[primary],color=discord.Color.purple())
                        for lang in enabled: embed.add_field(name={"th":"🇹🇭 ไทย","en":"🇺🇸 English","ko":"🇰🇷 한국어"}[lang],value=body_map[lang],inline=False)
                        try:
                            send_content = mention_target if mention_target.strip() else None
                            await guarded_channel_send(channel, context="library-boss", content=send_content, embed=embed)
                        except Exception as e: print(f"❌ ส่งข้อความเตือน Library Boss ไม่สำเร็จ: {e}")

                    spoken_text_th = "Library Boss ถึงเวลาเตรียมตัวแล้วค่ะ"
                    spoken_text_en = "It's time to prepare for Library Boss."
                    spoken_text_ko = "도서관 보스 준비 시간입니다."
                    asyncio.create_task(speak_in_guild(guild, text_th=spoken_text_th, text_en=spoken_text_en, text_ko=spoken_text_ko))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_library_boss_notifications': {e}")


def get_enabled_discord_notification_languages():
    """Return enabled Discord text-notification languages in TH/EN/KO order."""
    langs = []
    if discord_notify_th_enabled:
        langs.append("th")
    if discord_notify_en_enabled:
        langs.append("en")
    if discord_notify_ko_enabled:
        langs.append("ko")
    return langs


def build_boss_discord_notification(boss_name: str, stage: str, spawn_time: datetime, notice_minutes: int):
    """Build one Discord embed containing only the enabled TH/EN/KO text."""
    enabled = get_enabled_discord_notification_languages()
    time_str = spawn_time.strftime('%H:%M:%S')
    title_by_lang = {
        "th": f"⚠️ {boss_name} ใกล้เกิด!" if stage == "advance" else f"⚔️ {boss_name} เกิดแล้ว!",
        "en": f"⚠️ {boss_name} Spawning Soon!" if stage == "advance" else f"⚔️ {boss_name} Spawned!",
        "ko": f"⚠️ {boss_name} 젠 임박!" if stage == "advance" else f"⚔️ {boss_name} 젠 완료!",
    }
    body_by_lang = {
        "th": (f"บอส **{boss_name}** จะเกิดในอีก **{notice_minutes} นาที**!\nเวลาเกิด: **{time_str} น.**" if stage == "advance" else f"บอส **{boss_name}** เกิดแล้วในขณะนี้!\nเวลาเกิด: **{time_str} น.**"),
        "en": (f"Boss **{boss_name}** will spawn in **{notice_minutes} minutes**.\nSpawn time: **{time_str}**" if stage == "advance" else f"Boss **{boss_name}** has spawned!\nSpawn time: **{time_str}**"),
        "ko": (f"보스 **{boss_name}**가 **{notice_minutes}분 후에** 나타납니다.\n생성 시간: **{time_str}**" if stage == "advance" else f"보스 **{boss_name}**가 지금 나타났습니다!\n생성 시간: **{time_str}**"),
    }
    if not enabled:
        return discord.Embed(
            title=f"⚔️ Boss Timer • {boss_name}",
            description=f"{time_str} • {('ADVANCE' if stage == 'advance' else 'SPAWN')}",
            color=discord.Color.gold() if stage == "advance" else discord.Color.green(),
        )
    primary_lang = enabled[0]
    embed = discord.Embed(title=title_by_lang[primary_lang], color=discord.Color.gold() if stage == "advance" else discord.Color.green())
    labels = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}
    for lang in enabled:
        embed.add_field(name=labels[lang], value=body_by_lang[lang], inline=False)
    return embed


def get_notification_mentions(guild: discord.Guild) -> str:
    if not guild:
        return ""
    roles = []
    seen = set()
    for role_id in TARGET_ROLE_IDS:
        try:
            role = guild.get_role(int(role_id))
        except (TypeError, ValueError):
            role = None
        if role and role.id not in seen:
            roles.append(role)
            seen.add(role.id)
    for role_name in TARGET_ROLE_NAMES:
        role = discord.utils.find(lambda r: r.name.casefold() == role_name.casefold(), guild.roles)
        if role and role.id not in seen:
            roles.append(role)
            seen.add(role.id)
    return " ".join(role.mention for role in roles)


boss_notification_diag_last_ts = {}
_boss_voice_stage_inflight = set()

def _queue_boss_rest_notification(
    boss_name: str,
    stage: str,
    channel,
    *,
    content=None,
    embed=None,
) -> bool:
    """Queue one non-essential boss text notification without probing Discord."""
    try:
        channel_id = int(channel.id)
    except Exception:
        return False
    key = (str(boss_name), str(stage), channel_id)
    with pending_boss_rest_lock:
        if key in pending_boss_rest_keys:
            return False
        if len(pending_boss_rest_notifications) >= PENDING_BOSS_REST_MAX:
            print(f"⚠️ Boss REST queue full; dropping newest non-essential text notification | key={key}", flush=True)
            return False
        pending_boss_rest_keys.add(key)
        pending_boss_rest_notifications.append({
            "key": key,
            "boss_name": str(boss_name),
            "stage": str(stage),
            "channel_id": channel_id,
            "content": content,
            "embed": embed,
            "queued_at": time.time(),
        })
    print(f"⏸️ Boss text notification queued | boss={boss_name} | stage={stage} | channel={channel_id}", flush=True)
    return True


async def _flush_one_boss_rest_notification() -> bool:
    """Send at most one queued boss text notification per worker pass."""
    with pending_boss_rest_lock:
        if not pending_boss_rest_notifications:
            return False
        item = pending_boss_rest_notifications[0]

    channel = bot.get_channel(int(item["channel_id"]))
    if channel is None:
        with pending_boss_rest_lock:
            if pending_boss_rest_notifications and pending_boss_rest_notifications[0] is item:
                pending_boss_rest_notifications.popleft()
                pending_boss_rest_keys.discard(item["key"])
        print(f"⚠️ Boss REST queue target channel unavailable | boss={item['boss_name']} | stage={item['stage']}", flush=True)
        return False

    try:
        result = await guarded_channel_send(
            channel,
            context=f"boss-notify:{item['boss_name']}:queue:{item['stage']}",
            content=item.get("content"),
            embed=item.get("embed"),
            background=True,
        )
    except discord.HTTPException as exc:
        # Keep the item queued. The central guard records the 429 and quarantines background REST.
        print(
            f"⏸️ Boss REST queue held after Discord HTTP error | boss={item['boss_name']} | "
            f"stage={item['stage']} | status={getattr(exc, 'status', None)}",
            flush=True,
        )
        return False
    except Exception as exc:
        print(f"⚠️ Boss REST queue send failed safely | boss={item['boss_name']} | stage={item['stage']} | {exc!r}", flush=True)
        return False

    if result is None:
        return False

    with pending_boss_rest_lock:
        if pending_boss_rest_notifications and pending_boss_rest_notifications[0] is item:
            pending_boss_rest_notifications.popleft()
            pending_boss_rest_keys.discard(item["key"])

    try:
        if item["stage"] == "advance":
            await save_boss_notification_flags(item["boss_name"], notified_advance=True)
        elif item["stage"] == "spawn":
            await save_boss_notification_flags(item["boss_name"], notified_spawn=True)
    except Exception as exc:
        print(f"⚠️ Boss notification flag update after queued send failed: {item['boss_name']}: {exc}", flush=True)

    print(f"🟢 Boss REST queue sent: {item['boss_name']} | stage={item['stage']}", flush=True)
    return True


async def flush_pending_boss_rest_notifications_once():
    """V58 queue worker: one background REST attempt max per scheduler pass."""
    await _flush_one_boss_rest_notification()


async def save_boss_notification_flags(boss_name: str, **flags):
    clean = {k: bool(v) for k, v in flags.items()}
    if not clean:
        return
    with schedule_lock:
        if boss_name in boss_schedule:
            boss_schedule[boss_name].update(clean)
    mapping = {
        "notified_advance": "notifiedNotice",
        "notified_spawn": "notifiedSpawn",
        "voice_notice_sent": "voiceNoticeSent",
        "voice_spawn_sent": "voiceSpawnSent",
    }
    try:
        await asyncio.to_thread(
            db.reference(f"boss_schedule/{boss_name}").update,
            {mapping.get(k, k): v for k, v in clean.items()}
        )
    except Exception as e:
        print(f"⚠️ Firebase notification flag update failed: {boss_name}: {e}")


@tasks.loop(seconds=15)
async def _run_boss_voice_stage(boss_name: str, stage: str, notice_minutes: int, target_guilds):
    """Run one Boss Voice announcement without blocking the Boss scheduler loop.

    The previous V75 scheduler awaited Voice/TTS directly. A slow advance announcement
    could therefore delay the scheduler past the real spawn time, after which the stale
    guard could mark voice_spawn_sent=True without ever speaking the spawn announcement.
    This helper keeps the same occupancy and Voice/TTS rules but runs independently.
    """
    key = (str(boss_name), str(stage))
    try:
        spoken_name = get_boss_pronunciation(boss_name)
        all_ok = True
        spoken_rooms = 0
        for guild in list(target_guilds):
            configured_rooms = get_configured_voice_channels(guild)
            rooms = [r for r in configured_rooms if any(not m.bot for m in r.members)]
            print(
                f"🔎 Boss VOICE {stage.upper()} targets | guild={guild.name} | "
                f"configured={len(configured_rooms)} | occupied={len(rooms)}",
                flush=True,
            )
            if not rooms:
                print(
                    f"⏭️ Boss VOICE WAIT | guild={guild.name} | configured={len(configured_rooms)} | "
                    f"occupied=0 | stage={stage}",
                    flush=True,
                )
                continue

            for room in rooms:
                try:
                    if stage == "advance":
                        text_th = f"บอส {spoken_name} จะเกิดในอีก {notice_minutes} นาทีค่ะ"
                        text_en = f"Boss {boss_name} will spawn in {notice_minutes} minutes."
                        text_ko = f"보스 {boss_name}가 {notice_minutes}분 후에 나타납니다."
                    else:
                        text_th = f"บอส {spoken_name} เกิดแล้วค่ะ"
                        text_en = f"Boss {boss_name} has spawned."
                        text_ko = f"보스 {boss_name}가 나타났습니다."

                    result = await asyncio.wait_for(
                        speak_in_guild(
                            guild,
                            text_th=text_th,
                            text_en=text_en,
                            text_ko=text_ko,
                            target_channel=room,
                        ),
                        timeout=180,
                    )
                    if result:
                        spoken_rooms += 1
                    else:
                        all_ok = False
                except Exception as exc:
                    all_ok = False
                    print(
                        f"⚠️ {stage.title()} TTS failed ({boss_name}/{guild.name}/{room.name}): {exc}",
                        flush=True,
                    )

        print(
            f"🔊 Boss VOICE {stage.upper()} RESULT | boss={boss_name} | "
            f"spoken_rooms={spoken_rooms} | all_ok={all_ok}",
            flush=True,
        )
        if spoken_rooms > 0 and all_ok:
            if stage == "advance":
                await save_boss_notification_flags(boss_name, voice_notice_sent=True)
                print(f"🔊 Advance TTS sent: {boss_name} -> {spoken_rooms} occupied room(s)", flush=True)
            else:
                await save_boss_notification_flags(boss_name, voice_spawn_sent=True)
                print(f"🔊 Spawn TTS sent: {boss_name} -> {spoken_rooms} occupied room(s)", flush=True)
    finally:
        _boss_voice_stage_inflight.discard(key)


@tasks.loop(seconds=15)
async def check_boss_notifications():
    try:
        now = datetime.now(TZ_THAI)
        with schedule_lock:
            schedule_copy = {boss: dict(data) for boss, data in boss_schedule.items() if isinstance(data, dict)}

        for boss_name, data in schedule_copy.items():
            spawn_time = parse_to_thai_datetime(data.get("spawn_time") or data.get("spawnTimeMs"))
            if not spawn_time:
                print(f"⚠️ Boss notification skip: {boss_name} has invalid spawn time")
                continue

            time_left = (spawn_time - now).total_seconds()

            # Library Boss rows exist in boss_schedule for Dashboard/Auto Attendance,
            # but their legacy 08:50/20:50 notification task remains the sole text/TTS
            # notifier. This prevents duplicate generic Boss alerts.
            if parse_bool(data.get("suppressBossNotifications"), False):
                continue

            try:
                notice_minutes = max(1, int(data.get("noticeMinutes") or get_boss_advance_notice_seconds(boss_name) / 60))
            except (TypeError, ValueError):
                notice_minutes = max(1, int(get_boss_advance_notice_seconds(boss_name) / 60))
            notice_seconds = notice_minutes * 60
            notified_advance = parse_bool(data.get("notified_advance", data.get("notifiedNotice", False)))
            notified_spawn = parse_bool(data.get("notified_spawn", data.get("notifiedSpawn", False)))
            voice_advance = parse_bool(data.get("voice_notice_sent", data.get("voiceNoticeSent", False)))
            voice_spawn = parse_bool(data.get("voice_spawn_sent", data.get("voiceSpawnSent", False)))

            # Never replay an old boss after a Render restart/deploy.
            # A schedule more than 120 seconds past spawn is considered stale.
            # Mark every notification flag complete before continuing.
            if time_left < -120:
                if not (notified_advance and notified_spawn and voice_advance and voice_spawn):
                    await save_boss_notification_flags(
                        boss_name,
                        notified_advance=True,
                        notified_spawn=True,
                        voice_notice_sent=True,
                        voice_spawn_sent=True,
                    )
                    print(f"⏭️ Stale boss suppressed: {boss_name} | left={time_left:.1f}s")
                continue

            # Do not spam Render logs every 5 seconds while nothing is changing.
            # Log only when a real notification action is due.
            # Report only when a notification state can actually change. During a
            # global REST cooldown, a pending text notification is expected to remain
            # false; do not flood logs every scheduler tick while Voice has already
            # succeeded.
            rest_blocked = _discord_rest_rate_limit_remaining() > 0
            advance_text_due = (0 < time_left <= notice_seconds and not notified_advance)
            advance_voice_due = (0 < time_left <= notice_seconds and not voice_advance)
            spawn_text_due = (time_left <= 0 and not notified_spawn)
            spawn_voice_due = (-120 <= time_left <= 0 and not voice_spawn)
            notification_action_due = advance_text_due or advance_voice_due or spawn_text_due or spawn_voice_due

            if notification_action_due:
                # Throttle informational "action due" lines to once per boss/stage per
                # 60 seconds. This does not alter the actual send/retry behavior.
                stage_key = (
                    "advance" if advance_voice_due or advance_text_due
                    else "spawn" if spawn_voice_due or spawn_text_due
                    else "none"
                )
                diag_key = (boss_name, stage_key)
                now_mono = time.monotonic()
                last_diag = boss_notification_diag_last_ts.get(diag_key, 0.0)
                if now_mono - last_diag >= 60.0:
                    boss_notification_diag_last_ts[diag_key] = now_mono
                    print(
                        f"🔎 Boss notification action due: {boss_name} | spawn={spawn_time.isoformat()} | "
                        f"left={time_left:.1f}s | notice={notice_minutes}m | advance={notified_advance} | "
                        f"spawn_sent={notified_spawn} | voice_advance={voice_advance} | voice_spawn={voice_spawn}"
                    )

            # Text notification targets are now persisted in Firebase under
            # notification_channels and are read immediately before a Boss event.
            # This preserves the old per-boss/fallback behavior only when no explicit
            # notification channels are configured yet.
            configured_notification_ids = await get_notification_channel_ids_from_database()
            channels_to_notify = []
            seen_channel_ids = set()
            for target_channel_id in configured_notification_ids:
                try:
                    target_channel_id = int(target_channel_id)
                except (TypeError, ValueError):
                    continue
                if target_channel_id in seen_channel_ids:
                    continue
                ch = bot.get_channel(target_channel_id)
                if ch is None:
                    try:
                        ch = await guarded_fetch_channel(target_channel_id, context=f"boss-notify:fetch-configured-channel:{boss_name}")
                    except Exception:
                        ch = None
                if isinstance(ch, discord.TextChannel):
                    if ch.id not in seen_channel_ids:
                        channels_to_notify.append(ch)
                        seen_channel_ids.add(ch.id)

            if not channels_to_notify:
                # Backward-compatible fallback for deployments that have not run
                # /set-notification yet. Do not remove the existing boss channel/fallback.
                channel = None
                channel_id = data.get("channel_id") or data.get("channelId")
                if channel_id:
                    try:
                        channel = bot.get_channel(int(channel_id))
                        if channel is None:
                            channel = await guarded_fetch_channel(int(channel_id), context=f"boss-notify:fetch-channel:{boss_name}")
                    except Exception:
                        channel = None
                if channel:
                    channels_to_notify.append(channel)

                if not channels_to_notify:
                    for guild in bot.guilds:
                        fb_channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                        if not fb_channel:
                            fb_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                        if fb_channel and fb_channel.id not in seen_channel_ids:
                            channels_to_notify.append(fb_channel)
                            seen_channel_ids.add(fb_channel.id)

            # Voice notifications must not depend on text-channel resolution or REST availability.
            # Always evaluate configured /setvoice targets directly from the READY guild cache.
            target_guilds = set(bot.guilds)

            # Advance: text and voice are independent one-shot states.
            advance_stage_queued = False
            spawn_stage_queued = False
            with pending_boss_rest_lock:
                advance_stage_queued = any(item.get("boss_name") == boss_name and item.get("stage") == "advance" for item in pending_boss_rest_notifications)
                spawn_stage_queued = any(item.get("boss_name") == boss_name and item.get("stage") == "spawn" for item in pending_boss_rest_notifications)

            if 0 < time_left <= notice_seconds and not notified_advance and not advance_stage_queued:
                await refresh_discord_notification_languages()
                embed = build_boss_discord_notification(boss_name, "advance", spawn_time, notice_minutes)
                for ch in channels_to_notify:
                    try:
                        mentions = get_notification_mentions(getattr(ch, "guild", None))
                        _queue_boss_rest_notification(
                            boss_name,
                            "advance",
                            ch,
                            content=mentions or None,
                            embed=embed,
                        )
                    except Exception as e:
                        print(f"⚠️ Queue advance notification failed ({boss_name}): {e}", flush=True)
                # The queue owns the text send and flag update. Do not call Discord REST here.

            if 0 < time_left <= notice_seconds and not voice_advance:
                voice_key = (str(boss_name), "advance")
                if voice_key not in _boss_voice_stage_inflight:
                    _boss_voice_stage_inflight.add(voice_key)
                    print(
                        f"📢 Schedule Boss VOICE ADVANCE task | boss={boss_name} | "
                        f"left={time_left:.1f}s | notice={notice_minutes}m",
                        flush=True,
                    )
                    asyncio.create_task(
                        _run_boss_voice_stage(boss_name, "advance", notice_minutes, target_guilds),
                        name=f"boss-voice-advance-{boss_name}",
                    )

            # Spawn: only notify at the actual crossing. Old schedules >60s late
            # are marked complete instead of replaying after every deploy/reload.
            if time_left <= 0 and not notified_spawn and not spawn_stage_queued:
                await refresh_discord_notification_languages()
                embed = build_boss_discord_notification(boss_name, "spawn", spawn_time, notice_minutes)
                for ch in channels_to_notify:
                    try:
                        mentions = get_notification_mentions(getattr(ch, "guild", None))
                        _queue_boss_rest_notification(
                            boss_name,
                            "spawn",
                            ch,
                            content=mentions or None,
                            embed=embed,
                        )
                    except Exception as e:
                        print(f"⚠️ Queue spawn notification failed ({boss_name}): {e}", flush=True)
                # The queue owns the text send and flag update. Do not call Discord REST here.

            if -120 <= time_left <= 0 and not voice_spawn:
                voice_key = (str(boss_name), "spawn")
                if voice_key not in _boss_voice_stage_inflight:
                    _boss_voice_stage_inflight.add(voice_key)
                    print(
                        f"📢 Schedule Boss VOICE SPAWN task | boss={boss_name} | "
                        f"left={time_left:.1f}s | guilds={len(target_guilds)}",
                        flush=True,
                    )
                    asyncio.create_task(
                        _run_boss_voice_stage(boss_name, "spawn", notice_minutes, target_guilds),
                        name=f"boss-voice-spawn-{boss_name}",
                    )
            elif time_left < -120 and not voice_spawn:
                await save_boss_notification_flags(boss_name, voice_spawn_sent=True)
                print(f"⏭️ Legacy expired boss marked complete: {boss_name} (left={time_left:.1f}s)")

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
                try: channel = await guarded_fetch_channel(channel_id, context="live:fetch-channel")
                except Exception: return
            try: cached_live_message = await guarded_fetch_message(channel, message_id, context="live:fetch-message")
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
        try: await guarded_message_edit(cached_live_message, context="live:edit", embed=embed)
        except Exception as e: print(f"❌ อัปเดต Live Embed ไม่สำเร็จ: {e}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'update_live_embed': {e}")


@tasks.loop(seconds=60)
async def check_auto_disconnect():
    try:
        now = datetime.now(TZ_THAI)
        for guild in bot.guilds:
            vc = guild.voice_client
            if vc and vc.is_connected() and vc.channel and not vc.is_playing():
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
                            except Exception as e: print(f"❌ ตัดสายไม่สำเร็จ: {e}")
                            del voice_empty_start[guild.id]
                else:
                    if guild.id in voice_empty_start:
                        del voice_empty_start[guild.id]
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_auto_disconnect': {e}")


@bot.tree.command(name="notice", description="ประกาศข้อความเสียงไปยังทุกห้องสนทนาที่มีคนอยู่")
@app_commands.describe(message="ข้อความที่ต้องการให้บอทประกาศ")
@has_allowed_role()
async def notice_command(interaction: discord.Interaction, message: str):
    global NOTICE_LAST_RUN_TS
    # Serialize /notice calls so one operator cannot create a REST/Voice burst.
    if NOTICE_COMMAND_LOCK.locked():
        try:
            await _safe_interaction_send_message(interaction, "⏳ /notice กำลังทำงานอยู่ กรุณารอสักครู่", ephemeral=True)
        except discord.HTTPException as exc:
            print(f"⚠️ /notice busy response failed: {exc}", flush=True)
        return

    if time.monotonic() - NOTICE_LAST_RUN_TS < 2.0:
        try:
            await _safe_interaction_send_message(interaction, "⏳ /notice เพิ่งถูกเรียกไป กรุณารอสักครู่", ephemeral=True)
        except discord.HTTPException as exc:
            print(f"⚠️ /notice cooldown response failed: {exc}", flush=True)
        return

    async with NOTICE_COMMAND_LOCK:
        NOTICE_LAST_RUN_TS = time.monotonic()
        ack_ok = await _safe_interaction_ack(interaction, ephemeral=True)
        if not ack_ok:
            # Do NOT discard the actual work merely because the initial interaction callback
            # hit a transient/global 429.  Run the notice anyway; if Discord REST is available
            # the Voice path can still complete.  The command UI may still show a timeout when
            # Discord blocks the acknowledgement endpoint itself, which cannot be fixed client-side.
            print("⚠️ /notice ACK unavailable due to Discord 429; continuing Voice notice attempt", flush=True)

        if not message.strip():
            if ack_ok:
                try:
                    await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ กรุณาระบุข้อความที่ต้องการประกาศครับ", ephemeral=True)
                except discord.HTTPException as exc:
                    print(f"⚠️ /notice empty-message response failed: {exc}", flush=True)
            return

        # Global notice: every occupied Voice channel in the current guild.
        occupied = []
        for vc in interaction.guild.voice_channels:
            humans = [m for m in vc.members if not m.bot]
            if humans:
                occupied.append(vc)

        if not occupied:
            if ack_ok:
                try:
                    await guarded_interaction_followup_send(interaction, "interaction-followup", "⚠️ ขณะนี้ไม่มีสมาชิกอยู่ในห้อง Voice ใดเลย", ephemeral=True)
                except discord.HTTPException as exc:
                    print(f"⚠️ /notice empty-room response failed: {exc}", flush=True)
            else:
                print(f"⚠️ /notice no occupied Voice rooms | guild={interaction.guild.name}", flush=True)
            return

        names = ", ".join(f"**{vc.name}**" for vc in occupied)
        if ack_ok:
            try:
                await guarded_interaction_edit_original(interaction, "interaction-edit-original", 
                    content=f"📢 เริ่มประกาศใน **{len(occupied)} ห้อง**: {names}\nบอทจะเข้า → พูด → ออกทีละห้อง"
                )
            except discord.HTTPException as exc:
                print(f"⚠️ /notice progress update failed: {exc}", flush=True)

        results = []
        for vc in occupied:
            try:
                ok = await asyncio.wait_for(
                    speak_in_guild(
                        interaction.guild,
                        text_th=message,
                        text_en=message,
                        text_ko=message,
                        target_channel=vc,
                    ),
                    timeout=180,
                )
                results.append((vc.name, bool(ok)))
            except Exception as exc:
                print(f"❌ /notice TTS failed in {vc.name}: {exc}", flush=True)
                results.append((vc.name, False))

        ok_count = sum(1 for _, ok in results if ok)
        print(f"📢 /notice GLOBAL complete: {ok_count}/{len(results)} rooms", flush=True)

        if ack_ok:
            failed = [name for name, ok in results if not ok]
            try:
                if failed:
                    await guarded_interaction_edit_original(interaction, "interaction-edit-original", 
                        content=(
                            f"⚠️ ประกาศเสียงสำเร็จ {ok_count}/{len(results)} ห้อง\n"
                            f"❌ ห้องที่ไม่สำเร็จ: {', '.join(failed)}"
                        )
                    )
                else:
                    await guarded_interaction_edit_original(interaction, "interaction-edit-original", 
                        content=f"✅ /notice ประกาศสำเร็จ {ok_count}/{len(results)} ห้อง"
                    )
            except discord.HTTPException as exc:
                print(f"⚠️ /notice final response update failed: {exc}", flush=True)
        else:
            # Best-effort text audit only.  This does not fix a Discord interaction callback 429,
            # but gives Render logs a deterministic completion result.
            print(
                f"📣 /notice completed without interaction ACK | success={ok_count}/{len(results)} | "
                f"guild={interaction.guild.name}",
                flush=True,
            )

def generate_boss_time_summary():
    """Build the /time embed and the multilingual TTS summary from current boss_schedule."""
    now = datetime.now(TZ_THAI)
    with schedule_lock:
        schedule_copy = boss_schedule.copy()

    if not schedule_copy:
        return (
            None,
            "ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบครับ",
            None,
            None,
        )

    sorted_bosses = sorted(
        schedule_copy.items(),
        key=lambda x: parse_to_thai_datetime(x[1].get("spawn_time")) or now,
    )
    embed = discord.Embed(
        title="⌛ สรุปเวลาที่เหลือของบอสทุกตัว (เรียงจากน้อยไปมาก)",
        description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`",
        color=discord.Color.purple(),
    )

    tts_lines_th = ["สรุปเวลาบอสเรียงจากน้อยไปมากค่ะ"]
    tts_lines_en = ["Boss time summary from earliest to latest."]
    tts_lines_ko = ["보스 스폰 시간 요약입니다."]

    for boss, data in sorted_bosses[:20]:
        spawn_time = parse_to_thai_datetime(data.get("spawn_time"))
        if not spawn_time:
            continue

        time_left_sec = (spawn_time - now).total_seconds()
        spoken_name = get_boss_pronunciation(boss)
        rec_by = data.get("recorded_by") or data.get("recordedBy") or "-"

        if time_left_sec <= 0:
            time_left_str = "เกิดแล้ว!"
            tts_lines_th.append(f"บอส {spoken_name} เกิดแล้วค่ะ")
            tts_lines_en.append(f"Boss {boss} has spawned.")
            tts_lines_ko.append(f"보스 {boss}가 나타났습니다.")
        else:
            total_seconds = max(0, int(time_left_sec))
            m, s = divmod(total_seconds, 60)
            h, m = divmod(m, 60)

            parts = []
            if h > 0:
                parts.append(f"{h} ชม.")
            if m > 0 or h > 0:
                parts.append(f"{m} นาที")
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

        embed.add_field(
            name=f"👾 {boss}",
            value=(
                f"เวลาเกิด: `{spawn_time.strftime('%H:%M:%S น.')}` | "
                f"นับถอยหลัง: **{time_left_str}**\n"
                f"*(บันทึกโดย: {rec_by})*"
            ),
            inline=False,
        )

    if len(sorted_bosses) > 20:
        embed.add_field(
            name="📌 หมายเหตุ",
            value=f"*ยังมีบอสอีก {len(sorted_bosses) - 20} ตัว สามารถดูเพิ่มเติมได้บน Dashboard*",
            inline=False,
        )

    return (
        embed,
        " ".join(tts_lines_th),
        " ".join(tts_lines_en),
        " ".join(tts_lines_ko),
    )

async def _time_channel_fallback(interaction: discord.Interaction, *, embed=None, content=None):
    """Fallback for /time when Discord rejects the interaction callback.

    This is a normal channel message, not an interaction response, so it is only
    used after the one allowed initial interaction callback has already failed.
    It is bounded to a few seconds and goes through the existing REST guard so
    we never create a retry storm during a Discord restriction.
    """
    channel = getattr(interaction, "channel", None)
    if channel is None:
        print("⚠️ /time channel fallback unavailable: interaction.channel is None", flush=True)
        return False

    try:
        message = await asyncio.wait_for(
            guarded_discord_call(
                lambda: channel.send(
                    content=content,
                    embed=embed,
                ),
                context="time-channel-fallback",
                # Do not sleep for a Discord global/IP block. The command must remain
                # non-blocking; the audit/result queue handles recovery after cooldown.
                wait_for_cooldown=False,
            ),
            timeout=4.0,
        )
        if message is not None:
            print("✅ /time channel fallback sent successfully", flush=True)
            return True
        _queue_channel_result(
            interaction.channel_id,
            content=content,
            embed=embed,
            context="time-channel-fallback-queued",
        )
        print("⏸️ /time result queued for automatic delivery after Discord REST cooldown", flush=True)
        return False
    except asyncio.TimeoutError:
        _queue_channel_result(
            interaction.channel_id,
            content=content,
            embed=embed,
            context="time-channel-fallback-queued",
        )
        print("⚠️ /time channel fallback timed out safely | result queued", flush=True)
        return False
    except discord.HTTPException as exc:
        _queue_channel_result(
            interaction.channel_id,
            content=content,
            embed=embed,
            context="time-channel-fallback-queued",
        )
        print(
            f"⚠️ /time channel fallback failed | status={getattr(exc, 'status', None)} | {exc} | result queued",
            flush=True,
        )
        return False
    except Exception as exc:
        _queue_channel_result(
            interaction.channel_id,
            content=content,
            embed=embed,
            context="time-channel-fallback-queued",
        )
        print(f"⚠️ /time channel fallback failed unexpectedly: {exc!r} | result queued", flush=True)
        return False


@bot.tree.command(name="time", description="คำนวณเวลาที่เหลือของบอสทุกตัว เรียงจากน้อยไปมาก และส่งเสียงอ่าน TTS ในห้องเสียง")
async def boss_time_slash(interaction: discord.Interaction):
    # Discord requires the initial interaction callback to be acknowledged promptly.
    # Defer FIRST, then build the summary, and edit the original response afterward.
    # This preserves the existing /time result and Voice/TTS behavior while avoiding
    # a preventable timeout caused by doing work before the initial ACK.
    ack_ok = False
    fallback_sent = False
    response_content = None
    response_embed = None
    tts_text_th = tts_text_en = tts_text_ko = ""
    try:
        ack_ok = await _safe_interaction_ack(interaction, ephemeral=True)

        embed, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
        response_content = None if embed is not None else tts_text_th
        response_embed = embed if embed is not None else None

        if ack_ok:
            edited = await guarded_interaction_edit_original(
                interaction,
                "time-interaction-result",
                content=response_content,
                embed=response_embed,
            )
            if edited is None:
                print("⚠️ /time initial ACK succeeded but original response edit was unavailable", flush=True)
        else:
            print(
                "⚠️ /time ACK unavailable; using one bounded normal-channel fallback safely",
                flush=True,
            )
            fallback_sent = await _time_channel_fallback(
                interaction,
                content=response_content,
                embed=response_embed,
            )

        if embed is not None:
            print("✅ /time summary prepared and response delivery attempted", flush=True)
        else:
            print("ℹ️ /time summary generated without embed", flush=True)

        # Keep the original Voice/TTS behavior unchanged.
        if interaction.guild is not None:
            asyncio.create_task(
                speak_in_guild(
                    interaction.guild,
                    text_th=tts_text_th,
                    text_en=tts_text_en,
                    text_ko=tts_text_ko,
                )
            )

        # Audit remains independent from the interaction callback.  The existing
        # guard/queue policy is preserved for REST restrictions.
        await send_audit_log(
            interaction.guild,
            interaction.user,
            "เช็กเวลาบอสพร้อม TTS (/time)",
            (
                "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย"
                if ack_ok
                else "คำนวณสรุปเวลาบอสสำเร็จ แต่ Discord ปฏิเสธ interaction callback; "
                     f"ส่งผลลัพธ์ผ่านข้อความในห้อง = {'สำเร็จ' if fallback_sent else 'ไม่สำเร็จ'}"
            ),
            discord.Color.purple(),
        )
        if not ack_ok and not fallback_sent:
            print("⚠️ /time result could not be posted to Discord; audit attempt completed safely", flush=True)
    except Exception as exc:
        print(f"❌ /time command failed safely: {exc!r}", flush=True)
        if ack_ok:
            try:
                await guarded_interaction_edit_original(
                    interaction,
                    "time-interaction-error",
                    content="⚠️ ไม่สามารถสร้าง/ส่งผลลัพธ์ /time กลับไปใน Discord ได้ในขณะนี้",
                    embed=None,
                )
            except Exception as response_exc:
                print(f"⚠️ /time error response failed: {response_exc!r}", flush=True)

@bot.command(name="time")
async def boss_time_prefix(ctx: commands.Context):
    embed, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
    if embed is None:
        await guarded_context_send(ctx, tts_text_th, context="prefix-time")
        return
    await guarded_context_send(ctx, context="prefix-time", embed=embed)
    asyncio.create_task(speak_in_guild(ctx.guild, text_th=tts_text_th, text_en=tts_text_en, text_ko=tts_text_ko))
    await send_audit_log(ctx.guild, ctx.author, "เช็กเวลาบอสพร้อม TTS (!time)", "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย", discord.Color.purple())

async def boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Discord autocomplete must finish fast and return <=25 valid choices."""
    try:
        needle = (current or "").strip().casefold()
        names = sorted(
            {str(x).strip() for x in BOSS_RESPAWN_TIMES.keys() if str(x).strip()},
            key=str.casefold,
        )
        if needle:
            names = [x for x in names if needle in x.casefold()]
        result = []
        for boss in names[:25]:
            value = boss[:100]
            label = boss[:100]
            result.append(app_commands.Choice(name=label, value=value))
        return result
    except Exception as e:
        print(f"⚠️ boss autocomplete error: {e}")
        return []

@bot.tree.command(name="kill", description="บันทึกเวลาที่บอสตายเพื่อเริ่มคำนวณเวลานับถอยหลัง")
@app_commands.describe(
    boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการบันทึกเวลา",
    kill_time="ระบุเวลาที่บอสตาย (เช่น 17:30 หรือ 1730) ถ้าไม่ระบุจะใช้เวลาปัจจุบัน",
    kill_date="วันที่ (DD/MM/YYYY) (เว้นว่าง = วันนี้)"
)

@has_allowed_role()
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str = None, kill_date: str = None):
    # Acknowledge immediately.  /kill intentionally has NO autocomplete callback
    # so typing the boss name cannot trigger a separate autocomplete interaction.
    ack_ok = False
    try:
        ack_ok = await _safe_interaction_ack(interaction, ephemeral=False)
    except Exception as e:
        print(f"❌ /kill initial ACK failed: {e}", flush=True)
        return

    try:
        canonical_name = get_boss_canonical_name(boss_name)
        now = datetime.now(TZ_THAI)

        try:
            selected_date = parse_date_input(kill_date, now)
            if kill_time and kill_time.strip():
                parsed_time = parse_time_input(kill_time, now)
                boss_died_at = datetime(selected_date.year, selected_date.month, selected_date.day, parsed_time.hour, parsed_time.minute, parsed_time.second, tzinfo=TZ_THAI)
            else:
                boss_died_at = datetime(selected_date.year, selected_date.month, selected_date.day, now.hour, now.minute, now.second, tzinfo=TZ_THAI)
        except ValueError:
            if ack_ok:
                await guarded_interaction_followup_send(interaction, "interaction-followup",
                    "❌ วันที่/เวลาไม่ถูกต้อง! วันที่ใช้รูปแบบ **DD/MM/YYYY** เช่น **29/08/2026** และเวลาใช้ **17:30** หรือ **1730**",
                    ephemeral=True
                )
            else:
                print("⚠️ /kill input validation failed but interaction ACK was unavailable; followup skipped safely", flush=True)
            return

        respawn_time = get_boss_respawn_time(canonical_name)
        next_spawn = boss_died_at + respawn_time
        is_already_past = next_spawn <= now
        user_name = interaction.user.display_name

        record = {
            "spawn_time": next_spawn.isoformat(),
            "killTimeMs": int(boss_died_at.timestamp() * 1000),
            "killDate": boss_died_at.strftime("%Y-%m-%d"),
            "channelId": interaction.channel_id,
            "notifiedNotice": is_already_past,
            "notifiedSpawn": is_already_past,
            "voiceNoticeSent": is_already_past,
            "voiceSpawnSent": is_already_past,
            "noticeMinutes": int(get_boss_advance_notice_seconds(canonical_name) / 60),
            "recordedBy": user_name,
            "recordedByDisplayName": user_name,
            "recordedByUserId": str(interaction.user.id),
            "spawnTimeMs": int(next_spawn.timestamp() * 1000),
            "confirmationRequestId": (uuid.uuid4().hex),
            "confirmationRequestedAt": datetime.now(TZ_THAI).isoformat(),
            "confirmationStatus": "pending"
        }

        with schedule_lock:
            boss_schedule[canonical_name] = {
                "spawn_time": next_spawn,
                "killTimeMs": record["killTimeMs"],
                "killDate": record["killDate"],
                "channel_id": interaction.channel_id,
                "notified_advance": is_already_past,
                "notified_spawn": is_already_past,
                "voice_notice_sent": is_already_past,
                "voice_spawn_sent": is_already_past,
                "noticeMinutes": record["noticeMinutes"],
                "recorded_by": user_name,
                "recordedByUserId": str(interaction.user.id),
                "confirmationRequestId": record["confirmationRequestId"],
                "confirmationRequestedAt": record["confirmationRequestedAt"],
                "confirmationStatus": "pending"
            }

        cd_text = get_boss_cd_text(canonical_name)

        embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
        embed.add_field(name="👾 ชื่อบอส", value=f"`{canonical_name}`", inline=True)
        embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
        embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=cd_text, inline=True)
        embed.add_field(name="👤 ผู้บันทึก", value=f"`{user_name}`", inline=True)
        embed.add_field(name="🔔 บอสจะเกิดเวลา", value=f"**{next_spawn.strftime('%H:%M:%S น.')}**", inline=False)
        embed.set_footer(text=f"บันทึกโดย {user_name}")

        # Only send a followup when the initial interaction ACK succeeded.
        # If Discord rejected the initial callback (for example HTTP 429), a followup
        # would be invalid and would create avoidable REST traffic during the restriction.
        if ack_ok:
            await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
        else:
            print(
                f"⚠️ /kill saved locally but initial interaction ACK was unavailable | "
                f"boss={canonical_name} | followup skipped safely",
                flush=True,
            )

        async def persist_kill():
            try:
                with schedule_lock:
                    current = dict(boss_schedule.get(canonical_name, {}))
                firebase_record = _schedule_record_to_firebase(canonical_name, current)
                await asyncio.wait_for(
                    asyncio.to_thread(db.reference(f"boss_schedule/{canonical_name}").set, firebase_record),
                    timeout=10
                )
                print(f"💾 /kill saved: {canonical_name} | kill={boss_died_at.strftime("%d/%m/%Y %H:%M:%S")} | spawn={next_spawn.isoformat()}")
                try:
                    with schedule_lock:
                        confirm_data = dict(boss_schedule.get(canonical_name, {}))
                    await _voice_confirm_boss_recording(canonical_name, confirm_data)
                except Exception as confirmation_error:
                    print(f"⚠️ /kill confirmation failed: {confirmation_error}")
            except Exception as e:
                print(f"❌ /kill Firebase save failed: {e}")
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
        print(f"❌ /kill unexpected error: {e}", flush=True)
        traceback.print_exc()
        if ack_ok:
            try:
                await guarded_interaction_followup_send(interaction, "interaction-followup", f"❌ /kill เกิดข้อผิดพลาด: `{e}`", ephemeral=True)
            except Exception:
                pass
        else:
            print("⚠️ /kill error response skipped because initial interaction ACK was unavailable", flush=True)

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
    # Initial interaction ACK is time-critical and intentionally isolated from
    # background REST cooldown.  If Discord rejects it (for example during a
    # temporary API restriction), we still complete the Firebase write but do
    # not generate additional followup/audit REST traffic that is known to fail.
    # Critical initial response: use a direct message rather than defer(), then
    # update the original response after Firebase persistence. This is still
    # subject to Discord's interaction callback rate limits; no retry storm is
    # attempted when Discord returns HTTP 429.
    ack_ok = await _safe_interaction_send_message(
        interaction,
        "⏳ กำลังเพิ่มบอสเข้า Boss Definition...",
        ephemeral=False,
    )
    name = (name or "").strip()
    if not name:
        await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ กรุณาระบุชื่อบอส", ephemeral=True)
        return
    if any(c in name for c in "/\\.#$[]"):
        await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ ชื่อบอสมีอักขระที่ Firebase ไม่อนุญาต (/ . # $ [ ])", ephemeral=True)
        return
    total_seconds = hours * 3600 + minutes * 60 + seconds
    if total_seconds <= 0 or notice_minutes < 1:
        await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ CD ต้องมากกว่า 0 วินาที และ notice ต้องอย่างน้อย 1 นาที", ephemeral=True)
        return
    canonical = get_boss_canonical_name(name)
    if canonical in BOSS_RESPAWN_TIMES and canonical in DEFAULT_BOSS_NAMES:
        await guarded_interaction_followup_send(interaction, "interaction-followup", f"⚠️ บอส **{canonical}** มีอยู่ในระบบแล้ว — /addboss ใช้เพิ่มชื่อบอสใหม่เท่านั้น ไม่สร้าง Timer", ephemeral=True)
        return
    if "wadangka" in canonical.lower() or "วาดังการ์" in canonical:
        notice_minutes = 30
    BOSS_RESPAWN_TIMES[canonical] = timedelta(seconds=total_seconds)
    BOSS_CD_TEXT[canonical] = (f"{hours} ชั่วโมง " if hours else "") + (f"{minutes} นาที " if minutes else "") + (f"{seconds} วินาที" if seconds else "")
    BOSS_CD_TEXT[canonical] = BOSS_CD_TEXT[canonical].strip() or "0 วินาที"
    ADVANCE_NOTICE_SECONDS[canonical] = notice_minutes * 60
    ADVANCE_NOTICE_TEXT[canonical] = f"{notice_minutes} นาที"
    BOSS_PRONUNCIATION.setdefault(canonical, canonical)
    now_iso = datetime.now(TZ_THAI).isoformat()
    custom_bosses[canonical] = {
        "respawnSeconds": int(total_seconds),
        "noticeMinutes": int(notice_minutes),
        "cdText": BOSS_CD_TEXT[canonical],
        "pronunciation": BOSS_PRONUNCIATION[canonical],
        "createdAt": custom_bosses.get(canonical, {}).get("createdAt", now_iso),
        "updatedAt": now_iso,
        "createdBy": str(interaction.user.display_name),
        "createdById": str(interaction.user.id)
    }
    saved_ok = await save_custom_bosses_to_github()
    if not saved_ok:
        if ack_ok:
            await guarded_interaction_edit_original(
                interaction,
                "interaction-edit",
                content="❌ เพิ่มบอสไม่สำเร็จในการบันทึก Firebase — ไม่ถือว่าสำเร็จจนกว่าจะบันทึกได้",
            )
        else:
            print(f"⚠️ /add boss Firebase save failed and interaction ACK unavailable | boss={canonical}", flush=True)
        return
    # IMPORTANT: /addboss never writes boss_schedule.
    success_text = (
        f"✅ เพิ่มบอส **{canonical}** เข้า Boss Definition สำเร็จ\n"
        f"⏳ CD สำหรับ /kill: **{BOSS_CD_TEXT[canonical]}**\n"
        f"🔔 แจ้งเตือนล่วงหน้า: **{notice_minutes} นาที**\n"
        f"📌 ยังไม่ได้สร้าง Timer — ใช้ `/kill {canonical}` เมื่อบอสตาย"
    )
    if ack_ok:
        await guarded_interaction_edit_original(
            interaction,
            "interaction-edit",
            content=success_text,
        )
    else:
        # Firebase persistence is still completed, but do not send followups/audit
        # while Discord has rejected the interaction callback.  This avoids creating
        # another guaranteed-failing REST request during the active restriction.
        print(
            f"⚠️ /add boss saved successfully but Discord interaction ACK unavailable | "
            f"boss={canonical} | followup skipped safely",
            flush=True,
        )
        return
    await send_audit_log(interaction.guild, interaction.user, "เพิ่มบอส (/addboss)", f"➕ `{canonical}` | CD {BOSS_CD_TEXT[canonical]} | ไม่มีการสร้าง boss_schedule", discord.Color.green())

@bot.tree.command(name="delboss", description="ลบบอสออกจากตารางนับถอยหลัง")
@app_commands.describe(boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def del_boss(interaction: discord.Interaction, boss_name: str):
    await _safe_interaction_ack(interaction, ephemeral=False)
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
        await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
        await send_audit_log(interaction.guild, interaction.user, "ลบบอส (/delboss)", f"🗑️ ลบบอส: `{matched_key}`", discord.Color.orange())
    else:
        await guarded_interaction_followup_send(interaction, "interaction-followup", f"❌ ไม่พบบอส **{boss_name}** ในตารางนับถอยหลังขณะนี้", ephemeral=True)

@bot.tree.command(name="status", description="เช็กสถานะเวลาบอสทั้งหมดที่กำลังนับถอยหลัง")
async def boss_status(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
    with schedule_lock: schedule_copy = boss_schedule.copy()
    if not schedule_copy:
        embed = discord.Embed(title="📜 ตารางเวลาบอส", description="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ\nใช้คำสั่ง `/kill [ชื่อบอส]` เพื่อเริ่มบันทึกเวลาได้เลยครับ", color=discord.Color.blue())
        await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
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
    await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)

@bot.tree.command(name="setlive", description="ตั้งค่าป้ายไฟนับถอยหลังเวลาบอสเกิด Real-time ในช่องนี้")
@has_allowed_role()
async def set_live(interaction: discord.Interaction):
    await _safe_interaction_ack(interaction, ephemeral=False)
    now = datetime.now(TZ_THAI)
    embed = discord.Embed(title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time", description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`", color=discord.Color.teal())
    embed.add_field(name="📌 สถานะ", value="กำลังเริ่มต้นระบบ...", inline=False)
    embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")

    msg = await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)
    if msg is None:
        print("⏭️ /setlive skipped while Discord REST global cooldown is active", flush=True)
        return
    global live_message_config, cached_live_message
    live_message_config = {"channel_id": interaction.channel_id, "message_id": msg.id}
    cached_live_message = msg
    await save_live_config()
    await send_audit_log(interaction.guild, interaction.user, "สร้าง Live Embed (/setlive)", f"📌 ช่อง: <#{interaction.channel_id}>\nMessage ID: `{msg.id}`", discord.Color.teal())


# ==========================================
# ⚔️ BOSS RAID ATTENDANCE SYSTEM (V78)
# ใช้ Firebase root ใหม่ raid_attendance แยกจาก boss_schedule
# ==========================================

def is_guild_admin_or_owner(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member) or not member.guild:
        return False
    return bool(member.id == member.guild.owner_id or member.guild_permissions.administrator)


def _attendance_config_snapshot(guild_id: int):
    with schedule_lock:
        cfg = dict(attendance_config.get(str(guild_id), {}) or {})
    return cfg


def _autoattendance_enabled_for_guild(guild_id: int) -> bool:
    cfg = _attendance_config_snapshot(guild_id)
    return parse_bool(cfg.get("autoattendance_enabled"), False)


def _safe_firebase_key(value: str, max_len: int = 64) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value or "").strip()).strip("_")
    return (cleaned or "item")[:max_len]


def _next_library_boss_occurrence(now: datetime, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


LIBRARY_BOSS_SCHEDULE_SPECS = (
    ("Library Boss 09-00", 9, "09-00"),
    ("Library Boss 21-00", 21, "21-00"),
)


def _canonical_library_boss_key(key: str) -> str:
    cleaned = str(key or "").strip()
    compact = cleaned.replace("_", " ").replace(":", "-")
    aliases = {
        "Library Boss 09-00": "Library Boss 09-00",
        "Library Boss 09-00": "Library Boss 09-00",
        "Library Boss 09 00": "Library Boss 09-00",
        "Library_Boss_09-00": "Library Boss 09-00",
        "Library_Boss_09_00": "Library Boss 09-00",
        "Library Boss 21-00": "Library Boss 21-00",
        "Library Boss 21 00": "Library Boss 21-00",
        "Library_Boss_21-00": "Library Boss 21-00",
        "Library_Boss_21_00": "Library Boss 21-00",
    }
    return aliases.get(cleaned, aliases.get(compact, cleaned))


def _library_schedule_alias_keys(canonical_key: str) -> set[str]:
    if canonical_key == "Library Boss 09-00":
        return {"Library_Boss_09-00", "Library_Boss_09_00", "Library Boss 09 00"}
    if canonical_key == "Library Boss 21-00":
        return {"Library_Boss_21-00", "Library_Boss_21_00", "Library Boss 21 00"}
    return set()


async def ensure_library_boss_schedule_records(*, force_refresh: bool = False) -> None:
    """Ensure exactly two canonical recurring Library Boss rows exist in boss_schedule.

    V96 fix: V95 wrote the in-memory key with spaces but wrote Firebase using
    _safe_firebase_key(), producing a second underscore-key row. Firebase root-sync
    then loaded both rows, so Dashboard displayed duplicates and the next-day 09:00
    occurrence could be overwritten by the stale alias. This function now uses the same
    canonical key in memory and Firebase, migrates/deletes legacy underscore aliases,
    and advances each fixed daily slot independently after it has passed.
    """
    global is_updating_from_bot

    now = datetime.now(TZ_THAI)
    migrated = 0
    changed = 0
    aliases_deleted = 0

    try:
        root = await asyncio.wait_for(asyncio.to_thread(db.reference("boss_schedule").get), timeout=8)
    except Exception as exc:
        root = None
        print(f"⚠️ Library Boss schedule root read failed safely: {exc}", flush=True)
    if not isinstance(root, dict):
        root = {}

    try:
        is_updating_from_bot = True

        for canonical_key, hour, slot_text in LIBRARY_BOSS_SCHEDULE_SPECS:
            candidates = []
            for raw_key, raw_data in root.items():
                if not isinstance(raw_data, dict):
                    continue
                if _canonical_library_boss_key(raw_key) == canonical_key:
                    candidates.append((str(raw_key), dict(raw_data)))

            # Prefer the canonical human-readable key; otherwise migrate the alias.
            existing_key = canonical_key if any(k == canonical_key for k, _ in candidates) else (candidates[0][0] if candidates else None)
            existing = next((d for k, d in candidates if k == existing_key), {}) if existing_key else {}
            existing_spawn = parse_to_thai_datetime(
                existing.get("spawn_time") or existing.get("spawnTimeMs")
            )
            desired_spawn = _next_library_boss_occurrence(now, hour)

            # Keep a still-future occurrence. Once it has crossed, advance exactly
            # one occurrence for this fixed daily slot.
            if existing_spawn and existing_spawn > now + timedelta(seconds=5):
                target_spawn = existing_spawn
            else:
                target_spawn = desired_spawn

            target_ms = int(target_spawn.timestamp() * 1000)
            existing_ms = int(existing_spawn.timestamp() * 1000) if existing_spawn else None
            metadata_ok = (
                parse_bool(existing.get("is_library_boss_schedule"), False)
                and str(existing.get("library_slot") or "") == slot_text
                and str(existing.get("recurrence") or "") == "daily"
            )
            needs_write = force_refresh or existing_ms != target_ms or not metadata_ok or existing_key != canonical_key

            record = {
                "spawnTimeMs": target_ms,
                "spawn_time": target_spawn.isoformat(),
                "noticeMinutes": 30,
                "recordedBy": "SYSTEM",
                "recordedByDisplayName": "SKYNET Auto Schedule",
                "recordedByUserId": "",
                "confirmationRequestId": "",
                "confirmationRequestedAt": None,
                "confirmationStatus": "",
                "notifiedNotice": False,
                "notifiedSpawn": False,
                "voiceNoticeSent": False,
                "voiceSpawnSent": False,
                "suppressBossNotifications": True,
                "is_library_boss_schedule": True,
                "library_slot": slot_text,
                "recurrence": "daily",
                "autoAttendanceEligible": True,
            }

            if needs_write:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(db.reference(f"boss_schedule/{canonical_key}").set, record),
                        timeout=8,
                    )
                    changed += 1
                    if existing_key and existing_key != canonical_key:
                        migrated += 1
                except Exception as exc:
                    print(f"⚠️ Library Boss canonical schedule write failed | key={canonical_key} | {exc}", flush=True)
                    continue

            # Remove all known alias keys so the root can only contain one row per slot.
            alias_keys = _library_schedule_alias_keys(canonical_key)
            for alias_key in alias_keys:
                if alias_key not in root:
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(db.reference(f"boss_schedule/{alias_key}").delete), timeout=8
                    )
                    aliases_deleted += 1
                except Exception as exc:
                    print(f"⚠️ Library Boss alias cleanup failed | key={alias_key} | {exc}", flush=True)

            with schedule_lock:
                boss_schedule.pop(existing_key, None) if existing_key and existing_key != canonical_key else None
                boss_schedule[canonical_key] = _firebase_to_internal(canonical_key, record) or {
                    "spawn_time": target_spawn,
                    "noticeMinutes": 30,
                    "notified_advance": False,
                    "notified_spawn": False,
                    "voice_notice_sent": False,
                    "voice_spawn_sent": False,
                    "recorded_by": "SYSTEM",
                    "recordedByDisplayName": "SKYNET Auto Schedule",
                    "is_library_boss_schedule": True,
                    "library_slot": slot_text,
                    "suppressBossNotifications": True,
                    "autoAttendanceEligible": True,
                }

        # Remove stale alias keys from the local in-memory cache too.
        with schedule_lock:
            for raw_key in list(boss_schedule.keys()):
                canonical = _canonical_library_boss_key(raw_key)
                if canonical in {spec[0] for spec in LIBRARY_BOSS_SCHEDULE_SPECS} and raw_key != canonical:
                    boss_schedule.pop(raw_key, None)
    finally:
        is_updating_from_bot = False

    print(
        f"📚 Library Boss schedule ensured | changed={changed} | migrated={migrated} | "
        f"aliases_deleted={aliases_deleted} | slots=09:00,21:00",
        flush=True,
    )


async def save_attendance_config():
    with schedule_lock:
        data = {str(k): dict(v) for k, v in (attendance_config or {}).items()}
    try:
        await asyncio.wait_for(
            asyncio.to_thread(db.reference("attendance_config").set, data), timeout=8
        )
    except Exception as e:
        print(f"⚠️ บันทึก attendance_config ลง Firebase ไม่สำเร็จ: {e}", flush=True)
    await asyncio.to_thread(set_db_value, "attendance_config", data)
    await asyncio.to_thread(save_json_local, "attendance_config.json", data)


async def load_attendance_config():
    global attendance_config
    data = None
    try:
        data = await asyncio.to_thread(db.reference("attendance_config").get)
    except Exception as e:
        print(f"⚠️ โหลด attendance_config จาก Firebase ไม่สำเร็จ: {e}", flush=True)
    if not isinstance(data, dict) or not data:
        data = get_db_value("attendance_config", None)
    normalized = {}
    if isinstance(data, dict):
        for guild_id, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            cid = cfg.get("summary_channel_id")
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            try:
                gid = int(cfg.get("guild_id", guild_id))
            except (TypeError, ValueError):
                continue
            normalized[str(gid)] = {
                "guild_id": gid,
                "summary_channel_id": cid,
                "channel_name": str(cfg.get("channel_name") or ""),
                "updated_by": str(cfg.get("updated_by") or ""),
                "updated_at": str(cfg.get("updated_at") or ""),
                # Auto Attendance is opt-in. Missing legacy values remain OFF.
                "autoattendance_enabled": parse_bool(cfg.get("autoattendance_enabled"), False),
                "autoattendance_updated_by": str(cfg.get("autoattendance_updated_by") or ""),
                "autoattendance_updated_at": str(cfg.get("autoattendance_updated_at") or ""),
            }
    attendance_config = normalized
    print(f"✅ load_attendance_config สำเร็จ ({len(attendance_config)} server(s))", flush=True)
    return attendance_config


def _attendance_now_iso():
    return datetime.now(TZ_THAI).isoformat()


def _attendance_normalize_time_text(time_text: str) -> str | None:
    """Accept Attendance time as HH:MM or HHMM and return canonical HH:MM."""
    raw = str(time_text or "").strip().replace(".", ":")
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", raw):
            hour_text, minute_text = raw.split(":", 1)
        elif re.fullmatch(r"\d{3,4}", raw):
            hour_text, minute_text = (raw[0], raw[1:]) if len(raw) == 3 else (raw[:2], raw[2:])
        else:
            return None
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"
    except (TypeError, ValueError):
        return None

def _attendance_parse_local(date_text: str, time_text: str) -> datetime | None:
    normalized = _attendance_normalize_time_text(time_text)
    if not normalized:
        return None
    try:
        dt = datetime.strptime(f"{date_text} {normalized}", "%d/%m/%Y %H:%M")
        return dt.replace(tzinfo=TZ_THAI)
    except Exception:
        return None


def _attendance_activity_path(guild_id: int, activity_id: str) -> str:
    return f"raid_attendance/{int(guild_id)}/{activity_id}"


def _attendance_member_display(member: discord.Member | discord.User) -> str:
    return clean_display_name(getattr(member, "display_name", getattr(member, "name", "member")))


def _attendance_language_blocks(activity: dict, participants: list[dict], *, closed=False):
    enabled = get_enabled_discord_notification_languages()
    if not enabled:
        enabled = ["th"]
    count = sum(1 for p in participants if str(p.get("status")) == "checked_in")
    cancelled = sum(1 for p in participants if str(p.get("status")) == "cancelled")
    boss = str(activity.get("boss_name") or "Boss")
    attack = str(activity.get("attack_time") or "-")
    date_text = str(activity.get("activity_date") or "-")
    status = str(activity.get("status") or "scheduled")
    title_by_lang = {
        "th": "🔒 BOSS RAID ปิดเช็คชื่อ" if closed else "⚔️ BOSS RAID ATTENDANCE",
        "en": "🔒 BOSS RAID CHECK-IN CLOSED" if closed else "⚔️ BOSS RAID ATTENDANCE",
        "ko": "🔒 보스 레이드 출석 마감" if closed else "⚔️ 보스 레이드 출석",
    }
    line_by_lang = {
        "th": f"⚔️ บอส: **{boss}**\n📅 วันที่: **{date_text}**\n⏰ เวลาโจมตี: **{attack} น.**\n👥 ผู้เข้าร่วม: **{count} คน**",
        "en": f"⚔️ Boss: **{boss}**\n📅 Date: **{date_text}**\n⏰ Attack Time: **{attack}**\n👥 Checked in: **{count}**",
        "ko": f"⚔️ 보스: **{boss}**\n📅 날짜: **{date_text}**\n⏰ 공격 시간: **{attack}**\n👥 참석: **{count}명**",
    }
    return enabled, title_by_lang, line_by_lang, count, cancelled, status


def build_raid_activity_embed(activity: dict, participants: list[dict], *, closed=False):
    enabled, title_map, body_map, count, cancelled, status = _attendance_language_blocks(activity, participants, closed=closed)
    primary = enabled[0]
    embed = discord.Embed(title=title_map[primary], color=discord.Color.red() if closed else discord.Color.blurple(), timestamp=datetime.now(TZ_THAI))
    for lang in enabled:
        prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
        embed.add_field(name=prefix, value=body_map[lang], inline=False)
    if not closed:
        open_text = str(activity.get("checkin_open") or "-")
        close_text = str(activity.get("checkin_close") or "-")
        extra = {
            "th": f"🟢 เปิดเช็คชื่อ: **{open_text} น.**\n🔴 ปิดเช็คชื่อ: **{close_text} น.**",
            "en": f"🟢 Check-in opens: **{open_text}**\n🔴 Check-in closes: **{close_text}**",
            "ko": f"🟢 출석 시작: **{open_text}**\n🔴 출석 마감: **{close_text}**",
        }
        for lang in enabled:
            prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
            embed.add_field(name=f"{prefix} • Schedule", value=extra[lang], inline=False)
    else:
        extra = {
            "th": f"✅ เช็กชื่อ: **{count}**\n❌ ยกเลิก: **{cancelled}**",
            "en": f"✅ Checked in: **{count}**\n❌ Cancelled: **{cancelled}**",
            "ko": f"✅ 출석: **{count}명**\n❌ 취소: **{cancelled}명**",
        }
        for lang in enabled:
            prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
            embed.add_field(name=f"{prefix} • Result", value=extra[lang], inline=False)
    embed.set_footer(text=f"Activity ID: {activity.get('activity_id', '-')} | Status: {status}")
    return embed


def build_raid_summary_embed(activity: dict, participants: list[dict]):
    enabled, title_map, body_map, count, cancelled, _ = _attendance_language_blocks(activity, participants, closed=True)
    primary = enabled[0]
    embed = discord.Embed(title=title_map[primary], color=discord.Color.green(), timestamp=datetime.now(TZ_THAI))
    for lang in enabled:
        prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
        embed.add_field(name=prefix, value=body_map[lang], inline=False)
    checked = [p for p in participants if p.get("status") == "checked_in"]
    names = []
    for idx, p in enumerate(checked, 1):
        names.append(f"{idx}. {p.get('display_name') or p.get('username') or p.get('user_id')}")
    name_text = "\n".join(names) if names else "-"
    if len(name_text) > 3900:
        name_text = name_text[:3890] + "\n…"
    labels = {"th": "📋 รายชื่อสมาชิก", "en": "📋 Participants", "ko": "📋 참석자"}
    embed.add_field(name=labels[primary], value=name_text, inline=False)
    return embed


async def _attendance_fetch_activity(guild_id: int, activity_id: str):
    try:
        data = await asyncio.wait_for(asyncio.to_thread(db.reference(_attendance_activity_path(guild_id, activity_id)).get), timeout=8)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"⚠️ อ่าน Attendance activity ไม่สำเร็จ: {guild_id}/{activity_id}: {e}", flush=True)
        return None


async def _attendance_fetch_participants(guild_id: int, activity_id: str):
    activity = await _attendance_fetch_activity(guild_id, activity_id)
    if not activity:
        return None, []
    participants = activity.get("participants") if isinstance(activity.get("participants"), dict) else {}
    rows = []
    for uid, p in participants.items():
        if not isinstance(p, dict):
            continue
        row = dict(p)
        row.setdefault("user_id", str(uid))
        rows.append(row)
    rows.sort(key=lambda p: str(p.get("checked_in_at") or p.get("updated_at") or ""))
    return activity, rows


async def _attendance_refresh_panel(guild: discord.Guild, activity: dict, participants: list[dict], *, closed=False):
    try:
        channel_id = int(activity.get("panel_channel_id") or 0)
        message_id = int(activity.get("panel_message_id") or 0)
    except (TypeError, ValueError):
        return
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = channel.get_partial_message(message_id)
        view = RaidAttendanceView(str(activity.get("activity_id"))) if not closed else RaidAttendanceView(str(activity.get("activity_id")), disabled=True)
        await guarded_message_edit(message, context=f"attendance:panel-edit:{activity.get('activity_id')}", embed=build_raid_activity_embed(activity, participants, closed=closed), view=view, background=False)
    except Exception as e:
        print(f"⚠️ อัปเดต Attendance panel ไม่สำเร็จ: {e}", flush=True)


async def close_raid_activity(guild: discord.Guild, activity_id: str, *, reason="scheduled-close"):
    async with attendance_lifecycle_lock:
        activity, participants = await _attendance_fetch_participants(guild.id, activity_id)
        if not activity or str(activity.get("status")) == "closed":
            return False
        activity["status"] = "closed"
        activity["closed_at"] = _attendance_now_iso()
        activity["closed_reason"] = reason
        try:
            await asyncio.wait_for(
                asyncio.to_thread(db.reference(_attendance_activity_path(guild.id, activity_id)).update, {
                    "status": "closed",
                    "closed_at": activity["closed_at"],
                    "closed_reason": reason,
                }), timeout=8
            )
        except Exception as e:
            print(f"⚠️ ปิด Attendance activity ไม่สำเร็จ: {guild.id}/{activity_id}: {e}", flush=True)
            return False
        await _attendance_refresh_panel(guild, activity, participants, closed=True)
        cfg = _attendance_config_snapshot(guild.id)
        summary_id = cfg.get("summary_channel_id")
        channel = guild.get_channel(int(summary_id)) if summary_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                embed = build_raid_summary_embed(activity, participants)
                await guarded_channel_send(channel, context=f"attendance:summary:{activity_id}", embed=embed, background=True)
            except Exception as e:
                print(f"⚠️ ส่ง Attendance summary ไม่สำเร็จ: {guild.name}: {e}", flush=True)
        print(f"📊 Attendance activity closed | guild={guild.name} | activity={activity_id} | participants={sum(1 for p in participants if p.get('status')=='checked_in')}", flush=True)
        return True


class RaidAttendanceCreateModal(discord.ui.Modal, title="⚔️ Create Boss Raid Activity"):
    def __init__(self, boss_name: str):
        super().__init__(timeout=300)
        self.boss_name = discord.ui.TextInput(label="Boss", default=boss_name[:100], max_length=100, required=True)
        self.activity_date = discord.ui.TextInput(label="วันที่ (DD/MM/YYYY)", placeholder="09/09/2026", max_length=10, required=True)
        self.attack_time = discord.ui.TextInput(label="เวลาโจมตี (HH:MM หรือ HHMM)", placeholder="20:30 หรือ 2030", max_length=5, required=True)
        self.checkin_open = discord.ui.TextInput(label="เปิดเช็คชื่อ (HH:MM หรือ HHMM)", placeholder="20:15 หรือ 2015", max_length=5, required=True)
        self.checkin_close = discord.ui.TextInput(label="ปิดเช็คชื่อ (HH:MM หรือ HHMM)", placeholder="20:45 หรือ 2045", max_length=5, required=True)
        for item in (self.boss_name, self.activity_date, self.attack_time, self.checkin_open, self.checkin_close):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not is_guild_admin_or_owner(interaction.user):
            await interaction.response.send_message("❌ เฉพาะ Admin หรือ Server Owner เท่านั้นที่สร้างกิจกรรมได้", ephemeral=True)
            return
        await _safe_interaction_ack(interaction, ephemeral=True)
        date_text = str(self.activity_date.value).strip()
        attack_input = str(self.attack_time.value).strip()
        open_input = str(self.checkin_open.value).strip()
        close_input = str(self.checkin_close.value).strip()
        attack_text = _attendance_normalize_time_text(attack_input)
        open_text = _attendance_normalize_time_text(open_input)
        close_text = _attendance_normalize_time_text(close_input)
        attack_dt = _attendance_parse_local(date_text, attack_input)
        open_dt = _attendance_parse_local(date_text, open_input)
        close_dt = _attendance_parse_local(date_text, close_input)
        if not attack_dt or not open_dt or not close_dt or not attack_text or not open_text or not close_text or not (open_dt <= attack_dt <= close_dt):
            await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ วันที่/เวลาไม่ถูกต้อง หรือช่วงเปิด-ปิดไม่ครอบคลุมเวลาโจมตี", ephemeral=True)
            return
        cfg = _attendance_config_snapshot(interaction.guild.id)
        summary_id = cfg.get("summary_channel_id")
        summary_channel = interaction.guild.get_channel(int(summary_id)) if summary_id else None
        if not isinstance(summary_channel, discord.TextChannel):
            await guarded_interaction_followup_send(interaction, "interaction-followup", "❌ ยังไม่ได้ตั้งห้องสรุป Attendance ใช้ `/set-notification` → `ตั้งห้องสรุป Attendance` ก่อน", ephemeral=True)
            return
        activity_id = f"{attack_dt.strftime('%Y%m%d')}_{re.sub(r'[^A-Za-z0-9]+', '_', str(self.boss_name.value).strip())[:32]}_{attack_dt.strftime('%H%M')}_{uuid.uuid4().hex[:6]}"
        activity = {
            "activity_id": activity_id,
            "guild_id": interaction.guild.id,
            "boss_name": str(self.boss_name.value).strip(),
            "activity_date": date_text,
            "attack_time": attack_text,
            "checkin_open": open_text,
            "checkin_close": close_text,
            "open_at": open_dt.isoformat(),
            "close_at": close_dt.isoformat(),
            "attack_at": attack_dt.isoformat(),
            "status": "scheduled" if datetime.now(TZ_THAI) < open_dt else "open",
            "created_by": str(interaction.user.id),
            "created_by_name": _attendance_member_display(interaction.user),
            "created_at": _attendance_now_iso(),
            "panel_channel_id": interaction.channel_id,
            "panel_message_id": None,
            "summary_channel_id": int(summary_channel.id),
            "participants": {},
        }
        try:
            await asyncio.wait_for(asyncio.to_thread(db.reference(_attendance_activity_path(interaction.guild.id, activity_id)).set, activity), timeout=8)
        except Exception as e:
            await guarded_interaction_followup_send(interaction, "interaction-followup", f"❌ บันทึกกิจกรรมลง Firebase ไม่สำเร็จ: {e}", ephemeral=True)
            return
        view = RaidAttendanceView(activity_id)
        message = await guarded_interaction_followup_send(interaction, "interaction-followup", embed=build_raid_activity_embed(activity, [], closed=False), view=view, ephemeral=False)
        if message is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(db.reference(_attendance_activity_path(interaction.guild.id, activity_id)).update, {"panel_message_id": int(message.id)}), timeout=8)
                activity["panel_message_id"] = int(message.id)
                bot.add_view(view, message_id=int(message.id))
            except Exception as e:
                print(f"⚠️ บันทึก panel message ID ไม่สำเร็จ: {e}", flush=True)
        else:
            print(f"⚠️ สร้าง Attendance panel message ไม่สำเร็จ: {activity_id}", flush=True)
        await send_audit_log(interaction.guild, interaction.user, "สร้าง Boss Raid Attendance", f"Boss: `{activity['boss_name']}` | วันที่: `{date_text}` | เปิด: `{open_text}` | ปิด: `{close_text}` | Activity: `{activity_id}`", discord.Color.blurple())
        print(f"⚔️ Attendance activity created | guild={interaction.guild.name} | activity={activity_id}", flush=True)


class RaidAttendanceView(discord.ui.View):
    def __init__(self, activity_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.activity_id = str(activity_id)
        self.add_item(self._button("check", "✅ เช็กชื่อ", discord.ButtonStyle.success, disabled))
        self.add_item(self._button("cancel", "❌ ยกเลิกเช็กชื่อ", discord.ButtonStyle.danger, disabled))
        self.add_item(self._button("list", "📋 รายชื่อ", discord.ButtonStyle.secondary, False))

    def _button(self, action: str, label: str, style, disabled: bool):
        button = discord.ui.Button(label=label, style=style, custom_id=f"raid_attendance:{action}:{self.activity_id}", disabled=disabled)
        if action == "check":
            button.callback = self._check
        elif action == "cancel":
            button.callback = self._cancel
        else:
            button.callback = self._list
        return button

    async def _load(self, interaction: discord.Interaction):
        activity, participants = await _attendance_fetch_participants(interaction.guild.id, self.activity_id)
        return activity, participants

    async def _check(self, interaction: discord.Interaction):
        await _safe_interaction_ack(interaction, ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        activity, participants = await self._load(interaction)
        if not activity:
            await guarded_interaction_followup_send(interaction, "attendance-check", "❌ ไม่พบกิจกรรมนี้", ephemeral=True)
            return
        now = datetime.now(TZ_THAI)
        open_dt = parse_to_thai_datetime(activity.get("open_at"))
        close_dt = parse_to_thai_datetime(activity.get("close_at"))
        if str(activity.get("status")) == "closed" or not open_dt or not close_dt or not (open_dt <= now <= close_dt):
            await guarded_interaction_followup_send(interaction, "attendance-check", "🔒 กิจกรรมนี้ยังไม่เปิดเช็คชื่อหรือปิดเช็คชื่อแล้ว", ephemeral=True)
            return
        ref_path = f"{_attendance_activity_path(interaction.guild.id, self.activity_id)}/participants/{interaction.user.id}"
        record = {
            "user_id": str(interaction.user.id),
            "username": str(interaction.user.name),
            "display_name": _attendance_member_display(interaction.user),
            "checked_in_at": _attendance_now_iso(),
            "status": "checked_in",
        }
        transaction_result = None
        try:
            def _tx(current_value):
                if isinstance(current_value, dict) and current_value.get("status") == "checked_in":
                    return current_value
                return record
            transaction_result = await asyncio.wait_for(
                asyncio.to_thread(db.reference(ref_path).transaction, _tx), timeout=8
            )
            if isinstance(transaction_result, dict) and transaction_result.get("status") == "checked_in" and str(transaction_result.get("checked_in_at")) != str(record.get("checked_in_at")):
                await guarded_interaction_followup_send(interaction, "attendance-check", "⚠️ คุณเช็คชื่อกิจกรรมนี้แล้ว", ephemeral=True)
                return
            await asyncio.wait_for(asyncio.to_thread(db.reference(_attendance_activity_path(interaction.guild.id, self.activity_id)).update, {"updated_at": _attendance_now_iso()}), timeout=8)
        except Exception as e:
            await guarded_interaction_followup_send(interaction, "attendance-check", f"❌ บันทึกเช็คชื่อไม่สำเร็จ: {e}", ephemeral=True)
            return
        await guarded_interaction_followup_send(interaction, "attendance-check", "✅ เช็คชื่อเข้าร่วมกิจกรรมสำเร็จ", ephemeral=True)
        activity, participants = await self._load(interaction)
        await _attendance_refresh_panel(interaction.guild, activity, participants, closed=False)

    async def _cancel(self, interaction: discord.Interaction):
        await _safe_interaction_ack(interaction, ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        activity, participants = await self._load(interaction)
        if not activity:
            await guarded_interaction_followup_send(interaction, "attendance-cancel", "❌ ไม่พบกิจกรรมนี้", ephemeral=True)
            return
        now = datetime.now(TZ_THAI)
        open_dt = parse_to_thai_datetime(activity.get("open_at"))
        close_dt = parse_to_thai_datetime(activity.get("close_at"))
        if str(activity.get("status")) == "closed" or not open_dt or not close_dt or not (open_dt <= now <= close_dt):
            await guarded_interaction_followup_send(interaction, "attendance-cancel", "🔒 กิจกรรมนี้ปิดเช็คชื่อแล้ว", ephemeral=True)
            return
        ref_path = f"{_attendance_activity_path(interaction.guild.id, self.activity_id)}/participants/{interaction.user.id}"
        try:
            current = await asyncio.wait_for(asyncio.to_thread(db.reference(ref_path).get), timeout=8)
        except Exception as e:
            await guarded_interaction_followup_send(interaction, "attendance-cancel", f"❌ อ่านข้อมูลเช็คชื่อไม่สำเร็จ: {e}", ephemeral=True)
            return
        if not isinstance(current, dict) or current.get("status") != "checked_in":
            await guarded_interaction_followup_send(interaction, "attendance-cancel", "ℹ️ คุณยังไม่ได้เช็คชื่อกิจกรรมนี้", ephemeral=True)
            return
        current.update({"status": "cancelled", "cancelled_at": _attendance_now_iso()})
        try:
            await asyncio.wait_for(asyncio.to_thread(db.reference(ref_path).update, current), timeout=8)
        except Exception as e:
            await guarded_interaction_followup_send(interaction, "attendance-cancel", f"❌ ยกเลิกเช็คชื่อไม่สำเร็จ: {e}", ephemeral=True)
            return
        await guarded_interaction_followup_send(interaction, "attendance-cancel", "❌ ยกเลิกเช็คชื่อเรียบร้อยแล้ว", ephemeral=True)
        activity, participants = await self._load(interaction)
        await _attendance_refresh_panel(interaction.guild, activity, participants, closed=False)

    async def _list(self, interaction: discord.Interaction):
        await _safe_interaction_ack(interaction, ephemeral=True)
        if not interaction.guild:
            return
        activity, participants = await self._load(interaction)
        if not activity:
            await guarded_interaction_followup_send(interaction, "attendance-list", "❌ ไม่พบกิจกรรมนี้", ephemeral=True)
            return
        checked = [p for p in participants if p.get("status") == "checked_in"]
        names = [f"{idx}. {p.get('display_name') or p.get('username') or p.get('user_id')}" for idx, p in enumerate(checked, 1)]
        if len(names) > 50:
            names = names[:50] + [f"… และอีก {len(checked)-50} คน"]
        await guarded_interaction_followup_send(interaction, "attendance-list", embed=discord.Embed(title="📋 รายชื่อผู้เข้าร่วม", description="\n".join(names) if names else "-", color=discord.Color.blurple()), ephemeral=True)


async def restore_raid_attendance_views():
    total = 0
    for guild in list(bot.guilds):
        try:
            data = await asyncio.wait_for(asyncio.to_thread(db.reference(f"raid_attendance/{guild.id}").get), timeout=8)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for activity_id, activity in data.items():
            if not isinstance(activity, dict):
                continue
            if str(activity.get("status")) == "closed":
                continue
            try:
                msg_id = int(activity.get("panel_message_id") or 0)
            except (TypeError, ValueError):
                msg_id = 0
            view = RaidAttendanceView(str(activity_id))
            try:
                if msg_id:
                    bot.add_view(view, message_id=msg_id)
                else:
                    bot.add_view(view)
                total += 1
            except Exception as e:
                print(f"⚠️ restore Attendance View failed: {guild.id}/{activity_id}: {e}", flush=True)
    print(f"✅ restore_raid_attendance_views สำเร็จ ({total} active view(s))", flush=True)


async def _autoattendance_find_or_create_for_schedule(
    guild: discord.Guild,
    boss_name: str,
    schedule: dict,
    activities: dict,
    now: datetime,
) -> bool:
    """Create one Auto Attendance activity for a boss spawn in the 30-minute window.

    Uses a deterministic Firebase activity ID based on the source schedule key and
    exact spawn timestamp, so repeated lifecycle ticks/restarts remain idempotent.
    """
    if not _autoattendance_enabled_for_guild(guild.id):
        return False

    # User-configured exclusion list: do not create or retry Auto Attendance for these bosses.
    if is_auto_attendance_excluded_boss(boss_name):
        return False

    spawn_dt = parse_to_thai_datetime(schedule.get("spawn_time") or schedule.get("spawnTimeMs"))
    if not spawn_dt:
        return False

    open_dt = spawn_dt - timedelta(minutes=30)
    if not (open_dt <= now < spawn_dt):
        return False

    cfg = _attendance_config_snapshot(guild.id)
    summary_id = cfg.get("summary_channel_id")
    try:
        summary_channel = guild.get_channel(int(summary_id)) if summary_id else None
    except (TypeError, ValueError):
        summary_channel = None

    if not isinstance(summary_channel, discord.TextChannel):
        # Do not manufacture a second channel setting. The existing Attendance system
        # already requires an Attendance summary channel to be configured.
        return False

    spawn_ms = int(spawn_dt.timestamp() * 1000)
    safe_boss = _safe_firebase_key(boss_name, 40)
    activity_id = f"auto_{spawn_dt.strftime('%Y%m%d_%H%M')}_{safe_boss}_{str(spawn_ms)[-6:]}"
    activity = activities.get(activity_id) if isinstance(activities, dict) else None

    if not isinstance(activity, dict):
        date_text = spawn_dt.strftime("%d/%m/%Y")
        attack_text = spawn_dt.strftime("%H:%M")
        activity = {
            "activity_id": activity_id,
            "guild_id": guild.id,
            "boss_name": str(boss_name),
            "activity_date": date_text,
            "attack_time": attack_text,
            "checkin_open": open_dt.strftime("%H:%M"),
            "checkin_close": spawn_dt.strftime("%H:%M"),
            "open_at": open_dt.isoformat(),
            "close_at": spawn_dt.isoformat(),
            "attack_at": spawn_dt.isoformat(),
            "status": "open" if now >= open_dt else "scheduled",
            "created_by": str(bot.user.id if bot.user else "SKYNET"),
            "created_by_name": "SKYNET Auto Attendance",
            "created_at": _attendance_now_iso(),
            "panel_channel_id": int(summary_channel.id),
            "panel_message_id": None,
            "summary_channel_id": int(summary_channel.id),
            "participants": {},
            "source": "autoattendance",
            "source_schedule_key": str(boss_name),
            "source_spawn_ms": spawn_ms,
            "auto_generated": True,
        }
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    db.reference(_attendance_activity_path(guild.id, activity_id)).set,
                    activity,
                ),
                timeout=8,
            )
        except Exception as e:
            print(
                f"⚠️ Auto Attendance Firebase create failed | guild={guild.name} | "
                f"boss={boss_name} | activity={activity_id} | {e}",
                flush=True,
            )
            return False

        if isinstance(activities, dict):
            activities[activity_id] = activity
        print(
            f"⚔️ AUTO ATTENDANCE CREATED | guild={guild.name} | boss={boss_name} | "
            f"open={open_dt.strftime('%H:%M')} | close={spawn_dt.strftime('%H:%M')} | activity={activity_id}",
            flush=True,
        )

    # A prior Discord REST cooldown can leave the activity persisted without its
    # panel message. Retry only the panel message on a later lifecycle tick.
    if not activity.get("panel_message_id"):
        embed = build_raid_activity_embed(activity, [], closed=False)
        view = RaidAttendanceView(activity_id)
        try:
            message = await guarded_channel_send(
                summary_channel,
                context=f"attendance:auto-create:{activity_id}",
                embed=embed,
                view=view,
                background=True,
            )
        except Exception as e:
            print(
                f"⚠️ Auto Attendance panel send failed | guild={guild.name} | "
                f"activity={activity_id} | {e}",
                flush=True,
            )
            return False
        if message is not None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        db.reference(_attendance_activity_path(guild.id, activity_id)).update,
                        {
                            "panel_message_id": int(message.id),
                            "panel_channel_id": int(summary_channel.id),
                        },
                    ),
                    timeout=8,
                )
                activity["panel_message_id"] = int(message.id)
                activity["panel_channel_id"] = int(summary_channel.id)
                try:
                    bot.add_view(view, message_id=int(message.id))
                except Exception as e:
                    print(f"⚠️ Auto Attendance add_view failed | {activity_id}: {e}", flush=True)
            except Exception as e:
                print(
                    f"⚠️ Auto Attendance panel metadata save failed | activity={activity_id} | {e}",
                    flush=True,
                )
                return False

    return True


async def _autoattendance_tick(now: datetime, root: dict) -> None:
    """Evaluate boss_schedule as the single source of truth for Auto Attendance."""
    with schedule_lock:
        schedule_copy = {str(k): dict(v) for k, v in boss_schedule.items()}

    if not schedule_copy:
        return

    for guild in list(bot.guilds):
        if not _autoattendance_enabled_for_guild(guild.id):
            continue

        activities = root.get(str(guild.id), {}) if isinstance(root, dict) else {}
        if not isinstance(activities, dict):
            activities = {}

        for boss_name, schedule in schedule_copy.items():
            if not isinstance(schedule, dict):
                continue
            try:
                await _autoattendance_find_or_create_for_schedule(
                    guild, boss_name, schedule, activities, now
                )
            except Exception as e:
                print(
                    f"⚠️ Auto Attendance evaluation failed | guild={guild.name} | "
                    f"boss={boss_name} | {e}",
                    flush=True,
                )


@tasks.loop(seconds=30)
async def attendance_lifecycle_loop():
    try:
        root = await asyncio.wait_for(asyncio.to_thread(db.reference("raid_attendance").get), timeout=8)
    except Exception as e:
        print(f"⚠️ Attendance lifecycle Firebase read failed: {e}", flush=True)
        return
    if not isinstance(root, dict):
        root = {}
    now = datetime.now(TZ_THAI)

    # Keep the Dashboard/Bot boss schedule authoritative and ensure the two recurring
    # Library Boss rows exist before Auto Attendance evaluates spawn times.
    try:
        await ensure_library_boss_schedule_records()
    except Exception as e:
        print(f"⚠️ Library Boss recurring schedule check failed: {e}", flush=True)

    # Auto Attendance must use the same in-memory boss_schedule loaded from Firebase.
    try:
        await _autoattendance_tick(now, root)
    except Exception as e:
        print(f"⚠️ Auto Attendance tick failed: {e}", flush=True)

    for guild_id, activities in root.items():
        try:
            guild = bot.get_guild(int(guild_id))
        except (TypeError, ValueError):
            guild = None
        if guild is None or not isinstance(activities, dict):
            continue
        for activity_id, activity in activities.items():
            if not isinstance(activity, dict):
                continue
            status = str(activity.get("status") or "scheduled")
            if status == "closed":
                continue
            open_dt = parse_to_thai_datetime(activity.get("open_at"))
            close_dt = parse_to_thai_datetime(activity.get("close_at"))
            if not open_dt or not close_dt:
                continue
            if now >= close_dt:
                await close_raid_activity(guild, str(activity_id), reason="scheduled-close")
            elif now >= open_dt and status != "open":
                try:
                    await asyncio.wait_for(asyncio.to_thread(db.reference(_attendance_activity_path(guild.id, str(activity_id))).update, {"status": "open", "opened_at": _attendance_now_iso()}), timeout=8)
                except Exception as e:
                    print(f"⚠️ Attendance open state update failed: {guild.id}/{activity_id}: {e}", flush=True)


@tasks.loop(minutes=5)
async def attendance_monthly_report_loop():
    now = datetime.now(TZ_THAI)
    if now.day != 1 or now.hour == 0 and now.minute < 5:
        return
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_last = first_this - timedelta(seconds=1)
    month_key = prev_last.strftime("%Y-%m")
    month_label = prev_last.strftime("%B %Y")
    for guild in list(bot.guilds):
        cfg = _attendance_config_snapshot(guild.id)
        summary_id = cfg.get("summary_channel_id")
        channel = guild.get_channel(int(summary_id)) if summary_id else None
        if not isinstance(channel, discord.TextChannel):
            continue
        sent_path = f"monthly_reports/{guild.id}/{month_key}"
        try:
            state = await asyncio.wait_for(asyncio.to_thread(db.reference(sent_path).get), timeout=8)
        except Exception:
            state = None
        if isinstance(state, dict) and state.get("sent_at"):
            continue
        try:
            activities = await asyncio.wait_for(asyncio.to_thread(db.reference(f"raid_attendance/{guild.id}").get), timeout=8)
        except Exception as e:
            print(f"⚠️ Monthly attendance read failed: {guild.name}: {e}", flush=True)
            continue
        events = []
        unique = set()
        total_checkins = 0
        if isinstance(activities, dict):
            for activity_id, activity in activities.items():
                if not isinstance(activity, dict):
                    continue
                created = str(activity.get("created_at") or "")
                attack_at = str(activity.get("attack_at") or created)
                if not attack_at.startswith(month_key):
                    continue
                participants = activity.get("participants") if isinstance(activity.get("participants"), dict) else {}
                checked = [p for p in participants.values() if isinstance(p, dict) and p.get("status") == "checked_in"]
                total_checkins += len(checked)
                for p in checked:
                    unique.add(str(p.get("user_id") or ""))
                events.append({"id": activity_id, "activity": activity, "checked": checked})
        ranking = {}
        for event in events:
            for p in event["checked"]:
                uid = str(p.get("user_id") or "")
                if not uid:
                    continue
                row = ranking.setdefault(uid, {"name": p.get("display_name") or p.get("username") or uid, "count": 0})
                row["count"] += 1
        top = sorted(ranking.values(), key=lambda x: (-x["count"], str(x["name"]).lower()))[:15]
        enabled = get_enabled_discord_notification_languages() or ["th"]
        primary = enabled[0]
        report_text = {
            "th": f"📅 {month_label}\n⚔️ กิจกรรมทั้งหมด: **{len(events)}**\n👥 สมาชิกที่เข้าร่วม: **{len(unique)} คน**\n✅ เช็กชื่อรวม: **{total_checkins} ครั้ง**\n📈 ค่าเฉลี่ยต่อกิจกรรม: **{(total_checkins/len(events) if events else 0):.1f} คน**",
            "en": f"📅 {month_label}\n⚔️ Total raids: **{len(events)}**\n👥 Unique members: **{len(unique)}**\n✅ Total check-ins: **{total_checkins}**\n📈 Average per raid: **{(total_checkins/len(events) if events else 0):.1f}**",
            "ko": f"📅 {month_label}\n⚔️ 전체 레이드: **{len(events)}**\n👥 참여 회원: **{len(unique)}명**\n✅ 총 출석: **{total_checkins}회**\n📈 레이드당 평균: **{(total_checkins/len(events) if events else 0):.1f}명**",
        }
        titles = {"th": "📊 Boss Raid Attendance — รายงานประจำเดือน", "en": "📊 Boss Raid Attendance — Monthly Report", "ko": "📊 보스 레이드 출석 — 월간 보고서"}
        embed = discord.Embed(title=titles[primary], color=discord.Color.gold(), timestamp=now)
        for lang in enabled:
            embed.add_field(name={"th":"🇹🇭 ไทย","en":"🇺🇸 English","ko":"🇰🇷 한국어"}[lang], value=report_text[lang], inline=False)
        rank_lines = [f"{idx}. {r['name']} — **{r['count']}**" for idx, r in enumerate(top, 1)] or ["-"]
        embed.add_field(name={"th":"🏆 อันดับการเข้าร่วม","en":"🏆 Attendance Ranking","ko":"🏆 출석 순위"}[primary], value="\n".join(rank_lines), inline=False)
        try:
            result = await guarded_channel_send(channel, context=f"attendance:monthly:{month_key}", embed=embed, background=True)
            if result is not None:
                await asyncio.wait_for(asyncio.to_thread(db.reference(sent_path).set, {
                    "guild_id": guild.id, "month": month_key, "month_label": month_label,
                    "activity_count": len(events), "unique_members": len(unique),
                    "total_checkins": total_checkins, "ranking": top, "sent_at": _attendance_now_iso()
                }), timeout=8)
                print(f"📊 Monthly Attendance report sent | guild={guild.name} | month={month_key}", flush=True)
        except Exception as e:
            print(f"⚠️ Monthly Attendance report send failed | guild={guild.name}: {e}", flush=True)


@bot.tree.command(name="code", description="ประกาศ Code และ Drop Item ของบอส")
@app_commands.describe(
    boss_name="ชื่อบอส",
    code="โค้ดสำหรับเช็คชื่อ (Code)",
    drop_item="ไอเทมที่ดรอป (Drop Item)"
)
@has_allowed_role()
async def code_command(interaction: discord.Interaction, boss_name: str, code: str, drop_item: str):
    """Original /attendance announcement flow, moved to /code so /attendance is attendance-only."""
    await _safe_interaction_ack(interaction, ephemeral=False)

    await refresh_discord_notification_languages()
    enabled = get_enabled_discord_notification_languages() or ["th"]
    primary = enabled[0]
    title_map = {
        "th": "📢 ประกาศ Code / Item ของบอส",
        "en": "📢 Boss Code / Item Announcement",
        "ko": "📢 보스 코드 / 아이템 공지",
    }
    embed = discord.Embed(title=title_map[primary], color=discord.Color.green(), timestamp=datetime.now(TZ_THAI))
    labels = {
        "th": ("👾 ชื่อบอส", "🔑 โค้ด (Code)", "🎁 ไอเทมดรอป"),
        "en": ("👾 Boss", "🔑 Code", "🎁 Drop Item"),
        "ko": ("👾 보스", "🔑 코드", "🎁 드롭 아이템"),
    }
    for lang in enabled:
        prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
        lb, lc, ld = labels[lang]
        embed.add_field(name=f"{prefix} • {lb}", value=f"`{boss_name}`", inline=True)
        embed.add_field(name=lc, value=f"**{code}**", inline=True)
        embed.add_field(name=ld, value=f"`{drop_item}`", inline=False)
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}")

    await guarded_interaction_followup_send(
        interaction, "interaction-followup", content="✅ ส่งประกาศ Code / Item สำเร็จ!", embed=embed
    )

    canonical_name = get_boss_canonical_name(boss_name)
    spoken_name = get_boss_pronunciation(canonical_name)
    spoken_th = f"ประกาศข้อมูลบอส {spoken_name} โค้ดคือ {code} ไอเทมที่ดรอปคือ {drop_item} ค่ะ"
    spoken_en = f"Boss {boss_name}. The code is {code}. Drop item is {drop_item}."
    spoken_ko = f"보스 {boss_name} 정보입니다. 코드는 {code} 이며, 드롭 아이템은 {drop_item} 입니다."
    asyncio.create_task(speak_in_guild(interaction.guild, text_th=spoken_th, text_en=spoken_en, text_ko=spoken_ko))

    if interaction.guild:
        attendance_channel = discord.utils.get(interaction.guild.text_channels, name="boss-attendance")
        if attendance_channel:
            await refresh_discord_notification_languages()
            enabled = get_enabled_discord_notification_languages() or ["th"]
            primary = enabled[0]
            log_embed = discord.Embed(
                title=f"📝 Audit Log: {_translate_audit_action('ประกาศ Code / Item ของบอส', primary)}",
                color=discord.Color.green(),
                timestamp=datetime.now(TZ_THAI),
            )
            labels = {
                "th": ("ผู้ประกาศ", "ชื่อบอส", "โค้ด (Code)", "ไอเทมดรอป"),
                "en": ("Announcer", "Boss", "Code", "Drop Item"),
                "ko": ("공지자", "보스", "코드", "드롭 아이템"),
            }
            for lang in enabled:
                prefix = {"th": "🇹🇭 ไทย", "en": "🇺🇸 English", "ko": "🇰🇷 한국어"}[lang]
                la, lb, lc, ld = labels[lang]
                log_embed.add_field(name=f"{prefix} • 👤 {la}", value=f"{interaction.user.mention} (`{interaction.user.name}`)", inline=False)
                log_embed.add_field(name=f"👾 {lb}", value=f"`{boss_name}`", inline=True)
                log_embed.add_field(name=f"🔑 {lc}", value=f"**{code}**", inline=True)
                log_embed.add_field(name=f"🎁 {ld}", value=f"`{drop_item}`", inline=False)
            log_embed.set_footer(text=f"User ID: {interaction.user.id}")
            try:
                await guarded_channel_send(attendance_channel, context="audit:boss-code", embed=log_embed)
            except Exception as e:
                print(f"❌ ส่ง Audit Log ใน boss-attendance ไม่สำเร็จ: {e}")


@bot.tree.command(name="autoattendance", description="เปิดหรือปิดระบบ Auto Attendance ก่อนบอสเกิด 30 นาที")
@app_commands.describe(enabled="True = เปิด Auto Attendance, False = ปิด Auto Attendance")
async def autoattendance_command(interaction: discord.Interaction, enabled: bool):
    """Admin/Server Owner control for automatic Boss Raid Attendance."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await _safe_interaction_send_message(
            interaction,
            "❌ คำสั่งนี้ใช้ได้เฉพาะภายใน Server เท่านั้น",
            ephemeral=True,
        )
        return
    if not is_guild_admin_or_owner(interaction.user):
        await _safe_interaction_send_message(
            interaction,
            "❌ การเปิด/ปิด Auto Attendance อนุญาตเฉพาะ Admin หรือ Server Owner",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    guild_key = str(guild.id)
    with schedule_lock:
        current = dict(attendance_config.get(guild_key, {}) or {})

    if not current.get("summary_channel_id"):
        await _safe_interaction_send_message(
            interaction,
            "❌ ยังไม่ได้ตั้งห้องสรุป Attendance กรุณาใช้ `/set-notification` → `ตั้งห้องสรุป Attendance` ก่อน",
            ephemeral=True,
        )
        return

    current.update({
        "guild_id": guild.id,
        "autoattendance_enabled": bool(enabled),
        "autoattendance_updated_by": str(interaction.user.id),
        "autoattendance_updated_at": _attendance_now_iso(),
    })
    with schedule_lock:
        attendance_config[guild_key] = current

    await save_attendance_config()

    state_text = "เปิดใช้งาน" if enabled else "ปิดใช้งาน"
    await _safe_interaction_send_message(
        interaction,
        (
            f"✅ Auto Attendance **{state_text}** แล้ว\n"
            f"📚 ระบบจะอ้างอิงเวลาเกิดจาก Boss Schedule เดียวกับ Dashboard\n"
            f"⏰ เปิดเช็คชื่อก่อนบอสเกิด **30 นาที** และปิดเมื่อถึงเวลาเกิด"
        ),
        ephemeral=True,
    )
    print(
        f"⚙️ Auto Attendance {'ENABLED' if enabled else 'DISABLED'} | "
        f"guild={guild.name} | by={interaction.user} | "
        f"summary_channel={current.get('summary_channel_id')}",
        flush=True,
    )


@bot.tree.command(name="attendance", description="สร้างและจัดการกิจกรรมเช็คชื่อการโจมตีบอส")
@app_commands.describe(boss_name="ชื่อบอสสำหรับสร้างกิจกรรม Attendance")
async def attendance_command(interaction: discord.Interaction, boss_name: str):
    """Create a Boss Raid Attendance activity; creation is Admin/Server Owner only."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not is_guild_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ การสร้างกิจกรรม Attendance อนุญาตเฉพาะ Admin หรือ Server Owner", ephemeral=True
        )
        return
    try:
        await interaction.response.send_modal(RaidAttendanceCreateModal(boss_name))
    except Exception as e:
        print(f"❌ เปิด Activity creation modal ไม่สำเร็จ: {e}", flush=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ ไม่สามารถเปิดแบบฟอร์มสร้างกิจกรรมได้ กรุณาลองใหม่", ephemeral=True)

# ==========================================
# 🚀 11. Run Bot Entry Point
# ==========================================
GATEWAY_PREFLIGHT_ENABLED = os.environ.get("ENABLE_GATEWAY_PREFLIGHT", "0").strip().lower() in {"1", "true", "yes", "on"}


async def _probe_gateway_session_start_limit(token: str):
    """Query Discord Gateway session-start metadata and preserve 429 details.

    Discord recommends honoring Retry-After and rate-limit headers rather than hard-coding
    retry timings. This preflight never performs IDENTIFY when the endpoint itself is limited.
    """
    url = "https://discord.com/api/v10/gateway/bot"
    headers = {"Authorization": f"Bot {token}", "User-Agent": "SKYNET/1.0"}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json(content_type=None)
                response_headers = {str(k): str(v) for k, v in resp.headers.items()}
                if resp.status == 429:
                    retry_after = 0.0
                    raw_retry = response_headers.get("Retry-After") or response_headers.get("retry-after")
                    if raw_retry is None and isinstance(data, dict):
                        raw_retry = data.get("retry_after")
                    try:
                        retry_after = max(0.0, float(raw_retry or 0))
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    scope = response_headers.get("X-RateLimit-Scope", "")
                    is_global = str(response_headers.get("X-RateLimit-Global", "")).lower() == "true"
                    print(
                        f"⚠️ Gateway preflight HTTP 429 | retry_after={retry_after:.1f}s "
                        f"| global={is_global} | scope={scope or '-'} | data={data}",
                        flush=True,
                    )
                    if retry_after > 0:
                        # Share the same cooldown state with REST notifications so the
                        # rest of the process also stops making non-essential API calls.
                        global discord_rest_rate_limited_until
                        discord_rest_rate_limited_until = max(
                            discord_rest_rate_limited_until, time.monotonic() + retry_after
                        )
                    return {
                        "status": 429,
                        "retry_after": retry_after,
                        "global": is_global,
                        "scope": scope,
                    }
                if resp.status != 200:
                    print(f"⚠️ Gateway preflight HTTP {resp.status}: {data}", flush=True)
                    return {"status": resp.status, "retry_after": 0.0, "global": False, "scope": ""}

                limit = data.get("session_start_limit") or {}
                remaining = int(limit.get("remaining", -1))
                total = int(limit.get("total", -1))
                reset_after_ms = int(limit.get("reset_after", 0) or 0)
                max_concurrency = int(limit.get("max_concurrency", 0) or 0)
                print(
                    f"🔎 Gateway session limit | remaining={remaining}/{total} "
                    f"reset_after={reset_after_ms}ms max_concurrency={max_concurrency}",
                    flush=True,
                )
                return {
                    "status": 200,
                    "remaining": remaining,
                    "total": total,
                    "reset_after_ms": reset_after_ms,
                    "max_concurrency": max_concurrency,
                }
    except Exception as e:
        print(f"⚠️ Gateway preflight failed: {e}", flush=True)
        return {"status": None, "error": str(e), "retry_after": 0.0}


async def _gateway_rate_limit_sleep(reason: str, seconds: float, attempt: int):
    """Wait without making Discord requests, while exposing a local retry countdown.

    Important: this is the bot's next-retry timer, not a guaranteed Discord unblock timer.
    An exact remote unblock ETA exists only when Discord provides Retry-After/retry_after.
    """
    delay = min(max(float(seconds or 0), 15.0), 3600.0)
    deadline = time.monotonic() + delay
    print(
        f"⏸️ Gateway rate-limit cooldown | {reason} | next_retry_in={delay:.0f}s "
        f"({delay/60:.1f}m) | attempt={attempt}",
        flush=True,
    )
    last_reported = None
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            print("✅ Gateway retry timer reached; attempting Discord connection again", flush=True)
            return
        bucket = int(remaining // 60) if remaining >= 60 else int(remaining)
        if bucket != last_reported:
            if remaining >= 60:
                print(f"⏳ Gateway retry countdown: ~{remaining/60:.1f}m remaining", flush=True)
            else:
                print(f"⏳ Gateway retry countdown: ~{remaining:.0f}s remaining", flush=True)
            last_reported = bucket
        await asyncio.sleep(min(60.0, remaining))


def _gateway_429_diagnostics(exc: Exception) -> dict:
    """Extract safe diagnostics from discord.py HTTPException without making a new request."""
    info = {
        "retry_after": 0.0,
        "header_retry_after": 0.0,
        "body_retry_after": 0.0,
        "scope": "",
        "global": False,
        "reset_after": 0.0,
        "reset": "",
        "via": "",
        "content_type": "",
        "server": "",
        "cf_ray": "",
        "date": "",
        "body_preview": "",
    }

    try:
        value = float(getattr(exc, "retry_after", 0) or 0)
        if value > 0:
            info["retry_after"] = value
    except (TypeError, ValueError):
        pass

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        def _header(*names):
            for name in names:
                try:
                    value = headers.get(name)
                except Exception:
                    value = None
                if value is not None and str(value).strip():
                    return str(value).strip()
            return ""

        raw = _header("Retry-After", "retry-after")
        try:
            info["header_retry_after"] = max(0.0, float(raw)) if raw else 0.0
        except (TypeError, ValueError):
            pass
        if info["retry_after"] <= 0 and info["header_retry_after"] > 0:
            info["retry_after"] = info["header_retry_after"]

        raw = _header("X-RateLimit-Reset-After", "X-Ratelimit-Reset-After")
        try:
            info["reset_after"] = max(0.0, float(raw)) if raw else 0.0
        except (TypeError, ValueError):
            pass
        info["reset"] = _header("X-RateLimit-Reset", "X-Ratelimit-Reset")
        info["scope"] = _header("X-RateLimit-Scope", "X-Ratelimit-Scope")
        info["global"] = _header("X-RateLimit-Global", "X-Ratelimit-Global").lower() == "true"
        info["via"] = _header("Via")
        info["content_type"] = _header("Content-Type", "content-type")
        info["server"] = _header("Server", "server")
        info["cf_ray"] = _header("CF-RAY", "cf-ray")
        info["date"] = _header("Date", "date")

    data = getattr(exc, "text", None)
    if data is None:
        # discord.py's HTTPException exposes response/data, but attribute names vary by version.
        data = getattr(exc, "data", None)
    if isinstance(data, dict):
        raw = data.get("retry_after")
        try:
            info["body_retry_after"] = max(0.0, float(raw)) if raw is not None else 0.0
        except (TypeError, ValueError):
            pass
        if info["retry_after"] <= 0 and info["body_retry_after"] > 0:
            info["retry_after"] = info["body_retry_after"]
        preview = data.get("message") or data
    else:
        preview = data
    if preview is not None:
        try:
            info["body_preview"] = str(preview).replace("\n", " ")[:180]
        except Exception:
            info["body_preview"] = ""
    return info


async def _prepare_gateway_http_transport():
    """Ensure discord.py HTTP transport can create a fresh ClientSession.

    discord.py 2.6.x creates an aiohttp.ClientSession in HTTPClient.static_login().
    When that session is closed, its owned TCP connector can also be closed. Reusing
    the closed connector on the next static_login() can surface ``RuntimeError:
    Session is closed`` even though the Bot itself reports is_closed() == False.
    This helper only resets local HTTP transport state; it does not issue any Discord
    request and therefore does not affect Discord rate limits.
    """
    http_client = getattr(bot, "http", None)
    if http_client is None:
        return

    # Clear a previously closed aiohttp session first.
    try:
        clear_http = getattr(http_client, "clear", None)
        if clear_http is not None:
            clear_http()
    except Exception as clear_exc:
        print(f"⚠️ Failed to clear Discord HTTP session before startup: {clear_exc!r}", flush=True)

    # If the connector was owned by discord.py and has already been closed,
    # discard it so HTTPClient.static_login() creates a brand-new connector.
    connector = getattr(http_client, "connector", None)
    if connector is not None and getattr(connector, "closed", False):
        try:
            http_client.connector = discord.utils.MISSING
            print("🧹 Reset closed Discord HTTP connector before Gateway startup", flush=True)
        except Exception as connector_exc:
            print(f"⚠️ Failed to reset Discord HTTP connector: {connector_exc!r}", flush=True)


async def _cleanup_failed_gateway_transport(reason: str):
    """Release a failed Gateway HTTP session and its owned connector safely.

    This is local transport cleanup only. It does not perform any retry request and
    does not bypass Discord rate limits. The connector is reset to discord.py's
    MISSING sentinel so the next login gets a fresh TCP connector as well as a fresh
    ClientSession.
    """
    http_client = getattr(bot, "http", None)
    if http_client is None:
        return

    try:
        close_http = getattr(http_client, "close", None)
        if close_http is not None:
            await close_http()
    except Exception as close_exc:
        print(f"⚠️ Failed to close Discord HTTP transport after {reason}: {close_exc!r}", flush=True)

    # The discord.py HTTPClient connector is created lazily by static_login().
    # After HTTPClient.close(), that connector may itself be closed because it is
    # owned by the ClientSession. Reset it so the next static_login() does not reuse
    # a closed connector and immediately raise ``Session is closed``.
    try:
        http_client.connector = discord.utils.MISSING
    except Exception as connector_exc:
        print(f"⚠️ Failed to reset Discord HTTP connector after {reason}: {connector_exc!r}", flush=True)

    try:
        clear_http = getattr(http_client, "clear", None)
        if clear_http is not None:
            clear_http()
    except Exception as clear_exc:
        print(f"⚠️ Failed to clear Discord HTTP session after {reason}: {clear_exc!r}", flush=True)

    print(
        f"🧹 Cleaned Discord HTTP session + connector after {reason}; client lifecycle reusable",
        flush=True,
    )


def validate_runtime_integrity():
    """Fail fast before Gateway startup if critical functions/commands were accidentally dropped."""
    required_funcs = [
        "boss_autocomplete", "get_notification_mentions", "save_boss_notification_flags",
        "check_bf_notifications", "check_library_boss_notifications", "check_boss_notifications",
        "update_live_embed", "check_auto_disconnect", "boss_time_slash", "generate_boss_time_summary",
    ]
    missing = [name for name in required_funcs if not callable(globals().get(name))]
    if missing:
        raise RuntimeError("V46 integrity check failed; missing functions: " + ", ".join(missing))
    direct = [cmd.name for cmd in bot.tree.get_commands() if isinstance(cmd, app_commands.Command)]
    if len(direct) != 18:
        raise RuntimeError(f"V79 integrity check failed; expected 19 direct slash commands, found {len(direct)}")
    group = next((cmd for cmd in bot.tree.get_commands() if isinstance(cmd, app_commands.Group) and cmd.name == "add"), None)
    if group is None or not any(sub.name == "boss" for sub in group.commands):
        raise RuntimeError("V79 integrity check failed; /add boss subcommand missing")
    print("✅ V93 integrity check passed | 19 direct + /add boss = 20 command paths", flush=True)


async def run_bot_with_backoff(token: str):
    """Single Gateway controller with safe HTTP/session lifecycle recovery and 429 diagnostics."""
    global is_bot_ready
    if getattr(run_bot_with_backoff, "_active", False):
        print("⚠️ Discord runner already active — skip duplicate session start", flush=True)
        return

    run_bot_with_backoff._active = True
    gateway_failure_attempt = 0
    try:
        while True:
            await _prepare_gateway_http_transport()

            if GATEWAY_PREFLIGHT_ENABLED:
                limit_info = await _probe_gateway_session_start_limit(token)
                if limit_info.get("status") == 429:
                    gateway_failure_attempt += 1
                    retry_after = float(limit_info.get("retry_after") or 0)
                    fallback = min(900.0 * (2 ** max(0, gateway_failure_attempt - 1)), 3600.0)
                    delay = max(retry_after + 2.0, fallback if retry_after <= 0 else retry_after + 2.0)
                    await _gateway_rate_limit_sleep("preflight HTTP 429", delay, gateway_failure_attempt)
                    continue
                if limit_info.get("status") != 200:
                    gateway_failure_attempt += 1
                    delay = min(30.0 * (2 ** max(0, gateway_failure_attempt - 1)), 900.0)
                    await _gateway_rate_limit_sleep("preflight unavailable", delay, gateway_failure_attempt)
                    continue
                remaining = int(limit_info.get("remaining", -1))
                if remaining == 0:
                    gateway_failure_attempt += 1
                    reset_after = max(0.0, float(limit_info.get("reset_after_ms", 0)) / 1000.0)
                    delay = max(60.0, reset_after + 2.0)
                    await _gateway_rate_limit_sleep("IDENTIFY quota exhausted", delay, gateway_failure_attempt)
                    continue
            else:
                print("ℹ️ Gateway preflight disabled (default) — starting discord.py Gateway directly", flush=True)

            print("🔌 กำลังเชื่อมต่อ Discord Gateway...", flush=True)
            is_bot_ready = False

            try:
                await bot.start(token, reconnect=True)

            except discord.HTTPException as e:
                is_bot_ready = False
                status = getattr(e, "status", None)
                if status == 429:
                    gateway_failure_attempt += 1
                    diag = _gateway_429_diagnostics(e)
                    retry_after = float(diag.get("retry_after") or 0)

                    # If Discord exposes a concrete Retry-After, honor it. If it exposes
                    # only Cloudflare-style 429 text/headers, no public endpoint exists
                    # that can reveal the exact remaining ban duration without another
                    # request, and making probe requests would worsen the restriction.
                    if retry_after > 0:
                        delay = retry_after + 2.0
                        timing_source = "Discord Retry-After"
                    elif diag.get("reset_after", 0) > 0:
                        delay = float(diag["reset_after"]) + 2.0
                        timing_source = "X-RateLimit-Reset-After"
                    else:
                        delay = min(900.0 * (2 ** max(0, gateway_failure_attempt - 1)), 3600.0)
                        timing_source = "local fallback (Discord gave no usable timer)"

                    print(
                        "🛑 Discord Gateway startup HTTP 429 | "
                        f"retry_after={retry_after:.3f}s | "
                        f"scope={diag.get('scope') or '-'} | "
                        f"global={bool(diag.get('global'))} | "
                        f"reset_after={float(diag.get('reset_after') or 0):.3f}s | "
                        f"timing_source={timing_source}",
                        flush=True,
                    )
                    header_bits = []
                    for key in ("via", "server", "cf_ray", "date", "content_type"):
                        value = diag.get(key)
                        if value:
                            header_bits.append(f"{key}={value}")
                    if header_bits:
                        print("🔎 Discord 429 headers | " + " | ".join(header_bits), flush=True)
                    if diag.get("body_preview"):
                        print(f"🔎 Discord 429 body | {diag['body_preview']}", flush=True)

                    await _cleanup_failed_gateway_transport("startup 429")
                    await _gateway_rate_limit_sleep(
                        f"Gateway startup 429 ({timing_source})",
                        delay,
                        gateway_failure_attempt,
                    )
                    continue

                print(f"❌ Discord HTTP error status={status}: {e}", flush=True)
                gateway_failure_attempt += 1
                await _cleanup_failed_gateway_transport("startup HTTP error")
                delay = min(30.0 * (2 ** max(0, gateway_failure_attempt - 1)), 900.0)
                await _gateway_rate_limit_sleep("Gateway HTTP error", delay, gateway_failure_attempt)
                continue

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                is_bot_ready = False
                gateway_failure_attempt += 1
                print(f"⚠️ Discord connection error: {e}", flush=True)
                await _cleanup_failed_gateway_transport("connection error")
                delay = min(30.0 * (2 ** max(0, gateway_failure_attempt - 1)), 900.0)
                await _gateway_rate_limit_sleep("Gateway connection error", delay, gateway_failure_attempt)
                continue

            except RuntimeError as e:
                is_bot_ready = False
                text = str(e)
                if "Session is closed" in text or "Client is closed" in text:
                    gateway_failure_attempt += 1
                    print(f"⚠️ Discord local session state invalid: {e}", flush=True)
                    await _cleanup_failed_gateway_transport("closed-session error")
                    delay = min(60.0 * (2 ** max(0, gateway_failure_attempt - 1)), 900.0)
                    await _gateway_rate_limit_sleep("local Gateway session recovered", delay, gateway_failure_attempt)
                    continue
                await _cleanup_failed_gateway_transport("unhandled runtime error")
                raise

            # If start() returns after a real connected session ended, allow discord.py
            # to settle before reconnecting. A successful session clears the startup
            # failure counter so a later independent outage starts at the base backoff.
            is_bot_ready = False
            gateway_failure_attempt = 0
            print("⚠️ Discord Gateway session ended — re-checking Gateway after 15s", flush=True)
            await asyncio.sleep(15)

    finally:
        run_bot_with_backoff._active = False


# -----------------------------------------------------------------------------
# V38 PATCH INTEGRITY CHECK
# Keep this check deliberately narrow: it only verifies that the handlers and
# notification tasks required by the existing bot architecture still exist.
# It does not modify runtime behavior or feature logic.
# -----------------------------------------------------------------------------
REQUIRED_PATCH_FUNCTIONS = (
    "boss_autocomplete",
    "get_notification_mentions",
    "save_boss_notification_flags",
    "check_bf_notifications",
    "check_library_boss_notifications",
    "check_boss_notifications",
    "update_live_embed",
    "check_auto_disconnect",
)

EXPECTED_SLASH_COMMANDS = {
    "add boss",
    "attendance",
    "autoattendance",
    "code",
    "delboss",
    "disconnect",
    "join",
    "kill",
    "leave",
    "notice",
    "notify",
    "panel",
    "ppl",
    "setlive",
    "set-notification",
    "setvoice",
    "status",
    "time",
    "tts",
    "vip",
}


def _collect_registered_command_paths():
    paths = set()
    try:
        for command in bot.tree.get_commands():
            if isinstance(command, app_commands.Group):
                children = getattr(command, "commands", []) or []
                if children:
                    for child in children:
                        paths.add(f"{command.name} {child.name}")
                else:
                    paths.add(command.name)
            else:
                paths.add(command.name)
    except Exception as exc:
        print(f"⚠️ V38 command integrity check skipped: {exc!r}")
    return paths


def _run_v38_integrity_check():
    missing = [name for name in REQUIRED_PATCH_FUNCTIONS if name not in globals()]
    if missing:
        raise RuntimeError(
            "V38 integrity failure: required functions missing: " + ", ".join(missing)
        )

    command_paths = _collect_registered_command_paths()
    if command_paths:
        missing_commands = sorted(EXPECTED_SLASH_COMMANDS - command_paths)
        unexpected_commands = sorted(command_paths - EXPECTED_SLASH_COMMANDS)
        if missing_commands or unexpected_commands:
            raise RuntimeError(
                "V38 integrity failure: command set mismatch | "
                f"missing={missing_commands} unexpected={unexpected_commands} "
                f"actual={sorted(command_paths)}"
            )
        print(
            f"✅ V38 integrity check passed | functions={len(REQUIRED_PATCH_FUNCTIONS)} "
            f"slash_commands={len(command_paths)}"
        , flush=True)
    else:
        raise RuntimeError("V38 integrity failure: no slash commands registered")


_run_v38_integrity_check()



def _run_v47_command_integrity_check():
    required = [
        "boss_autocomplete",
        "generate_boss_time_summary",
        "get_notification_mentions",
        "save_boss_notification_flags",
        "check_bf_notifications",
        "check_library_boss_notifications",
        "check_boss_notifications",
        "update_live_embed",
        "check_auto_disconnect",
        "boss_time_slash",
        "add_boss",
    ]
    missing = [name for name in required if name not in globals()]
    direct_names = []
    try:
        direct_names = sorted(
            cmd.name for cmd in bot.tree.get_commands()
            if isinstance(cmd, app_commands.Command)
        )
    except Exception:
        direct_names = []
    if missing:
        print(f"❌ V50 integrity failure | missing symbols: {missing}", flush=True)
        raise RuntimeError(f"V50 integrity failure: {missing}")
    if len(direct_names) != 19:
        print(f"⚠️ V93 command count unexpected at import time | direct={len(direct_names)} | commands={direct_names}", flush=True)
    else:
        print(
            f"✅ V93 command integrity check passed | 19 direct + /add boss = 20 command paths | "
            f"direct={', '.join(direct_names)}",
            flush=True,
        )


_run_v47_command_integrity_check()

if __name__ == "__main__":
    if SKYNET_RUNTIME_ROLE == "web":
        keep_alive()
        print("🌐 SKYNET web runtime active | Discord Gateway/REST disabled", flush=True)
        try:
            asyncio.run(asyncio.Event().wait())
        except KeyboardInterrupt:
            print("🛑 SKYNET web runtime stopped", flush=True)
    else:
        TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
        if not TOKEN:
            raise RuntimeError("SKYNET_RUNTIME_ROLE=bot requires DISCORD_TOKEN")
        try:
            asyncio.run(run_bot_with_backoff(TOKEN))
        except KeyboardInterrupt:
            print("🛑 หยุดบอทแล้ว", flush=True)
