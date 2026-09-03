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

NOTICE_BF_PATCH_VERSION = "V44_ADD_BOSS_INTERACTION_SAFE"
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Boss Timer Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;600&display=swap" rel="stylesheet">
    
    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-database-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.8.0/firebase-auth-compat.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <style>
        body {
            background-color: #0f172a;
            color: #f8fafc;
            font-family: 'Kanit', sans-serif;
        }
        .card {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
        }
        .form-label {
            color: #ffffff !important;
            font-weight: 500;
        }
        .form-control, .form-select {
            background-color: #0f172a !important;
            border: 1px solid #334155;
            color: #ffffff !important;
        }
        .form-control::placeholder {
            color: #64748b;
        }
        .form-control:focus, .form-select:focus {
            background-color: #0f172a !important;
            color: #ffffff !important;
            border-color: #3b82f6;
            box-shadow: none;
        }
        .table {
            color: #f8fafc;
        }
        .table-dark {
            --bs-table-bg: #1e293b;
            --bs-table-hover-bg: #334155;
        }
        .status-badge {
            font-size: 0.85rem;
            padding: 5px 10px;
            border-radius: 15px;
        }
        .settings-bar {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 12px 20px;
        }
        footer, footer small {
            color: #ffffff !important;
        }
        .modal-content {
            background-color: #1e293b;
            color: #f8fafc;
            border: 1px solid #334155;
        }
        .nav-tabs .nav-link {
            color: #94a3b8;
            border: none;
        }
        .nav-tabs .nav-link.active {
            background-color: transparent;
            color: #38bdf8;
            border-bottom: 3px solid #38bdf8;
            font-weight: 600;
        }
    </style>
<style>
.skynet-logo{width:52px;height:52px;object-fit:cover;border-radius:12px;border:1px solid rgba(255,255,255,.25);box-shadow:0 4px 16px rgba(0,0,0,.35)}
@media(max-width:576px){.skynet-logo{width:42px;height:42px}}
</style>
</head>
<body>
    <div id="authContainer" class="container py-5" style="max-width: 450px;">
        <div class="card p-4 shadow-lg">
            
            <div class="d-flex justify-content-end mb-2">
                <select id="authLangSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeLanguage(this.value)">
                    <option value="th">🇹🇭 ไทย (TH)</option>
                    <option value="en">🇺🇸 English (EN)</option>
                    <option value="ko">🇰🇷 한국어 (KO)</option>
                </select>
            </div>

            <h3 class="text-center text-warning mb-4" data-i18n="authTitle">⚔️ Boss Timer Access</h3>
            
            <ul class="nav nav-tabs nav-justified mb-3" id="authTabs" role="tablist">
                <li class="nav-item">
                    <button class="nav-link active" id="login-tab" data-bs-toggle="tab" data-bs-target="#loginPane" type="button" data-i18n="tabLogin">เข้าสู่ระบบ</button>
                </li>
                <li class="nav-item">
                    <button class="nav-link" id="register-tab" data-bs-toggle="tab" data-bs-target="#registerPane" type="button" data-i18n="tabRegister">ลงทะเบียน</button>
                </li>
            </ul>

            <div class="tab-content">
                <div class="tab-pane fade show active" id="loginPane" role="tabpanel">
                    <form id="loginForm" autocomplete="off">
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelUsername">ชื่อผู้ใช้</label>
                            <input type="text" id="loginUser" class="form-control" placeholder="กรอกชื่อผู้ใช้" data-i18n-ph="phLoginUser" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelPassword">รหัสผ่าน</label>
                            <input type="password" id="loginPass" class="form-control" placeholder="กรอกรหัสผ่าน" data-i18n-ph="phLoginPass" required>
                        </div>
                        <div class="form-check mb-3">
                            <input class="form-check-input" type="checkbox" id="rememberMe">
                            <label class="form-check-label text-white" for="rememberMe" data-i18n="rememberMe">จำชื่อผู้ใช้</label>
                        </div>
                        <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnLogin">🔑 เข้าสู่ระบบ</button>
                        <div class="alert alert-dark border-danger mt-3 mb-0 py-2 small">
                            🛡️ <strong>Admin Login</strong> — ใช้ Username/Password ของบัญชี Admin ที่สร้างใน Firebase Authentication<br>
                            <span class="text-white-50">บัญชี Admin ต้องมี users/&lt;UID&gt; → role = admin และ status = approved</span>
                        </div>
                    </form>
                </div>

                <div class="tab-pane fade" id="registerPane" role="tabpanel">
                    <form id="registerForm" autocomplete="off">
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelUsername">ชื่อผู้ใช้</label>
                            <input type="text" id="regUser" class="form-control" placeholder="ตั้งชื่อผู้ใช้" data-i18n-ph="phRegUser" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelPassword">รหัสผ่าน</label>
                            <input type="password" id="regPass" class="form-control" placeholder="ตั้งรหัสผ่าน" data-i18n-ph="phRegPass" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelRegCode">โค้ดสำหรับสมัคร</label>
                            <input type="password" id="regCode" class="form-control" placeholder="กรอกโค้ดสำหรับสมัคร" data-i18n-ph="phRegCode" required>
                        </div>
                        <button type="submit" class="btn btn-success w-100 fw-bold" data-i18n="btnRegister">📝 ลงทะเบียน</button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <div id="mainDashboard" class="container py-4" style="display: none;">
        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
            <h2 data-i18n="title" class="d-flex align-items-center gap-2">
                <img src="https://img2.pic.in.th/85cf3cd7-b2ad-4a9d-a2a2-d94ec53dd4c3.jpeg" alt="SKYNET Logo" class="skynet-logo" onerror="this.style.display='none'">
                <span>Boss Timer Dashboard</span>
            </h2>
            <div class="d-flex align-items-center gap-2">
                <span class="badge bg-success status-badge" id="syncStatus" data-i18n="online">🟢 Realtime Sync Active</span>
                <span class="badge bg-info text-dark status-badge" id="userBadge">👤 User</span>
                <button id="adminPanelMenuBtn" onclick="scrollToAdminPanel()" data-admin-only class="btn btn-outline-danger btn-sm" style="display:none;">🛡️ Admin Panel</button>
                <button onclick="openChangeCodeModal()" data-admin-only class="btn btn-outline-warning btn-sm" data-i18n="btnConfigCode">🔑 เปลี่ยนโค้ดสมัคร</button>
                <button onclick="logout()" class="btn btn-outline-danger btn-sm" data-i18n="btnLogout">🚪 ออกจากระบบ</button>
            </div>
        </div>

        <div class="settings-bar mb-4 d-flex flex-wrap align-items-center justify-content-between gap-3">
            <div class="d-flex flex-wrap align-items-center gap-3">
                <div class="d-flex align-items-center gap-2">
                    <label class="form-label mb-0 text-nowrap" data-i18n="labelLanguage">🌐 ภาษา:</label>
                    <select id="langSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeLanguage(this.value)">
                        <option value="th">🇹🇭 ไทย (TH)</option>
                        <option value="en">🇺🇸 English (EN)</option>
                        <option value="ko">🇰🇷 한국어 (KO)</option>
                    </select>
                </div>
                <div class="d-flex align-items-center gap-2">
                    <label class="form-label mb-0 text-nowrap" data-i18n="labelTimezone">🌍 เขตเวลา:</label>
                    <select id="tzSelect" class="form-select form-select-sm" style="width: auto;" onchange="changeTimezone(this.value)">
                        <option value="auto" data-i18n="tzAuto">💻 อัตโนมัติ (ตามเครื่อง)</option>
                        <option value="UTC">🌐 UTC (Universal Time)</option>
                        <option value="Asia/Bangkok">🇹🇭 Bangkok, Jakarta, Hanoi (UTC+7)</option>
                        <option value="Asia/Singapore">🇸🇬 Singapore, KL, Manila (UTC+8)</option>
                        <option value="Asia/Hong_Kong">🇭🇰 Hong Kong, Beijing, Taipei (UTC+8)</option>
                        <option value="Asia/Tokyo">🇯🇵 Tokyo, Seoul (UTC+9)</option>
                        <option value="Europe/London">🇬🇧 London, Dublin (UTC+0/+1)</option>
                        <option value="America/New_York">🇺🇸 New York, Toronto (EST)</option>
                    </select>
                </div>
                <div class="d-flex align-items-center gap-2">
                    <span class="badge bg-dark border border-secondary text-info px-3 py-2 fs-6" id="liveClockDisplay" style="letter-spacing: 1px;">00:00:00</span>
                </div>
            </div>
            <div class="d-flex gap-2">
                <button onclick="openBotSettingsModal()" class="btn btn-outline-info btn-sm fw-bold" data-i18n="btnBotSettings">⚙️ ตั้งค่าบอท</button>
                <button id="notifyToggleBtn" onclick="toggleNotifications()" class="btn btn-warning btn-sm fw-bold" data-i18n="enableNotify">🔔 เปิดระบบเสียง & แจ้งเตือน</button>
            </div>
        </div>

        <!-- 🔐 Admin User Management -->
        <div id="adminPanel" class="card p-4 mb-4 shadow-sm" style="display:none;">
            <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                <div>
                    <h4 class="card-title text-danger mb-1" data-i18n="adminPanelTitle">🛡️ Admin • จัดการผู้ใช้งาน</h4>
                    <small class="text-white-50" data-i18n="adminPanelSubtitle">อนุมัติผู้สมัคร ตรวจสอบประวัติ และดูผู้ที่กำลังใช้งาน • 🟢 ออนไลน์ = มี heartbeat ภายใน 90 วินาที • ปุ่มอนุมัติ / Ban / Unban อยู่ในตารางด้านล่าง</small>
                </div>
                <button class="btn btn-outline-info btn-sm" onclick="loadAdminUsers()" data-i18n="adminRefresh">🔄 รีเฟรช</button>
            </div>

            <div class="row g-3 mb-3">
                <div class="col-md-4">
                    <div class="card p-3 h-100">
                        <div class="text-warning small" data-i18n="adminPending">รออนุมัติ</div>
                        <div class="fs-3 fw-bold" id="adminPendingCount">0</div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card p-3 h-100">
                        <div class="text-success small" data-i18n="adminActive">กำลังใช้งาน</div>
                        <div class="fs-3 fw-bold" id="adminActiveCount">0</div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card p-3 h-100">
                        <div class="text-info small" data-i18n="adminTotal">ผู้ใช้ทั้งหมด</div>
                        <div class="fs-3 fw-bold" id="adminTotalCount">0</div>
                    </div>
                </div>
            </div>

            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr>
                            <th data-i18n="adminThUser">ผู้ใช้</th>
                            <th data-i18n="adminThStatus">สถานะ</th>
                            <th data-i18n="adminThCreated">สมัครเมื่อ</th>
                            <th data-i18n="adminThLastLogin">เข้าสู่ระบบล่าสุด</th>
                            <th data-i18n="adminThLastSeen">ใช้งานล่าสุด</th>
                            <th data-i18n="adminThLogins">จำนวนครั้ง</th>
                            <th data-i18n="adminThAction">จัดการ</th>
                        </tr>
                    </thead>
                    <tbody id="adminUsersBody"></tbody>
                </table>
            </div>
        </div>

        <div class="card p-4 mb-4 shadow-sm">
            <h4 class="card-title text-warning mb-3" data-i18n="formTitle">⏱️ บันทึกเวลาบอสตาย</h4>
            <form id="bossForm" class="row g-3" autocomplete="off">
                <div class="col-md-3">
                    <label class="form-label" data-i18n="labelBoss">เลือก หรือ พิมพ์ชื่อบอส</label>
                    <input list="bossOptions" id="bossSelect" class="form-control" placeholder="พิมพ์เพื่อค้นหา หรือคลิกเลือก..." data-i18n-ph="phBoss" required autocomplete="off">
                    <datalist id="bossOptions"></datalist>
                </div>
                <div class="col-md-3">
                    <label class="form-label" data-i18n="labelKillTime">เวลาที่ตาย (ระบบ 24 ชม.)</label>
                    <input type="text" id="killTime" class="form-control" placeholder="เช่น 17:30 หรือ 1730" data-i18n-ph="phKillTime" maxlength="5" autocomplete="off" enterkeyhint="done">
                    <small class="text-white-50" data-i18n="hintKillTime">*เว้นว่างไว้หากใช้เวลาปัจจุบัน</small>
                </div>
                <div class="col-md-2">
                    <label class="form-label" data-i18n="labelSpTime">เพิ่มเวลาพิเศษ (นาที)</label>
                    <input type="number" id="spTime" class="form-control" min="0" placeholder="0">
                </div>
                <div class="col-md-2">
                    <label class="form-label" data-i18n="labelNotice">แจ้งเตือนล่วงหน้า (นาที)</label>
                    <input type="number" id="noticeMinutes" class="form-control" min="1" value="5">
                </div>
                <div class="col-md-2 d-flex align-items-end">
                    <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnSave">⚔️ บันทึกเวลา</button>
                </div>
            </form>
        </div>

        <div class="card p-4 shadow-sm">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h4 class="card-title text-info mb-0" data-i18n="tableTitle">📜 ตารางเวลาบอสล่าสุด</h4>
                <button id="clearAllBtn" class="btn btn-outline-danger btn-sm" data-i18n="btnClear">ล้างตารางทั้งหมด</button>
            </div>
            
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle mb-0">
                    <thead>
                        <tr>
                            <th data-i18n="thBoss">ชื่อบอส</th>
                            <th data-i18n="thKillTime">เวลาตาย (24 ชม.)</th>
                            <th data-i18n="thSpawnTime">เวลาเกิด (24 ชม.)</th>
                            <th data-i18n="thCountdown">นับถอยหลัง</th>
                            <th data-i18n="thNotice">เตือนล่วงหน้า</th>
                            <th data-i18n="thRecordedBy">ผู้บันทึก</th>
                            <th data-i18n="thAction">จัดการ</th>
                        </tr>
                    </thead>
                    <tbody id="bossTableBody"></tbody>
                </table>
            </div>
        </div>

        <footer class="text-center text-white mt-4">
            <small data-i18n="footer">ระบบคำนวณเวลานับถอยหลังบอส Real-time • ข้อมูลบันทึกและซิงค์ผ่าน Cloud อัตโนมัติ</small>
        </footer>
    </div>

    <!-- Modal เปลี่ยนรหัสผ่าน -->
    <div class="modal fade" id="changeCodeModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content p-3">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title text-warning" data-i18n="modalChangeCodeTitle">🔑 เปลี่ยนโค้ดสำหรับสมัคร</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <form id="changeCodeForm">
                        <div class="mb-3">
                            <label class="form-label" data-i18n="labelNewRegCode">โค้ดสมัครใหม่</label>
                            <input type="text" id="newRegCodeInput" class="form-control" placeholder="กรอกโค้ดสำหรับสมัครใหม่" data-i18n-ph="phNewRegCode" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100 fw-bold" data-i18n="btnSaveCode">💾 บันทึกโค้ดใหม่</button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal ตั้งค่า Bot Notification -->
    <div class="modal fade" id="botSettingsModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content p-3">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title text-info" data-i18n="modalBotSettingsTitle">⚙️ ตั้งค่าแจ้งเตือนบอท</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <h6 class="text-warning mb-3" data-i18n="labelVoiceLang">🔊 ภาษาที่ใช้พูดแจ้งเตือน (Voice Notification)</h6>
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" id="ttsThToggle">
                        <label class="form-check-label" for="ttsThToggle">🇹🇭 ภาษาไทย (TH)</label>
                    </div>
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" id="ttsEnToggle">
                        <label class="form-check-label" for="ttsEnToggle">🇺🇸 ภาษาอังกฤษ (EN)</label>
                    </div>
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" id="ttsKoToggle">
                        <label class="form-check-label" for="ttsKoToggle">🇰🇷 ภาษาเกาหลี (KO)</label>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        // --- 1. ตั้งค่า FIREBASE CONFIG ---
        const firebaseConfig = Object.assign({
            apiKey: "AIzaSyC8-3NepDusElsH90Hp8mEUqVAFuWby094",
            authDomain: "skynet-3ad44.firebaseapp.com",
            databaseURL: "https://skynet-3ad44-default-rtdb.asia-southeast1.firebasedatabase.app",
            projectId: "skynet-3ad44",
            storageBucket: "skynet-3ad44.firebasestorage.app",
            messagingSenderId: "7120270934",
            appId: "1:7120270934:web:5e3db3f5dae6542352adf7",
            measurementId: "G-N8QER5X9BN"
        }, {{ firebase_web_config_json|safe }});

        // ตรวจว่ามี Web App config จริงก่อนเริ่ม Firebase Authentication
        if (firebaseConfig.apiKey.includes('PASTE_YOUR_') || firebaseConfig.authDomain.includes('PASTE_YOUR_') || firebaseConfig.projectId.includes('PASTE_YOUR_') || firebaseConfig.appId.includes('PASTE_YOUR_')) {
            console.warn('Firebase Authentication ยังไม่ได้ตั้งค่า Web App config ใน firebaseConfig');
        }

        // Initialize Firebase
        firebase.initializeApp(firebaseConfig);
        const db = firebase.database();
        const auth = firebase.auth();
        const bossRef = db.ref('boss_schedule');
        const usersRef = db.ref('users');
        const settingsRef = db.ref('app_settings');
        const botSettingsRef = db.ref('bot_settings'); // เพิ่ม Reference สำหรับการตั้งค่าบอท
        // API is hosted on Render while this Dashboard can be served by GitHub Pages.
        // Override with window.SKYNET_API_ORIGIN only when deploying the backend elsewhere.
        window.SKYNET_API_ORIGIN = window.SKYNET_API_ORIGIN || 'https://bosstimer-ry18.onrender.com';
        const sessionsRef = db.ref('dashboard_sessions');

        // 🔐 Admin account
        // บัญชี Admin ต้องสร้างใน Firebase Authentication ก่อน แล้วกำหนด
        // users/<ADMIN_UID> เป็น role=admin และ status=approved ใน Realtime Database
        // ไม่มีการสร้าง Admin อัตโนมัติจากหน้าเว็บ เพื่อป้องกันผู้ใช้ทั่วไปยกระดับสิทธิ์
        const ADMIN_DEFAULT_USERNAME = "admin";
        const ACTIVE_SESSION_TIMEOUT_MS = 90000;

        // โค้ดสมัครเริ่มต้น
        let currentRegCode = "1234";
        settingsRef.child('register_code').on('value', (snap) => {
            if (snap.exists() && snap.val()) {
                currentRegCode = snap.val();
            } else {
                settingsRef.child('register_code').set("1234");
            }
        });

        // ดึงการตั้งค่า Bot Settings ภาษาการแจ้งเตือน
        botSettingsRef.on('value', (snap) => {
            const data = snap.val() || {};
            // ถ้าค่ายังไม่ถูกตั้งใน Database จะให้เปิดเป็น True เป็นค่าพื้นฐาน
            document.getElementById('ttsThToggle').checked = data.tts_th_enabled !== false; 
            document.getElementById('ttsEnToggle').checked = data.tts_en_enabled !== false;
            document.getElementById('ttsKoToggle').checked = data.tts_ko_enabled !== false;
        });

        // ตรวจจับเมื่อมีการกดเปลี่ยนสวิตช์ภาษา
        ['ttsThToggle', 'ttsEnToggle', 'ttsKoToggle'].forEach(id => {
            document.getElementById(id).addEventListener('change', async (e) => {
                if (!await requireApprovedUser()) {
                    e.target.checked = !e.target.checked;
                    return;
                }
                const key = id === 'ttsThToggle' ? 'tts_th_enabled' : 
                            id === 'ttsEnToggle' ? 'tts_en_enabled' : 'tts_ko_enabled';
                await botSettingsRef.child(key).set(e.target.checked);
            });
        });

        // ฟังก์ชันเปิด Modal ตั้งค่า Bot
        async function openBotSettingsModal() {
            if (!await requireApprovedUser()) return;
            const modalEl = document.getElementById('botSettingsModal');
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
        }

        // --- 2. ฐานข้อมูลภาษา (i18n) ---
        const TRANSLATIONS = {
            th: {
                authTitle: "⚔️ Boss Timer Access",
                tabLogin: "เข้าสู่ระบบ",
                tabRegister: "ลงทะเบียน",
                labelUsername: "ชื่อผู้ใช้",
                labelPassword: "รหัสผ่าน",
                labelRegCode: "โค้ดสำหรับสมัคร",
                rememberMe: "จำชื่อผู้ใช้",
                btnLogin: "🔑 เข้าสู่ระบบ",
                btnRegister: "📝 ลงทะเบียน",
                btnLogout: "🚪 ออกจากระบบ",
                btnConfigCode: "🔑 เปลี่ยนโค้ดสมัคร",
                modalChangeCodeTitle: "🔑 เปลี่ยนโค้ดสำหรับสมัคร",
                labelNewRegCode: "โค้ดสมัครใหม่",
                btnSaveCode: "💾 บันทึกโค้ดใหม่",
                btnBotSettings: "⚙️ ตั้งค่าบอท",
                modalBotSettingsTitle: "⚙️ ตั้งค่าแจ้งเตือนบอท",
                labelVoiceLang: "🔊 ภาษาที่ใช้พูดแจ้งเตือน (Voice Notification)",
                phLoginUser: "กรอกชื่อผู้ใช้",
                phLoginPass: "กรอกรหัสผ่าน",
                phRegUser: "ตั้งชื่อผู้ใช้",
                phRegPass: "ตั้งรหัสผ่าน",
                phRegCode: "กรอกโค้ดสำหรับสมัคร",
                phNewRegCode: "กรอกโค้ดสำหรับสมัครใหม่",
                title: "⚔️ Boss Timer Dashboard",
                online: "🟢 Realtime Sync Active",
                labelLanguage: "🌐 ภาษา:",
                labelTimezone: "🌍 เขตเวลา:",
                tzAuto: "💻 อัตโนมัติ (ตามเครื่อง)",
                enableNotify: "🔔 เปิดระบบเสียง & แจ้งเตือน",
                disableNotify: "🔕 ปิดระบบเสียง & แจ้งเตือน",
                formTitle: "⏱️ บันทึกเวลาบอสตาย",
                labelBoss: "เลือก หรือ พิมพ์ชื่อบอส",
                phBoss: "พิมพ์เพื่อค้นหา หรือคลิกเลือก...",
                labelKillTime: "เวลาที่ตาย (ระบบ 24 ชม.)",
                labelSpTime: "เพิ่มเวลาพิเศษ (นาที)",
                phKillTime: "เช่น 17:30 หรือ 1730",
                hintKillTime: "*เว้นว่างไว้หากใช้เวลาปัจจุบัน",
                labelNotice: "แจ้งเตือนล่วงหน้า (นาที)",
                btnSave: "⚔️ บันทึกเวลา",
                tableTitle: "📜 ตารางเวลาบอสล่าสุด",
                btnClear: "ล้างตารางทั้งหมด",
                thBoss: "ชื่อบอส",
                thKillTime: "เวลาตาย (24 ชม.)",
                thSpawnTime: "เวลาเกิด (24 ชม.)",
                thCountdown: "นับถอยหลัง",
                thNotice: "เตือนล่วงหน้า",
                thRecordedBy: "ผู้บันทึก",
                thAction: "จัดการ",
                emptyMsg: "📌 ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ",
                spawned: "⚔️ เกิดแล้ว!",
                btnDelete: "ลบ",
                hourUnit: "ชม.",
                minUnit: "นาที",
                secUnit: "วินาที",
                footer: "ระบบคำนวณเวลานับถอยหลังบอส Real-time • ข้อมูลบันทึกและซิงค์ผ่าน Cloud อัตโนมัติ",
                invalidTimeAlert: "กรุณากรอกเวลาให้ถูกต้องตามระบบ 24 ชั่วโมง (เช่น 08:30 หรือ 17:45)",
                confirmClear: "คุณต้องการล้างตารางบอสทั้งหมดใช่หรือไม่?",
                notifyReadyTitle: "⚔️ ระบบแจ้งเตือนพร้อมทำงาน",
                notifyReadyBody: "จะมีการแจ้งเตือนเมื่อบอสใกล้เกิดและเมื่อบอสเกิดแล้ว",
                noNotifySupport: "เบราว์เซอร์นี้ไม่รองรับการแจ้งเตือนแบบ Pop-up",
                grantNotifyPrompt: "กรุณากดอนุญาต (Allow) การแจ้งเตือนในเบราว์เซอร์ของคุณ",
                errRegCodeInvalid: "โค้ดสำหรับสมัครไม่ถูกต้อง!",
                errUserExists: "ชื่อผู้ใช้นี้ถูกลงทะเบียนไปแล้ว!",
                regSuccess: "ลงทะเบียนสำเร็จ! กรุณาเข้าสู่ระบบ",
                errLoginFailed: "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง!",
                codeChangedSuccess: "เปลี่ยนโค้ดสำหรับสมัครเรียบร้อยแล้ว!",
                spawnNotifyTitle: "⚔️ {boss} เกิดแล้ว!",
                spawnNotifyBody: "บอส {boss} ได้เกิดแล้วในขณะนี้",
                noticeNotifyTitle: "⏳ {boss} ใกล้เกิด!",
                noticeNotifyBody: "บอส {boss} จะเกิดในอีก {min} นาที",
                regPending: "ลงทะเบียนสำเร็จ แต่บัญชียังรอ Admin อนุมัติ จึงยังเข้าใช้งานไม่ได้",
                errPendingApproval: "บัญชีนี้กำลังรอ Admin อนุมัติ",
                errAccountRejected: "บัญชีนี้ถูกปฏิเสธหรือปิดการใช้งานโดย Admin",
                errAccountNotApproved: "บัญชีนี้ยังไม่ได้รับอนุมัติจาก Admin",
                errWeakAccount: "ชื่อผู้ใช้ต้องมีอย่างน้อย 3 ตัวอักษร และรหัสผ่านอย่างน้อย 6 ตัวอักษร",
                errAdminUsername: "ไม่สามารถใช้ชื่อ admin สำหรับการสมัครทั่วไปได้",
                adminPanelTitle: "🛡️ Admin • จัดการผู้ใช้งาน",
                adminPanelSubtitle: "อนุมัติผู้สมัคร ตรวจสอบประวัติ และดูผู้ที่กำลังใช้งาน • ปุ่มอนุมัติ / Ban / Unban อยู่ในตารางด้านล่าง",
                adminRefresh: "🔄 รีเฟรช",
                adminPending: "รออนุมัติ",
                adminActive: "กำลังใช้งาน",
                adminTotal: "ผู้ใช้ทั้งหมด",
                adminThUser: "ผู้ใช้",
                adminThStatus: "สถานะ",
                adminThCreated: "สมัครเมื่อ",
                adminThLastLogin: "เข้าสู่ระบบล่าสุด",
                adminThLastSeen: "ใช้งานล่าสุด",
                adminThLogins: "จำนวนครั้ง",
                adminThAction: "จัดการ",
                adminApprove: "อนุมัติ",
                adminReject: "ปฏิเสธ",
                adminDisable: "⛔ Ban",
                adminActivate: "♻️ Unban",
                adminBan: "แบน",
                adminUnban: "ปลดแบน",
                adminNoUsers: "ยังไม่มีผู้ใช้งาน",
                adminOnly: "คำสั่งนี้อนุญาตเฉพาะ Admin",
                adminCannotChangeAdmin: "ไม่สามารถเปลี่ยนสถานะบัญชี Admin ได้"
            },
            en: {
                authTitle: "⚔️ Boss Timer Access",
                tabLogin: "Login",
                tabRegister: "Register",
                labelUsername: "Username",
                labelPassword: "Password",
                labelRegCode: "Registration Code",
                rememberMe: "Remember Username",
                btnLogin: "🔑 Login",
                btnRegister: "📝 Register",
                btnLogout: "🚪 Logout",
                btnConfigCode: "🔑 Change Reg Code",
                modalChangeCodeTitle: "🔑 Change Registration Code",
                labelNewRegCode: "New Registration Code",
                btnSaveCode: "💾 Save New Code",
                btnBotSettings: "⚙️ Bot Settings",
                modalBotSettingsTitle: "⚙️ Bot Notification Settings",
                labelVoiceLang: "🔊 Voice Notification Languages",
                phLoginUser: "Enter username",
                phLoginPass: "Enter password",
                phRegUser: "Set username",
                phRegPass: "Set password",
                phRegCode: "Enter registration code",
                phNewRegCode: "Enter new registration code",
                title: "⚔️ Boss Timer Dashboard",
                online: "🟢 Realtime Sync Active",
                labelLanguage: "🌐 Language:",
                labelTimezone: "🌍 Timezone:",
                tzAuto: "💻 Auto (Device Local)",
                enableNotify: "🔔 Enable Sound & Alerts",
                disableNotify: "🔕 Disable Sound & Alerts",
                formTitle: "⏱️ Record Boss Kill Time",
                labelBoss: "Select or Type Boss Name",
                phBoss: "Type to search or select...",
                labelKillTime: "Kill Time (24h Format)",
                labelSpTime: "Special Time (mins)",
                phKillTime: "e.g. 17:30 or 1730",
                hintKillTime: "*Leave blank to use current time",
                labelNotice: "Advance Notice (Mins)",
                btnSave: "⚔️ Save Time",
                tableTitle: "📜 Boss Schedule Table",
                btnClear: "Clear All Data",
                thBoss: "Boss Name",
                thKillTime: "Kill Time (24h)",
                thSpawnTime: "Spawn Time (24h)",
                thCountdown: "Countdown",
                thNotice: "Notice",
                thRecordedBy: "Recorded By",
                thAction: "Action",
                emptyMsg: "📌 No boss times recorded in the system",
                spawned: "⚔️ Spawned!",
                btnDelete: "Delete",
                hourUnit: "h",
                minUnit: "m",
                secUnit: "s",
                footer: "Real-time Boss Countdown System • Synced with Cloud Database",
                invalidTimeAlert: "Please enter time in valid 24-hour format (e.g. 08:30 or 17:45)",
                confirmClear: "Are you sure you want to clear all boss timers?",
                notifyReadyTitle: "⚔️ Notifications Active",
                notifyReadyBody: "You will be alerted before boss spawns and when spawned.",
                noNotifySupport: "This browser does not support Pop-up notifications.",
                grantNotifyPrompt: "Please grant notification permissions in your browser.",
                errRegCodeInvalid: "Invalid Registration Code!",
                errUserExists: "Username already exists!",
                regSuccess: "Registration successful! Please login.",
                errLoginFailed: "Invalid username or password!",
                codeChangedSuccess: "Registration code updated successfully!",
                spawnNotifyTitle: "⚔️ {boss} Spawned!",
                spawnNotifyBody: "Boss {boss} has spawned!",
                noticeNotifyTitle: "⏳ {boss} Spawning Soon!",
                noticeNotifyBody: "Boss {boss} will spawn in {min} minutes",
                regPending: "Registration submitted. Your account is pending Admin approval.",
                errPendingApproval: "This account is waiting for Admin approval.",
                errAccountRejected: "This account was rejected or disabled by Admin.",
                errAccountNotApproved: "This account has not been approved by Admin.",
                errWeakAccount: "Username must be at least 3 characters and password at least 6 characters.",
                errAdminUsername: "The admin username is reserved.",
                adminPanelTitle: "🛡️ Admin • User Management",
                adminPanelSubtitle: "Approve registrations, review history, and see active users. Approve / Ban / Unban buttons are in the table below.",
                adminRefresh: "🔄 Refresh",
                adminPending: "Pending",
                adminActive: "Active Now",
                adminTotal: "Total Users",
                adminThUser: "User",
                adminThStatus: "Status",
                adminThCreated: "Registered",
                adminThLastLogin: "Last Login",
                adminThLastSeen: "Last Seen",
                adminThLogins: "Logins",
                adminThAction: "Action",
                adminApprove: "Approve",
                adminReject: "Reject",
                adminDisable: "⛔ Ban",
                adminActivate: "♻️ Unban",
                adminBan: "Ban",
                adminUnban: "Unban",
                adminNoUsers: "No users found.",
                adminOnly: "Admin only.",
                adminCannotChangeAdmin: "The Admin account cannot be changed."
            },
            ko: {
                authTitle: "⚔️ Boss Timer Access",
                tabLogin: "로그인",
                tabRegister: "회원가입",
                labelUsername: "사용자 이름",
                labelPassword: "비밀번호",
                labelRegCode: "가입 코드",
                rememberMe: "사용자 이름 저장",
                btnLogin: "🔑 로그인",
                btnRegister: "📝 회원가입",
                btnLogout: "🚪 로그아웃",
                btnConfigCode: "🔑 가입 코드 변경",
                modalChangeCodeTitle: "🔑 가입 코드 변경",
                labelNewRegCode: "새 가입 코드",
                btnSaveCode: "💾 새 코드 저장",
                btnBotSettings: "⚙️ 봇 설정",
                modalBotSettingsTitle: "⚙️ 봇 알림 설정",
                labelVoiceLang: "🔊 음성 알림 언어",
                phLoginUser: "사용자 이름을 입력하세요",
                phLoginPass: "비밀번호를 입력하세요",
                phRegUser: "사용자 이름 설정",
                phRegPass: "비밀번호 설정",
                phRegCode: "가입 코드를 입력하세요",
                phNewRegCode: "새 가입 코드를 입력하세요",
                title: "⚔️ Boss Timer Dashboard",
                online: "🟢 실시간 동기화 활성화",
                labelLanguage: "🌐 언어:",
                labelTimezone: "🌍 시간대:",
                tzAuto: "💻 자동 (기기 설정)",
                enableNotify: "🔔 소리 및 알림 켜기",
                disableNotify: "🔕 소리 및 알림 끄기",
                formTitle: "⏱️ 보스 처치 시간 기록",
                labelBoss: "보스 선택 또는 입력",
                phBoss: "검색 또는 선택...",
                labelKillTime: "처치 시간 (24시간 형식)",
                labelSpTime: "추가 시간 (분)",
                phKillTime: "예: 17:30 또는 1730",
                hintKillTime: "*현재 시간을 사용하려면 비워두세요",
                labelNotice: "사전 알림 (분)",
                btnSave: "⚔️ 시간 저장",
                tableTitle: "📜 최근 보스 시간표",
                btnClear: "전체 목록 삭제",
                thBoss: "보스 이름",
                thKillTime: "처치 시간 (24h)",
                thSpawnTime: "젠 시간 (24h)",
                thCountdown: "카운트다운",
                thNotice: "사전 알림",
                thRecordedBy: "기록자",
                thAction: "관리",
                emptyMsg: "📌 시스템에 기록된 보스 시간이 없습니다",
                spawned: "⚔️ 젠 완료!",
                btnDelete: "삭제",
                hourUnit: "시간",
                minUnit: "분",
                secUnit: "초",
                footer: "실시간 보스 카운트다운 시스템 • 클라우드 자동 동기화",
                invalidTimeAlert: "24시간 형식에 맞게 올바른 시간을 입력해주세요. (예: 08:30 หรือ 17:45)",
                confirmClear: "모든 보스 타이머를 삭제하시겠습니까?",
                notifyReadyTitle: "⚔️ 알림 시스템 준비 완료",
                notifyReadyBody: "보스 젠 임박 및 젠 완료 시 알림이 전송됩니다.",
                noNotifySupport: "이 브라우저는 팝업 알림을 지원하지 않습니다.",
                grantNotifyPrompt: "브라우저에서 알림 권한을 허용해주세요.",
                errRegCodeInvalid: "가입 코드가 올바르지 않습니다!",
                errUserExists: "이미 존재하는 사용자 이름입니다!",
                regSuccess: "회원가입 성공! 로그인해주세요.",
                errLoginFailed: "사용자 이름 หรือ 비밀번호가 올바르지 않습니다!",
                codeChangedSuccess: "가입 코드가 성공적으로 변경되었습니다!",
                spawnNotifyTitle: "⚔️ {boss} 젠 완료!",
                spawnNotifyBody: "보스 {boss}(이)가 지금 젠되었습니다.",
                noticeNotifyTitle: "⏳ {boss} 젠 임박!",
                noticeNotifyBody: "보스 {boss}(이)가 {min}분 후에 젠됩니다.",
                regPending: "회원가입이 완료되었습니다. Admin 승인을 기다려 주세요.",
                errPendingApproval: "이 계정은 Admin 승인을 기다리고 있습니다.",
                errAccountRejected: "이 계정은 Admin에 의해 거부되었거나 비활성화되었습니다.",
                errAccountNotApproved: "이 계정은 아직 Admin의 승인을 받지 못했습니다.",
                errWeakAccount: "사용자 이름은 3자 이상, 비밀번호는 6자 이상이어야 합니다.",
                errAdminUsername: "admin 사용자 이름은 일반 가입에 사용할 수 없습니다.",
                adminPanelTitle: "🛡️ Admin • 사용자 관리",
                adminPanelSubtitle: "가입 승인, 이용 기록 및 현재 접속 사용자를 확인합니다.",
                adminRefresh: "🔄 새로고침",
                adminPending: "승인 대기",
                adminActive: "현재 접속",
                adminTotal: "전체 사용자",
                adminThUser: "사용자",
                adminThStatus: "상태",
                adminThCreated: "가입일",
                adminThLastLogin: "최근 로그인",
                adminThLastSeen: "최근 활동",
                adminThLogins: "로그인 횟수",
                adminThAction: "관리",
                adminApprove: "อนุมัติ",
                adminReject: "ปฏิเสธ",
                adminDisable: "⛔ Ban",
                adminActivate: "♻️ Unban",
                adminBan: "แบน",
                adminUnban: "ปลดแบน",
                adminNoUsers: "ยังไม่มีผู้ใช้งาน",
                adminOnly: "คำสั่งนี้อนุญาตเฉพาะ Admin",
                adminCannotChangeAdmin: "ไม่สามารถเปลี่ยนสถานะบัญชี Admin ได้"
            }
        };

        let currentLang = localStorage.getItem('app_lang') || 'th';
        let currentTz = localStorage.getItem('app_tz') || 'auto';
        const browserNotified = new Set();
        let isNotifyEnabled = localStorage.getItem('notify_enabled') === 'true';
        let activeBosses = {};

        const BOSS_DATABASE = {
            "Wadangka": { cd: 9000, notice: 30 },
            "Elemental Queen": { cd: 9000, notice: 5 },
            "Tank": { cd: 3500, notice: 5 },
            "Swirl Flame": { cd: 3500, notice: 5 },
            "Maelstrom": { cd: 3500, notice: 5 },
            "Twister": { cd: 3500, notice: 5 },
            "Bigmama": { cd: 172800, notice: 30 },
            "Chief Magief": { cd: 1800, notice: 5 },
            "Faith": { cd: 21180, notice: 30 },
            "Apapa": { cd: 900, notice: 5 },
            "Corrupt Forest Keeper": { cd: 3480, notice: 5 },
            "Recluse": { cd: 40980, notice: 30 },
            "Blackskull": { cd: 3410, notice: 5 },
            "Sleepy Kooii": { cd: 1200, notice: 5 },
            "Awaken Kooii": { cd: 3780, notice: 5 },
            "Eeheehee": { cd: 4008, notice: 5 },
            "Ooheeheek": { cd: 4083, notice: 5 },
            "Oohehe": { cd: 3908, notice: 5 },
            "Guardian Imp": { cd: 3780, notice: 5 },
            "Devilang": { cd: 19980, notice: 30 },
            "Blackjuno": { cd: 2100, notice: 5 },
            "Blacksky": { cd: 2100, notice: 5 },
            "Red Fox": { cd: 1200, notice: 5 },
            "7tailfox": { cd: 1200, notice: 5 },
            "777Tailfox": { cd: 1800, notice: 5 },
            "Sunrise Flower": { cd: 1200, notice: 5 },
            "Magma Senior Thief": { cd: 1200, notice: 5 },
            "Bbinikjoe": { cd: 1200, notice: 5 },
            "Bigmouse": { cd: 1200, notice: 5 },
            "Caligo": { cd: 604800, notice: 60 },
            "Poison Root Flower": { cd: 1690, notice: 5 },
            "Contaminated Queen Bee": { cd: 1680, notice: 5 },
            "Rotten Pudding": { cd: 1800, notice: 5 },
            "Swamp Flower Monster": { cd: 1800, notice: 5 },
            "Ukpana": { cd: 172800, notice: 30 },
            "Darlene the Witch": { cd: 259200, notice: 30 },
            "Illust": { cd: 259200, notice: 30 },
            "Actaemon": { cd: 21600, notice: 30 },
            "Aiyo's Protector": { cd: 259200, notice: 30 },
            "Glucose": { cd: 1800, notice: 5 },
            "Overload": { cd: 1792, notice: 5 },
            "Soul Lich": { cd: 87300, notice: 30 },
            "Platanista": { cd: 604800, notice: 60 },
            "Barslaf": { cd: 172800, notice: 30 },
            "Billiard": { cd: 28503, notice: 5 },
            "Shaaack": { cd: 1800, notice: 5 },
            "Suuuk": { cd: 1200, notice: 5 },
            "Sususuk": { cd: 1200, notice: 5 },
            "sandgrave": { cd: 1200, notice: 5 },
            "Elder Beholder": { cd: 1200, notice: 5 }
        };

        // --- 3. ระบบ Authentication & Session (Firebase Authentication) ---
        const AUTH_EMAIL_DOMAIN = "@skynet-3ad44.firebaseapp.com";
        settingsRef.child('register_code').on('value', (snap) => {
            if (snap.exists() && snap.val()) currentRegCode = snap.val();
            else settingsRef.child('register_code').set("1234");
        });

        function makeUserKey(username) {
            return username.toLowerCase().replace(/[.#$\\[\\]\\/]/g, '_');
        }

        function usernameToAuthEmail(username) {
            return `${makeUserKey(username)}${AUTH_EMAIL_DOMAIN}`;
        }

        function formatAdminDate(value) {
            if (!value) return '-';
            const d = new Date(value);
            if (isNaN(d.getTime())) return '-';
            return d.toLocaleString('th-TH', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' });
        }

        let currentSessionId = null;
        let currentUserKey = null;
        let currentUserData = null;
        let heartbeatTimer = null;

        async function createUserSession(userKey, userData) {
            currentSessionId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}_${Math.random().toString(36).slice(2)}`);
            currentUserKey = userKey;
            currentUserData = userData;
            const now = new Date().toISOString();

            await sessionsRef.child(currentSessionId).set({
                userKey, username:userData.username, loginAt:now, lastSeenAt:now, active:true
            });
            await usersRef.child(userKey).update({
                lastLoginAt:now, lastSeenAt:now, lastLogoutAt:null,
                loginCount:(Number(userData.loginCount)||0)+1
            });
            localStorage.setItem('logged_user', userData.username);
            localStorage.setItem('logged_user_key', userKey);
            localStorage.setItem('logged_session_id', currentSessionId);
            localStorage.setItem('logged_role', userData.role || 'user');
            startSessionHeartbeat();
        }

        function startSessionHeartbeat() {
            if (heartbeatTimer) clearInterval(heartbeatTimer);
            heartbeatTimer = setInterval(async () => {
                const sessionId=localStorage.getItem('logged_session_id');
                const userKey=localStorage.getItem('logged_user_key');
                if (!sessionId || !userKey || !auth.currentUser) return;
                const now=new Date().toISOString();
                try {
                    await sessionsRef.child(sessionId).update({lastSeenAt:now,active:true});
                    await usersRef.child(userKey).update({lastSeenAt:now});
                } catch(err) { console.warn('Session heartbeat error:',err); }
            },30000);
        }

        async function endUserSession(signOutAuth=true) {
            const sessionId=localStorage.getItem('logged_session_id');
            const userKey=localStorage.getItem('logged_user_key');
            const now=new Date().toISOString();
            if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer=null; }
            try {
                if (sessionId) await sessionsRef.child(sessionId).update({lastSeenAt:now,logoutAt:now,active:false});
                if (userKey) await usersRef.child(userKey).update({lastSeenAt:now,lastLogoutAt:now});
            } catch(err) { console.warn('Session logout update error:',err); }
            if (signOutAuth) { try { await auth.signOut(); } catch(err) {} }
            ['logged_user','logged_user_key','logged_session_id','logged_role'].forEach(k=>localStorage.removeItem(k));
            currentSessionId=null; currentUserKey=null; currentUserData=null;
        }

        async function checkAuthSession(firebaseUserOverride=null) {
            const savedUser=localStorage.getItem('saved_username');
            if(savedUser){document.getElementById('loginUser').value=savedUser;document.getElementById('rememberMe').checked=true;}

            const firebaseUser=firebaseUserOverride || auth.currentUser;
            if(firebaseUser){
                try{
                    const snap=await usersRef.child(firebaseUser.uid).once('value');
                    if(!snap.exists()) throw new Error('USER_NOT_FOUND');
                    const userData=snap.val();
                    if(userData.status!=='approved'){
                        await endUserSession(true);
                        throw new Error(userData.status==='pending'?'ACCOUNT_PENDING':'ACCOUNT_NOT_APPROVED');
                    }

                    currentUserData=userData;
                    currentUserKey=firebaseUser.uid;
                    dashboardUsersCache[firebaseUser.uid] = userData;
                    setTimeout(cacheCurrentUserProfile, 0);
                    document.getElementById('authContainer').style.display='none';
                    document.getElementById('mainDashboard').style.display='block';
                    document.getElementById('userBadge').innerText=`👤 ${userData.username || firebaseUser.email}${userData.role==='admin'?' • 🛡️ Admin':''}`;
                    const adminPanel=document.getElementById('adminPanel');
                    const adminOnlyButtons=document.querySelectorAll('[data-admin-only]');
                    const adminMenu=document.getElementById('adminPanelMenuBtn');
                    if(userData.role==='admin'){
                        adminPanel.style.display='block';
                        adminOnlyButtons.forEach(el=>el.style.display='');
                        if(adminMenu) adminMenu.style.display='inline-block';
                        await loadAdminUsers();
                    }else{
                        adminPanel.style.display='none';
                        adminOnlyButtons.forEach(el=>el.style.display='none');
                        if(adminMenu) adminMenu.style.display='none';
                    }
                    if(!currentSessionId) await createUserSession(firebaseUser.uid,userData);
                    else startSessionHeartbeat();
                    return true;
                }catch(err){
                    if(err.message==='ACCOUNT_PENDING') alert(TRANSLATIONS[currentLang].errPendingApproval);
                    else if(err.message==='ACCOUNT_NOT_APPROVED') alert(TRANSLATIONS[currentLang].errAccountNotApproved);
                }
            }
            document.getElementById('authContainer').style.display='block';
            document.getElementById('mainDashboard').style.display='none';
            const adminPanel=document.getElementById('adminPanel'); if(adminPanel) adminPanel.style.display='none';
            const adminMenu=document.getElementById('adminPanelMenuBtn'); if(adminMenu) adminMenu.style.display='none';
            document.querySelectorAll('[data-admin-only]').forEach(el=>el.style.display='none');
            return false;
        }

        function scrollToAdminPanel(){
            const panel=document.getElementById('adminPanel');
            if(panel) panel.scrollIntoView({behavior:'smooth',block:'start'});
        }

        document.getElementById('registerForm').addEventListener('submit',async(e)=>{
            e.preventDefault();
            const username=document.getElementById('regUser').value.trim();
            const password=document.getElementById('regPass').value;
            const regCode=document.getElementById('regCode').value.trim();
            if(username.length<3 || password.length<6){alert(TRANSLATIONS[currentLang].errWeakAccount);return;}
            if(makeUserKey(username)===makeUserKey(ADMIN_DEFAULT_USERNAME)){alert(TRANSLATIONS[currentLang].errAdminUsername);return;}
            if(regCode!==currentRegCode){alert(TRANSLATIONS[currentLang].errRegCodeInvalid);return;}
            try{
                const cred=await auth.createUserWithEmailAndPassword(usernameToAuthEmail(username),password);
                const now=new Date().toISOString();
                await usersRef.child(cred.user.uid).set({
                    username, usernameKey:makeUserKey(username), role:'user', status:'pending', createdAt:now,
                    approvedAt:null,approvedBy:null,rejectedAt:null,rejectedBy:null,lastLoginAt:null,lastLogoutAt:null,lastSeenAt:null,loginCount:0
                });
                await auth.signOut();
                alert(TRANSLATIONS[currentLang].regPending);
                document.getElementById('registerForm').reset();
                new bootstrap.Tab(document.getElementById('login-tab')).show();
            }catch(err){
                console.error(err);
                if(err.code==='auth/email-already-in-use') alert(TRANSLATIONS[currentLang].errUserExists);
                else showAuthError(err);
            }
        });

        document.getElementById('loginForm').addEventListener('submit',async(e)=>{
            e.preventDefault();
            const username=document.getElementById('loginUser').value.trim();
            const password=document.getElementById('loginPass').value;
            const rememberMe=document.getElementById('rememberMe').checked;
            if(!username || !password){alert(TRANSLATIONS[currentLang].errLoginFailed);return;}
            try{
                const cred=await auth.signInWithEmailAndPassword(usernameToAuthEmail(username),password);
                const snap=await usersRef.child(cred.user.uid).once('value');
                if(!snap.exists()){await auth.signOut();alert(TRANSLATIONS[currentLang].errLoginFailed);return;}
                const userData=snap.val();
                if(userData.status==='pending'){await auth.signOut();alert(TRANSLATIONS[currentLang].errPendingApproval);return;}
                if(userData.status==='rejected'||userData.status==='disabled'){await auth.signOut();alert(TRANSLATIONS[currentLang].errAccountRejected);return;}
                if(userData.status!=='approved'){await auth.signOut();alert(TRANSLATIONS[currentLang].errAccountNotApproved);return;}
                if(rememberMe)localStorage.setItem('saved_username',username);else localStorage.removeItem('saved_username');
                await createUserSession(cred.user.uid,userData);
                await checkAuthSession(cred.user);
            }catch(err){
                console.error('Firebase Authentication login error:',err);
                const code = err && err.code ? err.code : 'unknown';
                const detail = err && err.message ? err.message : '';
                alert(`${TRANSLATIONS[currentLang].errLoginFailed}\n\nFirebase: ${code}${detail ? '\n' + detail : ''}`);
            }
        });

        async function logout(){await endUserSession(true);checkAuthSession();}

        function openChangeCodeModal(){
            const modalEl=document.getElementById('changeCodeModal');
            const modal=bootstrap.Modal.getOrCreateInstance(modalEl);
            document.getElementById('newRegCodeInput').value=currentRegCode; modal.show();
        }

        document.getElementById('changeCodeForm').addEventListener('submit',async(e)=>{
            e.preventDefault();
            if(!await requireAdmin())return;
            const newCode=document.getElementById('newRegCodeInput').value.trim();
            if(newCode){await settingsRef.child('register_code').set(newCode);alert(TRANSLATIONS[currentLang].codeChangedSuccess);const modal=bootstrap.Modal.getInstance(document.getElementById('changeCodeModal'));if(modal)modal.hide();}
        });

        // --- Admin User Management ---
        async function requireAdmin(){
            const uid=auth.currentUser && auth.currentUser.uid;
            if(!uid)return false;
            const snap=await usersRef.child(uid).once('value'); const user=snap.val();
            if(!user||user.role!=='admin'||user.status!=='approved'){alert(TRANSLATIONS[currentLang].adminOnly);return false;}
            currentUserKey=uid; currentUserData=user; return true;
        }

        async function loadAdminUsers() {
            if (!currentUserKey) currentUserKey = localStorage.getItem('logged_user_key');
            if (!currentUserKey) return;

            const adminSnap = await usersRef.child(currentUserKey).once('value');
            const adminData = adminSnap.val();

            if (!adminData || adminData.role !== 'admin' || adminData.status !== 'approved') {
                document.getElementById('adminPanel').style.display = 'none';
                return;
            }

            currentUserData = adminData;

            const usersSnap = await usersRef.once('value');
            const users = usersSnap.val() || {};
            const sessionsSnap = await sessionsRef.once('value');
            const sessions = sessionsSnap.val() || {};
            const nowMs = Date.now();

            const userRows = Object.entries(users).map(([key, user]) => {
                const userSessions = Object.values(sessions).filter(s => s && s.userKey === key);
                const latestSession = userSessions.sort((a, b) =>
                    new Date(b.lastSeenAt || b.loginAt || 0) - new Date(a.lastSeenAt || a.loginAt || 0)
                )[0];

                const lastSeen = user.lastSeenAt || (latestSession && latestSession.lastSeenAt);
                const active = userSessions.some(session => {
                    if (!session || session.active === false || !session.lastSeenAt) return false;
                    const age = nowMs - new Date(session.lastSeenAt).getTime();
                    return age >= 0 && age <= ACTIVE_SESSION_TIMEOUT_MS;
                });

                return { key, user, active };
            });

            const pendingCount = userRows.filter(x => x.user.status === 'pending').length;
            const activeCount = userRows.filter(x => x.active && x.user.status === 'approved').length;

            document.getElementById('adminPendingCount').innerText = pendingCount;
            document.getElementById('adminActiveCount').innerText = activeCount;
            document.getElementById('adminTotalCount').innerText = userRows.length;

            const tbody = document.getElementById('adminUsersBody');
            tbody.innerHTML = '';

            userRows.sort((a, b) => {
                const order = { pending: 0, approved: 1, rejected: 2, disabled: 3 };
                return (order[a.user.status] ?? 9) - (order[b.user.status] ?? 9) ||
                       (new Date(b.user.createdAt || 0) - new Date(a.user.createdAt || 0));
            });

            userRows.forEach(({ key, user, active }) => {
                const statusBadge = user.role === 'admin'
                    ? '<span class="badge bg-danger">🛡️ ADMIN</span>'
                    : user.status === 'pending'
                        ? '<span class="badge bg-warning text-dark">⏳ Pending</span>'
                        : user.status === 'approved'
                            ? `<span class="badge ${active ? 'bg-success' : 'bg-primary'}">${active ? '🟢 Active' : '✅ Approved'}</span>`
                            : user.status === 'rejected'
                                ? '<span class="badge bg-danger">❌ Rejected</span>'
                                : '<span class="badge bg-secondary">⛔ Banned</span>';

                let actionHtml = '-';
                if (user.role !== 'admin') {
                    if (user.status === 'pending') {
                        actionHtml = `
                            <div class="d-flex gap-1 flex-wrap">
                                <button class="btn btn-sm btn-success" onclick="setUserStatus('${key}', 'approved')">✅ ${TRANSLATIONS[currentLang].adminApprove}</button>
                                <button class="btn btn-sm btn-danger" onclick="setUserStatus('${key}', 'disabled')">❌ ${TRANSLATIONS[currentLang].adminReject}</button>
                            </div>`;
                    } else if (user.status === 'approved') {
                        actionHtml = `<button class="btn btn-sm btn-outline-danger" onclick="setUserStatus('${key}', 'disabled')">⛔ ${TRANSLATIONS[currentLang].adminBan || 'Ban'}</button>`;
                    } else if (user.status === 'disabled' || user.status === 'rejected') {
                        actionHtml = `<button class="btn btn-sm btn-outline-success" onclick="setUserStatus('${key}', 'approved')">♻️ ${TRANSLATIONS[currentLang].adminUnban || 'Unban'}</button>`;
                    }
                }

                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>
                        <div class="fw-bold text-info">${escapeHtml(user.username || key)}</div>
                        <small class="text-white-50">${user.role === 'admin' ? 'Admin' : 'User'}</small>
                    </td>
                    <td>${statusBadge}</td>
                    <td><small>${formatAdminDate(user.createdAt)}</small></td>
                    <td><small>${formatAdminDate(user.lastLoginAt)}</small></td>
                    <td><small>${formatAdminDate(user.lastSeenAt)}</small></td>
                    <td>${Number(user.loginCount) || 0}</td>
                    <td>${actionHtml}</td>
                `;
                tbody.appendChild(tr);
            });

            if (!userRows.length) {
                tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted py-3">${TRANSLATIONS[currentLang].adminNoUsers}</td></tr>`;
            }
        }

        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, ch => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
            }[ch]));
        }

        async function setUserStatus(userKey, newStatus) {
            if (!currentUserKey) return;

            const adminSnap = await usersRef.child(currentUserKey).once('value');
            const adminData = adminSnap.val();
            if (!adminData || adminData.role !== 'admin' || adminData.status !== 'approved') {
                alert(TRANSLATIONS[currentLang].adminOnly);
                return;
            }

            const targetSnap = await usersRef.child(userKey).once('value');
            if (!targetSnap.exists()) return;
            const target = targetSnap.val();

            if (target.role === 'admin') {
                alert(TRANSLATIONS[currentLang].adminCannotChangeAdmin);
                return;
            }

            const now = new Date().toISOString();
            const updates = { status: newStatus };

            if (newStatus === 'approved') {
                updates.approvedAt = now;
                updates.approvedBy = adminData.username;
                updates.rejectedAt = null;
                updates.rejectedBy = null;
            } else if (newStatus === 'rejected') {
                updates.rejectedAt = now;
                updates.rejectedBy = adminData.username;
            }

            await usersRef.child(userKey).update(updates);

            // หากถูกปิดการใช้งาน ให้ปิด session ที่กำลัง active ของ user นั้นด้วย
            if (newStatus === 'disabled' || newStatus === 'rejected') {
                const sessionsSnap = await sessionsRef.once('value');
                const sessions = sessionsSnap.val() || {};
                const sessionUpdates = {};
                Object.entries(sessions).forEach(([sessionId, session]) => {
                    if (session && session.userKey === userKey && session.active !== false) {
                        sessionUpdates[`${sessionId}/active`] = false;
                        sessionUpdates[`${sessionId}/logoutAt`] = now;
                    }
                });
                if (Object.keys(sessionUpdates).length) {
                    await sessionsRef.update(sessionUpdates);
                }
            }

            await loadAdminUsers();
        }

        async function requireApprovedUser(){
            const uid=auth.currentUser && auth.currentUser.uid;
            if(!uid){await checkAuthSession();alert(TRANSLATIONS[currentLang].errAccountNotApproved);return false;}
            const snap=await usersRef.child(uid).once('value'); const user=snap.val();
            if(!user||user.status!=='approved'){await endUserSession(true);await checkAuthSession();alert(TRANSLATIONS[currentLang].errAccountNotApproved);return false;}
            currentUserKey=uid;currentUserData=user;return true;
        }

        // Firebase Authentication session listener
        auth.onAuthStateChanged(async (firebaseUser) => {
            if (firebaseUser) {
                await checkAuthSession(firebaseUser);
            } else {
                const dashboard=document.getElementById('mainDashboard');
                if(dashboard && dashboard.style.display==='block') await checkAuthSession(null);
            }
        });

        // --- 4. ระบบ Real-time Sync จาก Firebase ---
        const dashboardUsersCache = {};

        async function cacheCurrentUserProfile() {
            try {
                const u = auth.currentUser;
                if (!u) return;
                const snap = await usersRef.child(u.uid).once('value');
                if (snap.exists()) dashboardUsersCache[u.uid] = snap.val();
                renderTable();
            } catch (err) {
                console.warn('Could not cache current user profile:', err);
            }
        }

        function resolveRecordedBy(item) {
            const display = item.recordedByDisplayName || item.recorded_by_display_name || '';
            if (display && !['unknown','unknow','ไม่ระบุ'].includes(String(display).trim().toLowerCase())) return display;
            const raw = item.recordedBy || item.recorded_by || '';
            const uid = item.recordedByUserId || item.recorded_by_user_id || '';
            const profile = uid ? dashboardUsersCache[uid] : null;
            if (profile && profile.username) return profile.username;
            const authUser = auth.currentUser;
            if (uid && authUser && uid === authUser.uid) {
                if (currentUserData && currentUserData.username) return currentUserData.username;
                return authUser.email || 'สมาชิก';
            }
            if (raw && !['unknown','unknow','ไม่ระบุ'].includes(String(raw).trim().toLowerCase())) return raw;
            return 'ไม่ระบุ';
        }

        bossRef.on('value', (snapshot) => {
            const rawData = snapshot.val() || {};
            activeBosses = {};
            
            Object.keys(rawData).forEach(bossName => {
                const item = rawData[bossName];
                let spawnMs = item.spawnTimeMs;
                if (!spawnMs && item.spawn_time) {
                    spawnMs = new Date(item.spawn_time).getTime();
                }
                const cdSec = BOSS_DATABASE[bossName] ? BOSS_DATABASE[bossName].cd : 0;
                let killMs = item.killTimeMs || (spawnMs - (cdSec * 1000));
                
                activeBosses[bossName] = {
                    spawnTimeMs: spawnMs,
                    killTimeMs: killMs,
                    noticeMinutes: item.noticeMinutes || (BOSS_DATABASE[bossName] ? BOSS_DATABASE[bossName].notice : 5),
                    notifiedNotice: item.notified_advance || item.notifiedNotice || false,
                    notifiedSpawn: item.notifiedSpawn || false,
                    recordedBy: item.recordedBy || item.recorded_by || 'ไม่ระบุ',
                    recordedByDisplayName: item.recordedByDisplayName || item.recorded_by_display_name || '',
                    resolvedRecordedBy: resolveRecordedBy(item),
                    recordedByUserId: item.recordedByUserId || item.recorded_by_user_id || ''
                };
            });
            renderTable();
        });

        // --- UI & Control Functions ---
        function applyLanguage() {
            const langData = TRANSLATIONS[currentLang];
            document.querySelectorAll('[data-i18n]').forEach(el => {
                const key = el.getAttribute('data-i18n');
                if (langData[key]) el.innerText = langData[key];
            });
            document.querySelectorAll('[data-i18n-ph]').forEach(el => {
                const key = el.getAttribute('data-i18n-ph');
                if (langData[key]) el.placeholder = langData[key];
            });
            
            updateNotifyButtonUI();
            renderTable();
        }

        function changeLanguage(lang) {
            currentLang = lang;
            localStorage.setItem('app_lang', lang);

            const authLangSelect = document.getElementById('authLangSelect');
            const langSelect = document.getElementById('langSelect');
            if (authLangSelect) authLangSelect.value = lang;
            if (langSelect) langSelect.value = lang;

            applyLanguage();
        }

        function changeTimezone(tz) {
            currentTz = tz;
            localStorage.setItem('app_tz', tz);
            renderTable();
        }

        function updateNotifyButtonUI() {
            const notifyBtn = document.getElementById('notifyToggleBtn');
            const langData = TRANSLATIONS[currentLang];
            
            if (isNotifyEnabled) {
                notifyBtn.className = "btn btn-outline-success btn-sm fw-bold";
                notifyBtn.setAttribute('data-i18n', 'disableNotify');
                notifyBtn.innerText = langData.disableNotify;
            } else {
                notifyBtn.className = "btn btn-warning btn-sm fw-bold";
                notifyBtn.setAttribute('data-i18n', 'enableNotify');
                notifyBtn.innerText = langData.enableNotify;
            }
        }

        // --- Audio System ---
        let audioCtx = null;
        function playAlertSound(type) {
            if (!isNotifyEnabled) return;
            
            try {
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                if (audioCtx.state === 'suspended') audioCtx.resume();

                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.connect(gain);
                gain.connect(audioCtx.destination);

                if (type === 'notice') {
                    osc.type = 'sine';
                    osc.frequency.setValueAtTime(587.33, audioCtx.currentTime);
                    gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
                    osc.start();
                    osc.stop(audioCtx.currentTime + 0.15);
                    setTimeout(() => {
                        const osc2 = audioCtx.createOscillator();
                        const gain2 = audioCtx.createGain();
                        osc2.connect(gain2);
                        gain2.connect(audioCtx.destination);
                        osc2.type = 'sine';
                        osc2.frequency.setValueAtTime(880, audioCtx.currentTime);
                        gain2.gain.setValueAtTime(0.2, audioCtx.currentTime);
                        osc2.start();
                        osc2.stop(audioCtx.currentTime + 0.2);
                    }, 200);
                } else if (type === 'spawn') {
                    osc.type = 'sawtooth';
                    osc.frequency.setValueAtTime(880, audioCtx.currentTime);
                    osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.4);
                    gain.gain.setValueAtTime(0.3, audioCtx.currentTime);
                    osc.start();
                    osc.stop(audioCtx.currentTime + 0.4);
                }
            } catch (e) {
                console.log("Audio play error:", e);
            }
        }

        function toggleNotifications() {
            const langData = TRANSLATIONS[currentLang];
            
            if (isNotifyEnabled) {
                isNotifyEnabled = false;
                localStorage.setItem('notify_enabled', 'false');
                updateNotifyButtonUI();
                return;
            }

            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }

            if ("Notification" in window) {
                Notification.requestPermission().then(permission => {
                    if (permission === "granted") {
                        isNotifyEnabled = true;
                        localStorage.setItem('notify_enabled', 'true');
                        updateNotifyButtonUI();
                        playAlertSound('notice');
                        new Notification(langData.notifyReadyTitle, { body: langData.notifyReadyBody });
                    } else {
                        alert(langData.grantNotifyPrompt);
                    }
                });
            } else {
                alert(langData.noNotifySupport);
            }
        }

        function sendBrowserNotification(title, body) {
            if (!isNotifyEnabled) return;
            if ("Notification" in window && Notification.permission === "granted") {
                new Notification(title, { body: body, requireInteraction: true });
            }
        }

        function format24h(dateObj) {
            if (!dateObj || isNaN(dateObj.getTime())) return "00:00:00";
            const options = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, hourCycle: 'h23' };
            if (currentTz && currentTz !== 'auto') options.timeZone = currentTz;

            const formatter = new Intl.DateTimeFormat('en-GB', options);
            const parts = formatter.formatToParts(dateObj);
            let h = '00', m = '00', s = '00';
            for (const part of parts) {
                if (part.type === 'hour') h = part.value;
                if (part.type === 'minute') m = part.value;
                if (part.type === 'second') s = part.value;
            }
            return `${h}:${m}:${s}`;
        }

        function parseInputTime(timeStr) {
            if (!timeStr) return new Date();
            let h, m;
            if (timeStr.includes(':')) {
                const parts = timeStr.split(':');
                h = parseInt(parts[0], 10);
                m = parseInt(parts[1], 10);
            } else if (timeStr.length === 3) {
                h = parseInt(timeStr.substring(0, 1), 10);
                m = parseInt(timeStr.substring(1, 3), 10);
            } else if (timeStr.length === 4) {
                h = parseInt(timeStr.substring(0, 2), 10);
                m = parseInt(timeStr.substring(2, 4), 10);
            } else {
                return null;
            }

            if (isNaN(h) || isNaN(m) || h < 0 || h > 23 || m < 0 || m > 59) return null;

            const now = new Date();
            let d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0);
            
            if (d.getTime() > now.getTime() + 60000) {
                d.setDate(d.getDate() - 1);
            }
            return d;
        }

        document.getElementById('bossForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!await requireApprovedUser()) return;
            const bossInput = document.getElementById('bossSelect').value.trim();
            const timeInput = document.getElementById('killTime').value.trim();
            const noticeMin = parseInt(document.getElementById('noticeMinutes').value, 10) || 5;
            const spTimeMin = parseInt(document.getElementById('spTime').value, 10) || 0;
            if (!bossInput) return;

            try {
                const idToken = await auth.currentUser.getIdToken(true);
                // Dashboard may be hosted on GitHub Pages, while the API runs on Render.
                // Use the Render API origin explicitly so recording works from either host.
                const apiOrigin = window.SKYNET_API_ORIGIN || 'https://bosstimer-ry18.onrender.com';
                const response = await fetch(`${apiOrigin}/api/record-boss`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${idToken}`
                    },
                    body: JSON.stringify({
                        bossName: bossInput,
                        killTime: timeInput,
                        noticeMinutes: noticeMin,
                        spTimeMinutes: spTimeMin,
                        channelId: null
                    })
                });
                const result = await response.json().catch(() => ({}));
                if (!response.ok || !result.success) {
                    throw new Error(result.error || `HTTP ${response.status}`);
                }
                document.getElementById('bossForm').reset();
                document.getElementById('noticeMinutes').value = 5;
                // Update the UI immediately from the authoritative backend response.
                if (result.bossName) {
                    const fallbackKill = Number(result.killTimeMs) || Date.now();
                    const fallbackSpawn = Number(result.spawnTimeMs) || fallbackKill;
                    activeBosses[result.bossName] = {
                        spawnTimeMs: fallbackSpawn,
                        killTimeMs: fallbackKill,
                        noticeMinutes: noticeMin,
                        notifiedNotice: !!result.alreadyPassed,
                        notifiedSpawn: !!result.alreadyPassed,
                        recordedBy: result.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || 'ไม่ระบุ',
                        recordedByDisplayName: result.recordedByDisplayName || result.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || 'ไม่ระบุ',
                        recordedByUserId: result.recordedByUserId || auth.currentUser?.uid || '',
                        resolvedRecordedBy: result.recordedBy || (currentUserData && currentUserData.username) || auth.currentUser?.email || 'ไม่ระบุ'
                    };
                    renderTable();
                }
                console.log(`✅ Boss recorded: ${result.bossName} | by ${result.recordedBy} | uid=${result.recordedByUserId} | confirmation=${result.confirmationRequestId}`);
            } catch (err) {
                console.error('Dashboard boss record failed:', err);
                alert(`❌ บันทึกเวลาบอสไม่สำเร็จ\n${err.message || err}`);
            }
        });

        async function deleteBoss(bossName) {
            if (!await requireApprovedUser()) return;
            await bossRef.child(bossName).remove();
        }

        document.getElementById('clearAllBtn').addEventListener('click', async () => {
            if (!await requireApprovedUser()) return;
            if (confirm(TRANSLATIONS[currentLang].confirmClear)) {
                await bossRef.remove();
            }
        });

        function renderTable() {
            const tbody = document.getElementById('bossTableBody');
            tbody.innerHTML = '';
            const langData = TRANSLATIONS[currentLang];
            const sortedBosses = Object.keys(activeBosses).sort((a, b) => activeBosses[a].spawnTimeMs - activeBosses[b].spawnTimeMs);

            if (sortedBosses.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted py-3">${langData.emptyMsg}</td></tr>`;
                return;
            }

            sortedBosses.forEach(bossName => {
                const data = activeBosses[bossName];
                const tr = document.createElement('tr');
                const killTimeStr = format24h(new Date(data.killTimeMs));
                const spawnTimeStr = format24h(new Date(data.spawnTimeMs));
                
                tr.innerHTML = `
                    <td class="fw-bold text-warning">${escapeHtml(bossName)}</td>
                    <td>${killTimeStr}</td>
                    <td class="text-info">${spawnTimeStr}</td>
                    <td id="cd-${bossName}" class="fw-bold">--:--:--</td>
                    <td>${data.noticeMinutes} ${langData.minUnit}</td>
                    <td><span class="badge bg-secondary">${escapeHtml(resolveRecordedBy(data) || data.recordedBy || 'ไม่ระบุ')}</span></td>
                    <td>
                        <button class="btn btn-sm btn-danger" onclick="deleteBoss('${bossName}')">${langData.btnDelete}</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
            updateCountdowns();
        }

        function updateCountdowns() {
            const nowMs = new Date().getTime();
            document.getElementById('liveClockDisplay').innerText = format24h(new Date());

            Object.keys(activeBosses).forEach(bossName => {
                const data = activeBosses[bossName];
                const diffMs = data.spawnTimeMs - nowMs;
                const cdCell = document.getElementById(`cd-${bossName}`);
                
                if (!cdCell) return;

                if (diffMs <= 0) {
                    cdCell.innerHTML = `<span class="text-success">${TRANSLATIONS[currentLang].spawned}</span>`;
                    if (!browserNotified.has(`${bossName}|${data.spawnTimeMs}|spawn`)) {
                        playAlertSound('spawn');
                        const title = (TRANSLATIONS[currentLang].spawnNotifyTitle || "⚔️ {boss} Spawned!").replace('{boss}', bossName);
                        const body = (TRANSLATIONS[currentLang].spawnNotifyBody || "Boss {boss} has spawned!").replace('{boss}', bossName);
                        sendBrowserNotification(title, body);
                        browserNotified.add(`${bossName}|${data.spawnTimeMs}|spawn`);
                    }
                } else {
                    const totalSec = Math.floor(diffMs / 1000);
                    const h = Math.floor(totalSec / 3600);
                    const m = Math.floor((totalSec % 3600) / 60);
                    const s = totalSec % 60;
                    
                    cdCell.innerText = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
                    
                    const noticeMs = data.noticeMinutes * 60 * 1000;
                    if (diffMs <= noticeMs && diffMs > noticeMs - 5000 && !browserNotified.has(`${bossName}|${data.spawnTimeMs}|notice`)) {
                         playAlertSound('notice');
                         const title = (TRANSLATIONS[currentLang].noticeNotifyTitle || "⏳ {boss} Spawning Soon!").replace('{boss}', bossName);
                         const body = (TRANSLATIONS[currentLang].noticeNotifyBody || "Boss {boss} will spawn in {min} mins").replace('{boss}', bossName).replace('{min}', data.noticeMinutes);
                         sendBrowserNotification(title, body);
                         browserNotified.add(`${bossName}|${data.spawnTimeMs}|notice`);
                    }
                }
            });
        }

        function initApp() {
            const dataList = document.getElementById('bossOptions');
            Object.keys(BOSS_DATABASE).sort().forEach(boss => {
                const option = document.createElement('option');
                option.value = boss;
                dataList.appendChild(option);
            });

            document.getElementById('langSelect').value = currentLang;
            const authLangSelect = document.getElementById('authLangSelect');
            if (authLangSelect) authLangSelect.value = currentLang;
            
            document.getElementById('tzSelect').value = currentTz;
            
            applyLanguage();
            auth.onAuthStateChanged(() => checkAuthSession());
            checkAuthSession();

            setInterval(updateCountdowns, 1000);
            setInterval(() => {
                if (currentUserData && currentUserData.role === 'admin') loadAdminUsers();
            }, 30000);
        }

        window.addEventListener('beforeunload', () => {
            const sessionId = localStorage.getItem('logged_session_id');
            if (sessionId) {
                sessionsRef.child(sessionId).update({
                    lastSeenAt: new Date().toISOString(),
                    active: false,
                    logoutAt: new Date().toISOString()
                });
            }
        });

        window.addEventListener('DOMContentLoaded', initApp);
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

        # Queue confirmation immediately from the Bot process. The Firebase listener
        # remains as a safety net but this removes timing dependence on the listener.
        confirmation_result = False
        confirmation_pending = False
        try:
            confirmation_result = queue_voice_confirmation(
                canonical_name, dict(boss_schedule[canonical_name]), source='dashboard', wait=True, timeout=180
            )
            # None means the save is valid but Discord is not READY yet. Keep the
            # request pending and let on_ready drain it instead of showing a false failure.
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
custom_bosses = {}
last_voice_connect_attempt = {}
last_channel_fetch_attempt = {}
# Prevent overlapping boss notification passes (e.g. scheduled loop + manual /kill check).
boss_notification_pass_lock = asyncio.Lock()

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


def _discord_rest_rate_limit_remaining() -> float:
    return max(0.0, discord_rest_rate_limited_until - time.monotonic())


def _extract_discord_retry_after(exc: Exception) -> float:
    retry_after = 0.0
    try:
        retry_after = float(getattr(exc, "retry_after", 0) or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    if retry_after <= 0:
        try:
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None) or {}
            raw = headers.get("Retry-After") or headers.get("retry-after")
            retry_after = float(raw or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
    return max(0.0, retry_after)


def _apply_discord_rest_429(exc: Exception, *, context: str) -> float:
    global discord_rest_rate_limited_until, discord_rest_backoff_seconds, discord_rest_last_429_log
    retry_after = _extract_discord_retry_after(exc)
    if retry_after <= 0:
        retry_after = discord_rest_backoff_seconds
        discord_rest_backoff_seconds = min(discord_rest_backoff_seconds * 2.0, 900.0)
    else:
        # Trust Discord's value.  The extra guard backoff is only used when
        # Discord omits Retry-After.  Never shorten an already-active block.
        discord_rest_backoff_seconds = min(max(60.0, retry_after * 2.0), 900.0)

    until = time.monotonic() + max(1.0, retry_after)
    discord_rest_rate_limited_until = max(discord_rest_rate_limited_until, until)

    now_mono = time.monotonic()
    if now_mono - discord_rest_last_429_log >= 5.0:
        discord_rest_last_429_log = now_mono
        print(
            f"⏸️ GLOBAL Discord REST 429 cooldown | context={context} | "
            f"wait={retry_after:.1f}s | all non-essential REST paused",
            flush=True,
        )
    return retry_after


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


async def guarded_discord_call(call_factory, *, context: str, wait_for_cooldown: bool = False):
    """Run a Discord REST call through one process-wide guard.

    wait_for_cooldown=False is intentional for background tasks: when Discord is
    already blocking REST, skipping the request is safer than sleeping inside a
    scheduler/notification task.  Voice/TTS calls never enter this function.
    """
    global discord_rest_next_call_at
    remaining = _discord_rest_rate_limit_remaining()
    if remaining > 0:
        if not wait_for_cooldown:
            _log_rest_skip(context, remaining)
            return None
        # Only command code that explicitly asks to wait may sleep here.
        await asyncio.sleep(remaining)

    async with discord_rest_guard_lock:
        remaining = _discord_rest_rate_limit_remaining()
        if remaining > 0:
            if not wait_for_cooldown:
                print(
                    f"⏭️ Discord REST skipped during global cooldown | context={context} | "
                    f"remaining={remaining:.1f}s",
                    flush=True,
                )
                return None
            await asyncio.sleep(remaining)

        # Small spacing between all guarded REST requests prevents bursts even
        # after a cooldown expires.  This is intentionally far below Discord's
        # documented global 50 req/s ceiling.
        now = time.monotonic()
        if discord_rest_next_call_at > now:
            await asyncio.sleep(discord_rest_next_call_at - now)
        if discord_rest_min_interval > 0:
            discord_rest_next_call_at = time.monotonic() + discord_rest_min_interval

        try:
            result = await call_factory()
            _clear_discord_rest_backoff_after_success()
            return result
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                _apply_discord_rest_429(exc, context=context)
            else:
                _apply_discord_rest_error(exc, context=context)
            raise
        except Exception:
            # Do not turn application exceptions into a fake Discord rate-limit.
            raise


async def guarded_channel_send(channel, *, context: str, content=None, embed=None):
    return await guarded_discord_call(
        lambda: channel.send(content=content, embed=embed),
        context=context,
    )


async def guarded_fetch_channel(channel_id: int, *, context: str):
    return await guarded_discord_call(
        lambda: bot.fetch_channel(int(channel_id)),
        context=context,
    )


async def guarded_fetch_message(channel, message_id: int, *, context: str):
    return await guarded_discord_call(
        lambda: channel.fetch_message(int(message_id)),
        context=context,
    )


async def guarded_message_edit(message, *, context: str, **kwargs):
    return await guarded_discord_call(
        lambda: message.edit(**kwargs),
        context=context,
    )


async def guarded_context_send(ctx, *args, context: str, **kwargs):
    return await guarded_discord_call(
        lambda: ctx.send(*args, **kwargs),
        context=context,
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
    return await _interaction_webhook_call(
        lambda: interaction.followup.send(*args, **kwargs),
        context=context,
    )


async def guarded_interaction_edit_original(interaction: discord.Interaction, context: str, *args, **kwargs):
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

    try: await guarded_channel_send(log_channel, context=f"audit:{action}", embed=embed)
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
        return True
    try:
        result = future.result(timeout=float(timeout))
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
    """Speak one-shot confirmation for a newly recorded boss time in occupied Voice rooms only."""
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

    try:
        await asyncio.to_thread(db.reference(f"boss_schedule/{boss_name}").update, {"confirmationStatus": "processing"})
    except Exception as exc:
        print(f"⚠️ Could not mark confirmation processing: {boss_name}: {exc}")

    spoken_name = get_boss_pronunciation(boss_name)
    recorded_by = str(data.get("recordedBy") or data.get("recorded_by") or "").strip()
    if not recorded_by or recorded_by.lower() in {"unknown", "unknow", "ไม่ระบุ"}:
        recorded_by = "สมาชิก"

    success = False
    try:
        await refresh_tts_settings_from_firebase()
        # User explicitly requested a confirmation after saving; use the Thai text
        # and the currently enabled languages for the same TTS policy.
        text_th = f"บันทึกเวลาบอส {spoken_name} สำเร็จแล้วค่ะ"
        text_en = f"Boss {boss_name} time saved successfully."
        text_ko = f"보스 {boss_name} 시간이 성공적으로 저장되었습니다."
        for guild in list(bot.guilds):
            configured_channels = get_configured_voice_channels(guild)
            if not configured_channels:
                print(f"⚠️ Boss confirmation skipped: no /setvoice targets | guild={guild.name}")
                continue
            for configured in configured_channels:
                humans = [m for m in configured.members if not m.bot]
                if not humans:
                    print(f"⏭️ Boss confirmation skipped: configured Voice is empty | guild={guild.name} | channel={configured.name}")
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
            await asyncio.to_thread(
                db.reference(f"boss_schedule/{boss_name}").update,
                {"confirmationStatus": "sent" if success else "failed"}
            )
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
                canonical = get_boss_canonical_name(boss_name)
                internal = _firebase_to_internal(canonical, data)
                if internal:
                    new_schedule[canonical] = internal
            with schedule_lock:
                previous = dict(boss_schedule)
                boss_schedule.clear()
                boss_schedule.update(new_schedule)

            # Trigger one-shot confirmation only for newly requested recordings.
            for boss_name, item in new_schedule.items():
                req_id = str(item.get("confirmationRequestId") or "").strip()
                if not req_id or req_id in _confirmation_seen_ids:
                    continue
                prev_id = str((previous.get(boss_name) or {}).get("confirmationRequestId") or "").strip()
                _confirmation_seen_ids.add(req_id)
                if req_id != prev_id and str(item.get("confirmationStatus") or "pending") in ("", "pending"):
                    queue_voice_confirmation(boss_name, item, source='firebase-listener')
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

async def save_bot_settings():
    """Persist notification/TTS switches to Firebase and local SQLite."""
    data = {
        "bf_notify_enabled": bool(bf_notify_enabled),
        "lib_notify_enabled": bool(lib_notify_enabled),
        "ppl_notify_enabled": bool(ppl_notify_enabled),
        "tts_th_enabled": bool(tts_th_enabled),
        "tts_en_enabled": bool(tts_en_enabled),
        "tts_ko_enabled": bool(tts_ko_enabled),
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
    """Generate TTS with live Firebase settings and bounded retries.
    A transient Edge TTS "No audio was received" error must not make /notice silently fail.
    """
    await refresh_tts_settings_from_firebase()
    actual = []
    if tts_th_enabled and text_th:
        actual.append(("th", text_th, VOICE_THAI, "-20%", "+10Hz"))
    if tts_en_enabled and text_en:
        actual.append(("en", text_en, VOICE_ENG, "-10%", "+0Hz"))
    if tts_ko_enabled and text_ko:
        actual.append(("ko", text_ko, VOICE_KOR, "-10%", "+0Hz"))

    files = []
    uid = uuid.uuid4().hex
    for lang, text, voice, rate, pitch in actual:
        filename = f"temp_tts_{lang}_{guild_id}_{uid}.mp3"
        success = False
        last_error = None
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
                    print(f"🔊 TTS สร้างไฟล์สำเร็จ: {lang} ({guild_id}) attempt={attempt}")
                    success = True
                    break
                last_error = RuntimeError("No audio was received")
            except Exception as e:
                last_error = e
                print(f"⚠️ TTS attempt {attempt}/3 failed ({lang}): {e}")
                await asyncio.sleep(0.8 * attempt)
        if not success:
            print(f"❌ สร้าง TTS ไม่สำเร็จ ({lang}) หลังลอง 3 ครั้ง: {last_error}")
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
            channels = [target_channel]
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
    global is_bot_ready, bot_event_loop
    bot_event_loop = asyncio.get_running_loop()
    if is_bot_ready:
        print("🔄 บอท Reconnect สำเร็จ (ข้ามการโหลดข้อมูลซ้ำ)")
        return
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🔊 ใช้ FFmpeg จากตำแหน่ง: {get_ffmpeg_path()}")

    init_db()
    await load_bot_settings()
    print(f"🔐 Startup TTS settings: TH={tts_th_enabled} EN={tts_en_enabled} KO={tts_ko_enabled}")
    print(f"🔔 Startup notification settings: BF={bf_notify_enabled} LIB={lib_notify_enabled} PPL={ppl_notify_enabled}")
    await load_custom_bosses()
    await load_boss_data()
    await load_live_config()
    await load_vip_config()
    await load_voice_config()

    # Voice is ON-DEMAND: do not connect on startup. /setvoice only stores the target channel.
    print("🟢 Voice mode: ON-DEMAND GLOBAL (occupied-room announcements; connect only when speaking, disconnect after TTS)")

    await asyncio.sleep(3)
    
    if not check_boss_notifications.is_running(): check_boss_notifications.start()
    if not check_bf_notifications.is_running(): check_bf_notifications.start()
    if not check_library_boss_notifications.is_running(): check_library_boss_notifications.start()
    if not update_live_embed.is_running(): update_live_embed.start()
    if not check_auto_disconnect.is_running(): check_auto_disconnect.start()

    is_bot_ready = True
    loop = bot_event_loop
    threading.Thread(target=start_firebase_listener, args=(loop,), daemon=True).start()

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
            print(
                f"⚠️ Interaction ACK rejected by Discord 429 | retry_after={retry_after:.2f}s | "
                "shared REST cooldown unchanged",
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
            print(
                f"⚠️ Interaction initial response rejected by Discord 429 | retry_after={retry_after:.2f}s | "
                "shared REST cooldown unchanged",
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
                    embed = discord.Embed(
                        title="⚔️ แจ้งเตือนสงคราม Battlefield (BF)!",
                        description=f"สนามรบ **BF** กำลังจะเริ่มในอีก **3 นาที** (เวลา **{next_bf_time} น.**)!\nเตรียมตัวเข้าประจำที่ได้เลยครับ!",
                        color=discord.Color.red()
                    )
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
                        embed = discord.Embed(
                            title="⚔️ แจ้งเตือน Library Boss!",
                            description=f"บอส **Library Boss** ถึงเวลาเตรียมตัวแล้ว! (เวลา **{time_str}**)\nเตรียมตัวเข้าประจำที่ได้เลยครับ!",
                            color=discord.Color.purple()
                        )
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
            advance_text_due = (0 < time_left <= notice_seconds and not notified_advance and not rest_blocked)
            advance_voice_due = (0 < time_left <= notice_seconds and not voice_advance)
            spawn_text_due = (time_left <= 0 and not notified_spawn and not rest_blocked)
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

            channel = None
            channel_id = data.get("channel_id") or data.get("channelId")
            if channel_id:
                try:
                    channel = bot.get_channel(int(channel_id))
                    if channel is None:
                        channel = await bot.fetch_channel(int(channel_id))
                except Exception:
                    channel = None

            channels_to_notify = [channel] if channel else []
            if not channels_to_notify:
                for guild in bot.guilds:
                    fb_channel = discord.utils.get(guild.text_channels, name=LIVE_CHANNEL_NAME)
                    if not fb_channel:
                        fb_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                    if fb_channel:
                        channels_to_notify.append(fb_channel)

            # Voice notifications must not depend on text-channel resolution or REST availability.
            # Always evaluate configured /setvoice targets directly from the READY guild cache.
            target_guilds = set(bot.guilds)

            # Advance: text and voice are independent one-shot states.
            if 0 < time_left <= notice_seconds and not notified_advance:
                embed = discord.Embed(
                    title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
                    description=f"บอส **{boss_name}** จะเกิดในอีก **{notice_minutes} นาที**!\nเวลาเกิด: **{spawn_time.strftime('%H:%M:%S น.')}**",
                    color=discord.Color.gold()
                )
                text_sent = False
                for ch in channels_to_notify:
                    try:
                        mentions = get_notification_mentions(getattr(ch, "guild", None))
                        send_result = await guarded_channel_send(ch, context=f"boss-notify:{boss_name}", content=mentions or None, embed=embed)
                        if send_result is not None:
                            text_sent = True
                    except Exception as e:
                        print(f"❌ ส่งข้อความ advance ไม่สำเร็จ ({boss_name}): {e}")
                if text_sent:
                    await save_boss_notification_flags(boss_name, notified_advance=True)
                    notified_advance = True
                    print(f"🟢 Advance notification sent: {boss_name}")

            if 0 < time_left <= notice_seconds and not voice_advance:
                spoken_name = get_boss_pronunciation(boss_name)
                all_ok = True
                spoken_rooms = 0
                for guild in target_guilds:
                    configured_rooms = get_configured_voice_channels(guild)
                    rooms = [r for r in configured_rooms if any(not m.bot for m in r.members)]
                    if not rooms:
                        print(f"⏭️ Boss VOICE WAIT | guild={guild.name} | configured={len(configured_rooms)} | occupied=0 | stage=advance")
                    for room in rooms:
                        try:
                            result = await asyncio.wait_for(
                                speak_in_guild(
                                    guild,
                                    text_th=f"บอส {spoken_name} จะเกิดในอีก {notice_minutes} นาทีค่ะ",
                                    text_en=f"Boss {boss_name} will spawn in {notice_minutes} minutes.",
                                    text_ko=f"보스 {boss_name}가 {notice_minutes}분 후에 나타납니다.",
                                    target_channel=room
                                ),
                                timeout=180
                            )
                            spoken_rooms += 1
                            if result is False:
                                all_ok = False
                        except Exception as e:
                            all_ok = False
                            print(f"⚠️ Advance TTS failed ({boss_name}/{guild.name}/{room.name}): {e}")
                if spoken_rooms > 0 and all_ok:
                    await save_boss_notification_flags(boss_name, voice_notice_sent=True)
                    voice_advance = True
                    print(f"🔊 Advance TTS sent: {boss_name} -> {spoken_rooms} occupied room(s)")

            # Spawn: only notify at the actual crossing. Old schedules >60s late
            # are marked complete instead of replaying after every deploy/reload.
            if time_left <= 0 and not notified_spawn:
                embed = discord.Embed(
                    title="⚔️ บอสเกิดแล้ว!",
                    description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
                    color=discord.Color.green()
                )
                text_sent = False
                for ch in channels_to_notify:
                    try:
                        mentions = get_notification_mentions(getattr(ch, "guild", None))
                        send_result = await guarded_channel_send(ch, context=f"boss-notify:{boss_name}", content=mentions or None, embed=embed)
                        if send_result is not None:
                            text_sent = True
                    except Exception as e:
                        print(f"❌ ส่งข้อความ spawn ไม่สำเร็จ ({boss_name}): {e}")
                if text_sent:
                    await save_boss_notification_flags(boss_name, notified_spawn=True)
                    notified_spawn = True
                    print(f"🟢 Spawn notification sent: {boss_name}")

            if -120 <= time_left <= 0 and not voice_spawn:
                spoken_name = get_boss_pronunciation(boss_name)
                all_ok = True
                spoken_rooms = 0
                for guild in target_guilds:
                    configured_rooms = get_configured_voice_channels(guild)
                    rooms = [r for r in configured_rooms if any(not m.bot for m in r.members)]
                    if not rooms:
                        print(f"⏭️ Boss VOICE WAIT | guild={guild.name} | configured={len(configured_rooms)} | occupied=0 | stage=spawn")
                    for room in rooms:
                        try:
                            result = await asyncio.wait_for(
                                speak_in_guild(
                                    guild,
                                    text_th=f"บอส {spoken_name} เกิดแล้วค่ะ",
                                    text_en=f"Boss {boss_name} has spawned.",
                                    text_ko=f"보스 {boss_name}가 나타났습니다.",
                                    target_channel=room
                                ),
                                timeout=180
                            )
                            spoken_rooms += 1
                            if result is False:
                                all_ok = False
                        except Exception as e:
                            all_ok = False
                            print(f"⚠️ Spawn TTS failed ({boss_name}/{guild.name}/{room.name}): {e}")
                if spoken_rooms > 0 and all_ok:
                    await save_boss_notification_flags(boss_name, voice_spawn_sent=True)
                    voice_spawn = True
                    print(f"🔊 Spawn TTS sent: {boss_name} -> {spoken_rooms} occupied room(s)")
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

@bot.tree.command(name="time", description="คำนวณเวลาที่เหลือของบอสทุกตัว เรียงจากน้อยไปมาก และส่งเสียงอ่าน TTS ในห้องเสียง")
async def boss_time_slash(interaction: discord.Interaction):
    # Initial interaction ACK is time-critical and must remain isolated from the
    # background REST circuit breaker.  A Discord 429 must not crash the command.
    ack_ok = await _safe_interaction_ack(interaction, ephemeral=True)
    if not ack_ok:
        print("⚠️ /time ACK unavailable; continuing summary/TTS work safely", flush=True)

    try:
        embed, tts_text_th, tts_text_en, tts_text_ko = generate_boss_time_summary()
        if embed is None:
            if ack_ok:
                try:
                    await guarded_interaction_followup_send(
                        interaction, "interaction-followup", tts_text_th
                    )
                except discord.HTTPException as exc:
                    print(f"⚠️ /time result response failed: {exc}", flush=True)
            else:
                print("ℹ️ /time summary generated without interaction ACK", flush=True)
            return

        if ack_ok:
            try:
                await guarded_interaction_followup_send(
                    interaction, "interaction-followup", embed=embed
                )
            except discord.HTTPException as exc:
                print(f"⚠️ /time embed response failed: {exc}", flush=True)
        else:
            print("ℹ️ /time embed generated without interaction ACK", flush=True)

        # Keep the original Voice/TTS behavior unchanged.
        asyncio.create_task(
            speak_in_guild(
                interaction.guild,
                text_th=tts_text_th,
                text_en=tts_text_en,
                text_ko=tts_text_ko,
            )
        )
        await send_audit_log(
            interaction.guild,
            interaction.user,
            "เช็กเวลาบอสพร้อม TTS (/time)",
            "คำนวณสรุปเวลาบอสเรียงจากน้อยไปมากและส่งเสียงอ่านเรียบร้อย",
            discord.Color.purple(),
        )
    except Exception as exc:
        # Never let a REST/interaction error escape the slash-command callback.
        print(f"❌ /time command failed safely: {exc!r}", flush=True)
        if ack_ok:
            try:
                await guarded_interaction_followup_send(
                    interaction,
                    "interaction-followup",
                    "⚠️ ไม่สามารถส่งผลลัพธ์ /time กลับไปใน Discord ได้ในขณะนี้",
                    ephemeral=True,
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
    kill_date="วันที่บอสตาย DD/MM/YYYY (เว้นว่าง = วันนี้)"
)

@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str = None, kill_date: str = None):
    # Acknowledge immediately.
    try:
        await _safe_interaction_ack(interaction, ephemeral=False)
    except Exception as e:
        print(f"❌ /kill defer failed: {e}")
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
            await guarded_interaction_followup_send(interaction, "interaction-followup", 
                "❌ วันที่/เวลาไม่ถูกต้อง! วันที่ใช้รูปแบบ **DD/MM/YYYY** เช่น **29/08/2026** และเวลาใช้ **17:30** หรือ **1730**",
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

        # Send Discord response before any Firebase/local I/O.
        await guarded_interaction_followup_send(interaction, "interaction-followup", embed=embed)

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
        print(f"❌ /kill unexpected error: {e}")
        traceback.print_exc()
        try:
            await guarded_interaction_followup_send(interaction, "interaction-followup", f"❌ /kill เกิดข้อผิดพลาด: `{e}`", ephemeral=True)
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
    # Initial interaction ACK is time-critical and intentionally isolated from
    # background REST cooldown.  If Discord rejects it (for example during a
    # temporary API restriction), we still complete the Firebase write but do
    # not generate additional followup/audit REST traffic that is known to fail.
    ack_ok = await _safe_interaction_ack(interaction, ephemeral=False)
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

@bot.tree.command(name="attendance", description="แจ้งเตือนเช็คชื่อบอสพร้อมโค้ดและไอเทมดรอป")
@app_commands.describe(
    boss_name="ชื่อบอสที่ต้องการเช็คชื่อ",
    code="โค้ดสำหรับเช็คชื่อ (Code)",
    drop_item="ไอเทมที่ดรอป (Drop Item)"
)
@has_allowed_role()
async def attendance_command(interaction: discord.Interaction, boss_name: str, code: str, drop_item: str):
    await _safe_interaction_ack(interaction, ephemeral=False)
    
    embed = discord.Embed(
        title="📢 แจ้งเตือนเช็คชื่อบอส (Attendance)",
        color=discord.Color.green(),
        timestamp=datetime.now(TZ_THAI)
    )
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="🔑 โค้ด (Code)", value=f"**{code}**", inline=True)
    embed.add_field(name="🎁 ไอเทมดรอป", value=f"`{drop_item}`", inline=False)
    embed.set_footer(text=f"ประกาศโดย {interaction.user.display_name}")
    
    await guarded_interaction_followup_send(interaction, "interaction-followup", content="✅ ส่งประกาศเช็คชื่อสำเร็จ!", embed=embed)
    
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
                await guarded_channel_send(attendance_channel, context="audit:boss-attendance", embed=log_embed)
            except Exception as e:
                print(f"❌ ส่ง Audit Log ใน boss-attendance ไม่สำเร็จ: {e}")

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
        "update_live_embed", "check_auto_disconnect", "boss_time_slash",
    ]
    missing = [name for name in required_funcs if not callable(globals().get(name))]
    if missing:
        raise RuntimeError("V40 integrity check failed; missing functions: " + ", ".join(missing))
    direct = [cmd.name for cmd in bot.tree.get_commands() if isinstance(cmd, app_commands.Command)]
    if len(direct) != 16:
        raise RuntimeError(f"V40 integrity check failed; expected 16 direct slash commands, found {len(direct)}")
    group = next((cmd for cmd in bot.tree.get_commands() if isinstance(cmd, app_commands.Group) and cmd.name == "add"), None)
    if group is None or not any(sub.name == "boss" for sub in group.commands):
        raise RuntimeError("V40 integrity check failed; /add boss subcommand missing")
    print("✅ V40 integrity check passed | 16 direct + /add boss = 17 command paths", flush=True)


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
            # Retry paths close only the HTTP transport; keep the Bot lifecycle reusable.
            pass
    else:
        print("⚠️ กรุณาตั้งค่า DISCORD_TOKEN ใน Environment Variable หรือระบุ Token สำหรับรันบอท")
