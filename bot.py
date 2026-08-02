import os
import json
import threading
import requests
import base64
import asyncio
import time
import shutil
import traceback
import logging
import re  # 💡 เพิ่ม re สำหรับล้างตัวอักษรพิเศษและอีโมจิ
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, render_template_string
from waitress import serve
import edge_tts  # เปลี่ยนจาก gTTS มาใช้ edge-tts เพื่อแก้ปัญหา 429 Too Many Requests
import imageio_ffmpeg

# ==========================================
# ⚙️ ซ่อน Log แจ้งเตือนที่ไม่จำเป็นจาก Discord.py
# ==========================================
logging.getLogger('discord.player').setLevel(logging.WARNING)
logging.getLogger('discord.voice_state').setLevel(logging.WARNING)

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
    
    schedule_copy = boss_schedule.copy()
    sorted_bosses = sorted(schedule_copy.items(), key=lambda x: x[1]["spawn_time"])
    
    for boss_name, data in sorted_bosses:
        spawn_time = data["spawn_time"]
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
# ⚙️ 2. ตั้งค่า Timezone ไทย & Config
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))
DATA_FILE = "boss_data.json"
CUSTOM_BOSSES_FILE = "custom_bosses.json"
LIVE_CONFIG_FILE = "live_config.json"
VIP_CONFIG_FILE = "vip_config.json"

TARGET_ROLE_NAMES = ["Eternal", "Meaw", "Anti", "Admin"]
BF_ROLE_NAMES = ["Eternal", "Meaw", "Anti"]  # ยศสำหรับแท็กแจ้งเตือน BF

LOG_CHANNEL_NAME = "boss-logs"
LIVE_CHANNEL_NAME = "boss-schedule"

voice_empty_start = {}
voice_locks = {}

# ตัวแปรสถานะการแจ้งเตือน BF และการแจ้งเตือนเข้าห้องเสียง
bf_notify_enabled = True
ppl_notify_enabled = True  # สถานะสำหรับควบคุมการแจ้งเตือนเสียงสมาชิกเข้าห้องแบบปกติ
vip_config = {"enabled": False, "user_id": None, "user_name": "", "message": ""} # ข้อมูลทักทายคนพิเศษ
last_bf_notified_hour = -1

# ตั้งค่าเสียงอ่านภาษาไทยของ Microsoft Edge TTS
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
    "Billiard": timedelta(hours=7, minutes=55, seconds=3)
}

DEFAULT_BOSS_NAMES = set(BOSS_RESPAWN_TIMES.keys())

BOSS_CD_TEXT = {
    "Wadangka": "2 ชั่วโมง 30 นาที", "Elemental Queen": "2 ชั่วโมง 30 นาที",
    "Tank": "58 นาที 20 วินาที", "Swirl Flame": "58 นาที 20 วินาที",
    "Maelstrom": "58 นาที 20 วินาที", "Twister": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง", "Chief Magief": "30 นาที",
    "Faith": "5 ชั่วโมง 53 นาที", "Apapa": "15 นาที",
    "Corrupt Forest Keeper": "58 นาที", "Recluse": "11 ชั่วโมง 23 นาที",
    "Blackskull": "56 นาที 50 วินาที", "Sleepy Kooii": "20 นาที",
    "Awaken Kooii": "1 ชั่วโมง 3 นาที", "Eeheehee": "1 ชั่วโมง 6 นาที 48 วินาที",
    "Ooheeheek": "1 ชั่วโมง 8 นาที 3 วินาที", "Oohehe": "1 ชั่วโมง 5 นาที 8 วินาที",
    "Guardian Imp": "1 ชั่วโมง 3 นาที", "Devilang": "5 ชั่วโมง 33 นาที",
    "Blackjuno": "35 นาที", "Blacksky": "35 นาที",
    "Red Fox": "20 นาที", "7tailfox": "20 นาที",
    "777Tailfox": "30 นาที", "Sunrise Flower": "20 นาที",
    "Magma Senior Thief": "20 นาที", "Bbinikjoe": "20 นาที",
    "Bigmouse": "20 นาที", "Caligo": "7 วัน",
    "Poison Root Flower": "28 นาที 10 วินาที", "Contaminated Queen Bee": "28 นาที",
    "Rotten Pudding": "30 นาที", "Swamp Flower Monster": "30 นาที",
    "Ukpana": "48 ชั่วโมง", "Darlene the Witch": "72 ชั่วโมง",
    "Illust": "72 ชั่วโมง", "Actaemon": "6 ชั่วโมง",
    "Aiyo's Protector": "72 ชั่วโมง", "Glucose": "30 นาที",
    "Overload": "29 นาที 52 วินาที", "Soul Lich": "24 ชั่วโมง 15 นาที",
    "Platanista": "168 ชั่วโมง (7 วัน)", "Barslaf": "48 ชั่วโมง",
    "Billiard": "7 ชั่วโมง 55 นาที 3 วินาที"
}

ADVANCE_NOTICE_SECONDS = {
    "Wadangka": 1800, "Elemental Queen": 300, "Tank": 300, 
    "Swirl Flame": 300, "Maelstrom": 300, "Twister": 300,
    "Bigmama": 1800, "Chief Magief": 300, "Faith": 1800, "Apapa": 300, 
    "Corrupt Forest Keeper": 300, "Recluse": 1800, "Blackskull": 300, 
    "Sleepy Kooii": 300, "Awaken Kooii": 300, "Eeheehee": 300, 
    "Ooheeheek": 300, "Oohehe": 300, "Guardian Imp": 300, "Devilang": 1800, 
    "Blackjuno": 300, "Blacksky": 300, "Red Fox": 300, "7tailfox": 300, 
    "777Tailfox": 300, "Sunrise Flower": 300, "Magma Senior Thief": 300,
    "Bbinikjoe": 300, "Bigmouse": 300, "Caligo": 3600, "Poison Root Flower": 300,
    "Contaminated Queen Bee": 300, "Rotten Pudding": 300, "Swamp Flower Monster": 300,
    "Ukpana": 1800, "Darlene the Witch": 1800, "Illust": 1800, "Actaemon": 1800,
    "Aiyo's Protector": 1800, "Glucose": 300, "Overload": 300, "Soul Lich": 1800,
    "Platanista": 3600, "Barslaf": 1800, "Billiard": 300
}

ADVANCE_NOTICE_TEXT = {
    "Wadangka": "30 นาที", "Elemental Queen": "5 นาที", "Tank": "5 นาที", 
    "Swirl Flame": "5 นาที", "Maelstrom": "5 นาที", "Twister": "5 นาที",
    "Bigmama": "30 นาที", "Chief Magief": "5 นาที", "Faith": "30 นาที", 
    "Apapa": "5 นาที", "Corrupt Forest Keeper": "5 นาที", "Recluse": "30 นาที", 
    "Blackskull": "5 นาที", "Sleepy Kooii": "5 นาที", "Awaken Kooii": "5 นาที",
    "Eeheehee": "5 นาที", "Ooheeheek": "5 นาที", "Oohehe": "5 นาที", 
    "Guardian Imp": "5 นาที", "Devilang": "30 นาที", "Blackjuno": "5 นาที", 
    "Blacksky": "5 นาที", "Red Fox": "5 นาที", "7tailfox": "5 นาที", 
    "777Tailfox": "5 นาที", "Sunrise Flower": "5 นาที", "Magma Senior Thief": "5 นาที",
    "Bbinikjoe": "5 นาที", "Bigmouse": "5 นาที", "Caligo": "1 ชั่วโมง", 
    "Poison Root Flower": "5 นาที", "Contaminated Queen Bee": "5 นาที", 
    "Rotten Pudding": "5 นาที", "Swamp Flower Monster": "5 นาที",
    "Ukpana": "30 นาที", "Darlene the Witch": "30 นาที", "Illust": "30 นาที", 
    "Actaemon": "30 นาที", "Aiyo's Protector": "30 นาที", "Glucose": "5 นาที", 
    "Overload": "5 นาที", "Soul Lich": "30 นาที", "Platanista": "1 ชั่วโมง", 
    "Barslaf": "30 นาที", "Billiard": "5 นาที"
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
    "Billiard": "บิลเลียด"
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
    user_role_names = [role.name for role in member.roles]
    return any(role_name in user_role_names for role_name in TARGET_ROLE_NAMES)

def has_allowed_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return check_user_permission(interaction.user)
    return app_commands.check(predicate)

# ==========================================
# 💾 5. ระบบบันทึก/โหลดไฟล์ JSON & GitHub
# ==========================================
def save_vip_config():
    try:
        with open(VIP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(vip_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ เซฟ vip_config ไม่สำเร็จ: {e}")

    github_token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if github_token and repo_name:
        try:
            url = f"https://api.github.com/repos/{repo_name}/contents/{VIP_CONFIG_FILE}"
            headers = {"Authorization": f"token {github_token}"}
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha", "") if res.status_code == 200 else None

            content_b64 = base64.b64encode(json.dumps(vip_config, ensure_ascii=False, indent=2).encode('utf-8')).decode('utf-8')
            payload = {"message": "Auto-update vip_config.json via Discord Bot", "content": content_b64}
            if sha: payload["sha"] = sha

            put_res = requests.put(url, headers=headers, json=payload)
            if put_res.status_code in [200, 201]:
                print("✅ อัปเดต vip_config.json ขึ้น GitHub สำเร็จถาวร!")
        except Exception as e:
            print(f"❌ อัปเดต vip_config ขึ้น GitHub ไม่สำเร็จ: {e}")

def load_vip_config():
    global vip_config
    if not os.path.exists(VIP_CONFIG_FILE): return
    try:
        with open(VIP_CONFIG_FILE, "r", encoding="utf-8") as f:
            vip_config = json.load(f)
    except Exception as e:
        print(f"❌ โหลด vip_config ไม่สำเร็จ: {e}")

def save_live_config():
    try:
        with open(LIVE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(live_message_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ เซฟ live_config ไม่สำเร็จ: {e}")

    github_token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if github_token and repo_name:
        try:
            url = f"https://api.github.com/repos/{repo_name}/contents/{LIVE_CONFIG_FILE}"
            headers = {"Authorization": f"token {github_token}"}
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha", "") if res.status_code == 200 else None

            content_b64 = base64.b64encode(json.dumps(live_message_config, ensure_ascii=False, indent=2).encode('utf-8')).decode('utf-8')
            payload = {"message": "Auto-update live_config.json via Discord Bot", "content": content_b64}
            if sha: payload["sha"] = sha

            put_res = requests.put(url, headers=headers, json=payload)
            if put_res.status_code in [200, 201]:
                print("✅ อัปเดต live_config.json ขึ้น GitHub สำเร็จถาวร!")
        except Exception as e:
            print(f"❌ อัปเดต live_config ขึ้น GitHub ไม่สำเร็จ: {e}")

def load_live_config():
    global live_message_config
    if not os.path.exists(LIVE_CONFIG_FILE): return
    try:
        with open(LIVE_CONFIG_FILE, "r", encoding="utf-8") as f:
            live_message_config = json.load(f)
    except Exception as e:
        print(f"❌ โหลด live_config ไม่สำเร็จ: {e}")

def save_custom_bosses_to_github():
    custom_data = {}
    for name in list(BOSS_RESPAWN_TIMES.keys()):
        if name not in DEFAULT_BOSS_NAMES:
            custom_data[name] = {
                "total_seconds": int(BOSS_RESPAWN_TIMES[name].total_seconds()),
                "cd_text": BOSS_CD_TEXT[name],
                "notice_seconds": ADVANCE_NOTICE_SECONDS[name],
                "notice_text": ADVANCE_NOTICE_TEXT[name]
            }

    try:
        with open(CUSTOM_BOSSES_FILE, "w", encoding="utf-8") as f:
            json.dump(custom_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ เซฟลง local ไม่สำเร็จ: {e}")

    github_token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if github_token and repo_name:
        try:
            url = f"https://api.github.com/repos/{repo_name}/contents/{CUSTOM_BOSSES_FILE}"
            headers = {"Authorization": f"token {github_token}"}
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha", "") if res.status_code == 200 else None

            content_b64 = base64.b64encode(json.dumps(custom_data, ensure_ascii=False, indent=2).encode('utf-8')).decode('utf-8')
            payload = {"message": "Auto-update custom_bosses.json via Discord Bot", "content": content_b64}
            if sha: payload["sha"] = sha

            put_res = requests.put(url, headers=headers, json=payload)
            if put_res.status_code in [200, 201]:
                print("✅ อัปเดตไฟล์ custom_bosses.json บน GitHub สำเร็จถาวร!")
        except Exception as e:
            print(f"❌ อัปเดตขึ้น GitHub ไม่สำเร็จ: {e}")

def load_custom_bosses():
    if not os.path.exists(CUSTOM_BOSSES_FILE): return
    try:
        with open(CUSTOM_BOSSES_FILE, "r", encoding="utf-8") as f:
            custom_data = json.load(f)
            for boss_name, data in custom_data.items():
                BOSS_RESPAWN_TIMES[boss_name] = timedelta(seconds=data["total_seconds"])
                BOSS_CD_TEXT[boss_name] = data["cd_text"]
                ADVANCE_NOTICE_SECONDS[boss_name] = data["notice_seconds"]
                ADVANCE_NOTICE_TEXT[boss_name] = data["notice_text"]
    except Exception as e:
        print(f"❌ โหลดข้อมูล custom_bosses ไม่สำเร็จ: {e}")

def save_boss_data():
    global boss_schedule
    data_to_save = {}
    for boss_name, data in boss_schedule.items():
        data_to_save[boss_name] = {
            "spawn_time": data["spawn_time"].isoformat(),
            "channel_id": data["channel_id"],
            "notified_advance": data.get("notified_advance", False)
        }
    
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ บันทึกข้อมูล Local ไม่สำเร็จ: {e}")

    github_token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPO")

    if github_token and repo_name:
        try:
            url = f"https://api.github.com/repos/{repo_name}/contents/{DATA_FILE}"
            headers = {"Authorization": f"token {github_token}"}
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha", "") if res.status_code == 200 else None

            content_b64 = base64.b64encode(json.dumps(data_to_save, ensure_ascii=False, indent=2).encode('utf-8')).decode('utf-8')
            payload = {"message": "Auto-update boss_data.json via Discord Bot", "content": content_b64}
            if sha: payload["sha"] = sha

            put_res = requests.put(url, headers=headers, json=payload)
            if put_res.status_code in [200, 201]:
                print("✅ อัปเดตข้อมูลตารางบอสขึ้น GitHub สำเร็จถาวร!")
        except Exception as e:
            print(f"❌ อัปเดตข้อมูลตารางบอสขึ้น GitHub ไม่สำเร็จ: {e}")

def load_boss_data():
    global boss_schedule
    if not os.path.exists(DATA_FILE): return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
            for boss_name, data in saved_data.items():
                st = datetime.fromisoformat(data["spawn_time"])
                if st.tzinfo is None:
                    st = st.replace(tzinfo=TZ_THAI)
                boss_schedule[boss_name] = {
                    "spawn_time": st,
                    "channel_id": data.get("channel_id"),
                    "notified_advance": data.get("notified_advance", False)
                }
    except Exception as e:
        print(f"❌ โหลดข้อมูลไม่สำเร็จ: {e}")

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
        roles_str = ", ".join([f"`{r}`" for r in TARGET_ROLE_NAMES])
        embed = discord.Embed(
            title="🚫 ปฏิเสธการเข้าถึง",
            description=f"คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้!\nอนุญาตเฉพาะผู้ที่มีบทบาท: {roles_str} เท่านั้นครับ",
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
    """ฟังก์ชั่นทำความสะอาดชื่อสมาชิกและชื่อห้อง ลบอีโมจิและอักขระพิเศษ"""
    if not name:
        return "สมาชิก"
    # กรองเอาเฉพาะ ตัวอักษรไทย อังกฤษ ตัวเลข และช่องว่าง
    cleaned = re.sub(r'[^\w\s\u0E00-\u0E7F]', '', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else "สมาชิก"


async def speak_in_guild(guild: discord.Guild, text: str):
    if not guild: return

    # จัดทำระบบคิวเสียง (Queue) ด้วย Lock ต่อ Guild เพื่อให้พูดต่อกันทีละคน
    if guild.id not in voice_locks:
        voice_locks[guild.id] = asyncio.Lock()

    async with voice_locks[guild.id]:
        vc = guild.voice_client
        should_disconnect = False

        if not vc or not vc.is_connected():
            target_vc = None
            for channel in guild.voice_channels:
                human_members = [m for m in channel.members if not m.bot]
                if len(human_members) > 0:
                    target_vc = channel
                    break
            
            if target_vc:
                try:
                    vc = await target_vc.connect(reconnect=True)
                    should_disconnect = True
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"❌ เชื่อมต่อห้องเสียงไม่สำเร็จ: {e}")
                    return
            else:
                print("⚠️ ไม่พบห้องเสียงที่มีคนอยู่ บอทจึงไม่ได้เข้าเตือนด้วยเสียง")
                return

        # สร้างชื่อไฟล์ TTS แยกเฉพาะคิวด้วย Timestamp เพื่อป้องกันชื่อไฟล์ชนกัน
        tts_filename = f"temp_tts_{guild.id}_{int(time.time() * 1000)}.mp3"
        
        try:
            # 💡 [ครอบ try...except ช่วงแปลงเสียง TTS]
            try:
                communicate = edge_tts.Communicate(text, VOICE_THAI, rate="-30%", pitch="+20Hz")
                await communicate.save(tts_filename)
            except Exception as tts_err:
                print(f"❌ เกิดข้อผิดพลาดในการแปลง TTS ('{text}'): {tts_err}")
                return

            if not os.path.exists(tts_filename) or os.path.getsize(tts_filename) == 0:
                print("⚠️ ไฟล์เสียง TTS ไม่สมบูรณ์หรือข้ามการเล่น")
                return

            if vc.is_playing():
                vc.stop()

            ffmpeg_executable = get_ffmpeg_path()
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
                    print(f"❌ เกิดข้อผิดพลาดขณะเล่นเสียง: {error}")
                loop.call_soon_threadsafe(play_finished.set)

            vc.play(audio_source, after=after_playing)
            await play_finished.wait()

        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดในการเล่นเสียงพูด:")
            traceback.print_exc()

        finally:
            # ✅ แก้ไขจุดนี้: หากบอทเพิ่งเข้ามาเพื่อพูด (should_disconnect = True) ให้ตัดสายออกจากห้องทันทีเมื่อพูดจบ
            if should_disconnect and vc and vc.is_connected():
                await asyncio.sleep(0.5)
                try:
                    await vc.disconnect()
                except Exception as e:
                    print(f"❌ เกิดข้อผิดพลาดในการตัดสาย: {e}")
            
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

    # 1. ไม่ทำงานหากคนที่ย้าย/เข้าห้องเป็นบอท
    if member.bot:
        return

    # 2. เช็กว่าผู้ใช้เพิ่งกดเข้าห้องเสียง (หรือย้ายมาจากห้องอื่น)
    if before.channel != after.channel and after.channel is not None:
        
        # 3. เช็กว่าเป็นคนพิเศษ (VIP) หรือไม่
        if vip_config.get("enabled", False) and member.id == vip_config.get("user_id"):
            greeting_text = vip_config.get("message", "")
            if greeting_text:
                asyncio.create_task(speak_in_guild(member.guild, greeting_text))
        
        # 4. หากไม่ใช่ VIP ให้เช็กการทักทายสมาชิกปกติ (ถ้าเปิด /ppl อยู่)
        elif ppl_notify_enabled:
            user_name = clean_display_name(member.display_name)
            channel_name = clean_display_name(after.channel.name)
            greeting_text = f"ยินดีต้อนรับคุณ {user_name} เข้าสู่ห้อง{channel_name}"
            
            asyncio.create_task(speak_in_guild(member.guild, greeting_text))


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🔊 ใช้ FFmpeg จากตำแหน่ง: {get_ffmpeg_path()}")
    load_custom_bosses()
    load_boss_data()
    load_live_config()
    load_vip_config()

    await asyncio.sleep(3)
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print("⚠️ ชน Global Rate Limit ตอน Sync Commands แต่ระบบจะรันบอทต่อ...")
        else:
            print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    
    if not check_boss_notifications.is_running():
        check_boss_notifications.start()
    if not check_bf_notifications.is_running():
        check_bf_notifications.start()
    if not update_live_embed.is_running():
        update_live_embed.start()
    if not check_auto_disconnect.is_running():
        check_auto_disconnect.start()

# ==========================================
# ⏰ 7. Tasks เช็กเวลาเตือน + BF + Live Embed + Auto-Disconnect
# ==========================================
@tasks.loop(seconds=30)
async def check_bf_notifications():
    global last_bf_notified_hour, bf_notify_enabled
    if not bf_notify_enabled:
        return

    try:
        now = datetime.now(TZ_THAI)
        # BF เปิดชั่วโมงคู่ (08:00, 10:00, 12:00...) เตือนตอนนาทีที่ 57 ของชั่วโมงคี่
        if now.hour % 2 == 1 and now.minute == 57:
            if last_bf_notified_hour != now.hour:
                last_bf_notified_hour = now.hour
                
                next_bf_hour = (now.hour + 1) % 24
                next_bf_time = f"{next_bf_hour:02d}:00"
                
                for guild in bot.guilds:
                    mentions = []
                    for role_name in BF_ROLE_NAMES:
                        role = discord.utils.get(guild.roles, name=role_name)
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
                            await channel.send(content=mention_target, embed=embed)
                        except Exception as e:
                            print(f"❌ ส่งข้อความเตือน BF ไม่สำเร็จ: {e}")
                    
                    spoken_text = "แบทเทิ้ลฟิลด์กำลังจะเริ่มในอีก 3 นาทีค่ะ"
                    asyncio.create_task(speak_in_guild(guild, spoken_text))
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดใน Task 'check_bf_notifications': {e}")

@tasks.loop(seconds=60)
async def check_boss_notifications():
    try:
        now = datetime.now(TZ_THAI)
        changed = False
        
        for boss_name, data in list(boss_schedule.items()):
            spawn_time = data["spawn_time"]
            channel_id = data.get("channel_id")
            notified_advance = data.get("notified_advance", False)
            
            channel = None
            if channel_id:
                channel = bot.get_channel(channel_id)
                if not channel:
                    try: channel = await bot.fetch_channel(channel_id)
                    except Exception: channel = None

            if not channel: continue

            guild = channel.guild if hasattr(channel, "guild") else None
            mentions = []
            if guild:
                for role_name in TARGET_ROLE_NAMES:
                    role = discord.utils.get(guild.roles, name=role_name)
                    if role: mentions.append(role.mention)
            
            mention_target = " ".join(mentions) if mentions else ""

            time_left = (spawn_time - now).total_seconds()
            notice_limit = ADVANCE_NOTICE_SECONDS.get(boss_name, 300)
            notice_text = ADVANCE_NOTICE_TEXT.get(boss_name, "5 นาที")
            
            spoken_name = BOSS_PRONUNCIATION.get(boss_name, boss_name)
            
            if 0 < time_left <= notice_limit and not notified_advance:
                embed = discord.Embed(
                    title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
                    description=f"บอส **{boss_name}** จะเกิดในอีก **{notice_text}**!\nเวลาเกิด: **{spawn_time.strftime('%H:%M:%S น.')}**",
                    color=discord.Color.gold()
                )
                try:
                    await channel.send(content=mention_target, embed=embed)
                    if guild:
                        asyncio.create_task(speak_in_guild(guild, f"บอส {spoken_name} จะเกิดในอีก {notice_text} ค่ะ"))
                except discord.errors.HTTPException as e:
                    if e.status == 429:
                        print("⚠️ ชน Rate Limit ในการส่งข้อความเตือนบอส รอรอบถัดไป")
                        await asyncio.sleep(5)
                except Exception as e:
                    print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                    
                boss_schedule[boss_name]["notified_advance"] = True
                changed = True

            elif time_left <= 0:
                embed = discord.Embed(
                    title="⚔️ บอสเกิดแล้ว!",
                    description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
                    color=discord.Color.green()
                )
                try:
                    await channel.send(content=mention_target, embed=embed)
                    if guild:
                        asyncio.create_task(speak_in_guild(guild, f"บอส {spoken_name} เกิดแล้วค่ะ"))
                except discord.errors.HTTPException as e:
                    if e.status == 429:
                        print("⚠️ ชน Rate Limit ในการส่งข้อความแจ้งบอสเกิด")
                        await asyncio.sleep(5)
                except Exception as e:
                    print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                    
                del boss_schedule[boss_name]
                changed = True

        if changed:
            save_boss_data()
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดไม่คาดคิดใน Task 'check_boss_notifications': {e}")

@tasks.loop(seconds=60)
async def update_live_embed():
    try:
        if not live_message_config: return
        channel_id = live_message_config.get("channel_id")
        message_id = live_message_config.get("message_id")

        channel = bot.get_channel(channel_id)
        if not channel:
            try: channel = await bot.fetch_channel(channel_id)
            except Exception: return

        try:
            message = await channel.fetch_message(message_id)
        except Exception: return

        now = datetime.now(TZ_THAI)
        embed = discord.Embed(
            title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time",
            description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`",
            color=discord.Color.teal()
        )

        if not boss_schedule:
            embed.add_field(name="📌 สถานะ", value="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ", inline=False)
        else:
            sorted_bosses = sorted(boss_schedule.items(), key=lambda x: x[1]["spawn_time"])
            for boss, data in sorted_bosses:
                spawn_time = data["spawn_time"]
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

        embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")
        try:
            await message.edit(embed=embed)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print("⚠️ เกิด Rate Limit ตอนอัปเดต Live Embed บอทจะข้ามไปอัปเดตรอบถัดไป")
                await asyncio.sleep(10)
        except Exception as e:
            print(f"❌ อัปเดต Live Embed ไม่สำเร็จ: {e}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดไม่คาดคิดใน Task 'update_live_embed': {e}")

@tasks.loop(seconds=60)
async def check_auto_disconnect():
    try:
        now = datetime.now(TZ_THAI)
        for guild in bot.guilds:
            vc = guild.voice_client
            if vc and vc.is_connected():
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
        print(f"❌ เกิดข้อผิดพลาดไม่คาดคิดใน Task 'check_auto_disconnect': {e}")

async def boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=boss, value=boss)
        for boss in BOSS_RESPAWN_TIMES.keys()
        if current.lower() in boss.lower()
    ]
    return choices[:25]

# ==========================================
# 🔊 8. Voice & Notify Commands
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

    embed = discord.Embed(
        title="⚙️ ตั้งค่าการแจ้งเตือน BF",
        description=msg,
        color=color
    )
    await interaction.followup.send(embed=embed)

    await send_audit_log(
        interaction.guild,
        interaction.user,
        "ตั้งค่าการแจ้งเตือน BF (/notify)",
        f"เปลี่ยนสถานะการแจ้งเตือน BF เป็น: `{status.value.upper()}`",
        color
    )

@bot.tree.command(name="ppl", description="เปิดหรือปิดระบบแจ้งเตือนเสียงต้อนรับสมาชิกเข้าห้องเสียง (ทั่วไป)")
@app_commands.describe(status="เลือกเปิด (on) หรือปิด (off) การแจ้งเตือน")
@app_commands.choices(status=[
    app_commands.Choice(name="เปิดการแจ้งเตือน (on)", value="on"),
    app_commands.Choice(name="ปิดการแจ้งเตือน (off)", value="off")
])
async def toggle_ppl_notify(interaction: discord.Interaction, status: app_commands.Choice[str]):
    # 🔒 ตรวจสอบว่าเป็นเจ้าของเซิร์ฟเวอร์ (Server Owner) หรือไม่
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        embed = discord.Embed(
            title="🚫 ปฏิเสธการเข้าถึง",
            description="คำสั่งนี้อนุญาตเฉพาะ **เจ้าของเซิร์ฟเวอร์ (Server Owner)** เท่านั้นครับ!",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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

    embed = discord.Embed(
        title="⚙️ ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้องเสียง",
        description=msg,
        color=color
    )
    await interaction.followup.send(embed=embed)

    await send_audit_log(
        interaction.guild,
        interaction.user,
        "ตั้งค่าการแจ้งเตือนสมาชิกเข้าห้อง (/ppl)",
        f"เปลี่ยนสถานะการแจ้งเตือนต้อนรับสมาชิกเป็น: `{status.value.upper()}`",
        color
    )

@bot.tree.command(name="vip", description="[Admin Only] เปิด/ปิดและตั้งค่าระบบทักทายคนพิเศษ (พร้อมทักทายคนทั่วไป)")
@app_commands.describe(
    status="เลือกเปิด (on) หรือปิด (off) ระบบทักทายคนพิเศษ",
    user="เลือกสมาชิกคนพิเศษ (จำเป็นต้องระบุเมื่อเลือกเปิด)",
    message="ข้อความพูดทักทายคนพิเศษ (จำเป็นต้องระบุเมื่อเลือกเปิด)"
)
@app_commands.choices(status=[
    app_commands.Choice(name="เปิดระบบทักทายคนพิเศษ (on)", value="on"),
    app_commands.Choice(name="ปิดระบบทักทายคนพิเศษ (off)", value="off")
])
@app_commands.checks.has_permissions(administrator=True)
async def toggle_vip_greet(
    interaction: discord.Interaction, 
    status: app_commands.Choice[str], 
    user: discord.Member = None, 
    message: str = None
):
    await interaction.response.defer()
    global vip_config

    if status.value == "on":
        if not user or not message:
            await interaction.followup.send(
                "❌ **ข้อมูลไม่ครบถ้วน!** กรุณาระบุทั้ง **user** (คนพิเศษ) และ **message** (ข้อความทักทาย) เมื่อต้องการเปิดใช้งานครับ",
                ephemeral=True
            )
            return

        vip_config = {
            "enabled": True,
            "user_id": user.id,
            "user_name": user.display_name,
            "message": message
        }
        save_vip_config()

        embed = discord.Embed(
            title="🌟 เปิดใช้งานระบบทักทายคนพิเศษ (VIP)",
            description=f"🟢 **สถานะ:** เปิดใช้งาน\n👤 **คนพิเศษ:** {user.mention}\n💬 **คำทักทาย:** \"{message}\"\n\n*(หมายเหตุ: บอทจะทัก VIP ด้วยข้อความพิเศษนี้ และทักสมาชิกทั่วไปตามปกติหาก `/ppl` เปิดอยู่)*",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed)

        await send_audit_log(
            interaction.guild,
            interaction.user,
            "เปิดระบบทักทายคนพิเศษ (/vip)",
            f"👤 คนพิเศษ: `{user.display_name}` ({user.id})\n💬 ข้อความ: {message}",
            discord.Color.gold()
        )

    else: # status.value == "off"
        vip_config = {
            "enabled": False,
            "user_id": None,
            "user_name": "",
            "message": ""
        }
        save_vip_config()

        embed = discord.Embed(
            title="⚙️ ปิดระบบทักทายคนพิเศษ (VIP)",
            description="🔴 **สถานะ:** ปิดใช้งาน และยกเลิกข้อมูลคนพิเศษเดิมเรียบร้อยแล้ว\n*(ระบบจะยังคงทักทายสมาชิกทั่วไปตามปกติหาก `/ppl` เปิดอยู่)*",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

        await send_audit_log(
            interaction.guild,
            interaction.user,
            "ปิดระบบทักทายคนพิเศษ (/vip)",
            "ยกเลิกข้อมูลและปิดระบบทักทายคนพิเศษเรียบร้อยแล้ว",
            discord.Color.red()
        )

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

    embed = discord.Embed(
        title="🔊 เชื่อมต่อห้องเสียงสำเร็จ",
        description=f"บอทเข้าสู่ห้องเสียง **{voice_channel.name}** เรียบร้อยแล้ว!\nระบบพร้อมส่งเสียงแจ้งเตือนภาษาไทยเมื่อถึงเวลาครับ (มีระบบ Auto-disconnect เมื่อไม่มีคนในห้อง 3 นาที)",
        color=discord.Color.green()
    )
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
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()
            await interaction.followup.send("⏹️ บอทหยุดการทำงานและออกจากห้องเสียงเรียบร้อยแล้ว!")
        else:
            await interaction.followup.send("❌ บอทไม่ได้อยู่ในห้องเสียงครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ เกิดข้อผิดพลาด: `{e}`")

# ==========================================
# ⚔️ 9. Boss Slash Commands
# ==========================================
@bot.tree.command(name="kill", description="บันทึกเวลาที่บอสตายเพื่อเริ่มคำนวณเวลานับถอยหลัง")
@app_commands.describe(
    boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการบันทึกเวลา",
    kill_time="ระบุเวลาที่บอสตาย (เช่น 14:30 หรือ 14:30:00) ถ้าไม่ระบุจะใช้เวลาปัจจุบัน"
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
        await interaction.followup.send(f"❌ ไม่พบชื่อบอส **{boss_name}** ในระบบ! กรุณาตรวจสอบการพิมพ์อีกครั้ง", ephemeral=True)
        return

    now = datetime.now(TZ_THAI)
    
    if kill_time:
        try:
            time_parts = [int(p) for p in kill_time.strip().split(":")]
            if len(time_parts) == 2:
                hh, mm = time_parts
                ss = 0
            elif len(time_parts) == 3:
                hh, mm, ss = time_parts
            else:
                raise ValueError

            boss_died_at = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            
            if boss_died_at > now:
                boss_died_at -= timedelta(days=1)
                
        except ValueError:
            await interaction.followup.send("❌ รูปแบบเวลาไม่ถูกต้อง! กรุณากรอกแบบ **ชั่วโมง:นาที** เช่น `14:30` หรือ `09:15`", ephemeral=True)
            return
    else:
        boss_died_at = now

    next_spawn = boss_died_at + BOSS_RESPAWN_TIMES[matched_name]

    boss_schedule[matched_name] = {
        "spawn_time": next_spawn,
        "channel_id": interaction.channel_id,
        "notified_advance": False
    }
    save_boss_data()

    discord_time_str = f"**{next_spawn.strftime('%H:%M:%S น.')}**"

    embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายสำเร็จ", color=discord.Color.red())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{matched_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=boss_died_at.strftime("%H:%M:%S น."), inline=True)
    embed.add_field(name="⏳ ระยะเวลาเกิด (CD)", value=BOSS_CD_TEXT[matched_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=discord_time_str, inline=False)
    embed.set_footer(text=f"บันทึกโดย {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

    log_details = f"⚔️ **บันทึกคำสั่ง:** `/kill`\n👾 **บอส:** `{matched_name}`\n⏱️ **เวลานับตาย:** {boss_died_at.strftime('%H:%M:%S น.')}\n🔔 **เวลาเกิดถัดไป:** {next_spawn.strftime('%H:%M:%S น.')}"
    await send_audit_log(interaction.guild, interaction.user, "บันทึกเวลาบอสตาย (/kill)", log_details, discord.Color.red())

@bot.tree.command(name="addboss", description="เพิ่มบอสใหม่หรือแก้ไขเวลา คูลดาวน์ / เวลาเตือนล่วงหน้า")
@app_commands.describe(
    name="ชื่อบอสที่ต้องการเพิ่มหรือแก้ไข",
    hours="จำนวนชั่วโมงคูลดาวน์ (พิมพ์ตัวเลข)",
    minutes="จำนวนนาทีคูลดาวน์ (พิมพ์ตัวเลข)",
    seconds="จำนวนวินาทีคูลดาวน์ (พิมพ์ตัวเลข)",
    notice_minutes="เวลาที่ต้องการให้เตือนล่วงหน้า (นาที)"
)
@has_allowed_role()
async def add_boss(
    interaction: discord.Interaction, 
    name: str, 
    hours: int = 0, 
    minutes: int = 0, 
    seconds: int = 0, 
    notice_minutes: int = 5
):
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

    save_custom_bosses_to_github()

    embed = discord.Embed(title="✅ เพิ่ม/แก้ไขบอสสำเร็จ", color=discord.Color.green())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{matched_name}`", inline=True)
    embed.add_field(name="⏳ คูลดาวน์", value=cd_text, inline=True)
    embed.add_field(name="🔔 เตือนล่วงหน้า", value=f"{notice_minutes} นาที", inline=True)
    
    await interaction.followup.send(embed=embed)

    log_details = f"➕ **เพิ่ม/แก้ไขบอส:** `{matched_name}`\n⏳ **คูลดาวน์:** {cd_text}\n🔔 **เตือนล่วงหน้า:** {notice_minutes} นาที"
    await send_audit_log(interaction.guild, interaction.user, "เพิ่ม/แก้ไขบอส (/addboss)", log_details, discord.Color.green())

@bot.tree.command(name="delboss", description="ลบบอสออกจากตารางนับถอยหลัง")
@app_commands.describe(boss_name="เลือกหรือพิมพ์ชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
@has_allowed_role()
async def del_boss(interaction: discord.Interaction, boss_name: str):
    await interaction.response.defer()

    matched_key = None
    for k in boss_schedule.keys():
        if k.lower() == boss_name.lower():
            matched_key = k
            break

    if matched_key:
        del boss_schedule[matched_key]
        save_boss_data()
        
        embed = discord.Embed(
            title="🗑️ ลบบอสสำเร็จ",
            description=f"ทำการลบข้อมูลเวลาของบอส **{matched_key}** ออกจากระบบเรียบร้อยแล้ว",
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed)

        log_details = f"🗑️ **ลบบอสออกจากตาราง:** `{matched_key}`"
        await send_audit_log(interaction.guild, interaction.user, "ลบบอส (/delboss)", log_details, discord.Color.orange())
    else:
        await interaction.followup.send(f"❌ ไม่พบบอส **{boss_name}** ในตารางนับถอยหลังขณะนี้", ephemeral=True)

@bot.tree.command(name="status", description="เช็กสถานะเวลาบอสทั้งหมดที่กำลังนับถอยหลัง")
async def boss_status(interaction: discord.Interaction):
    await interaction.response.defer()

    if not boss_schedule:
        embed = discord.Embed(
            title="📜 ตารางเวลาบอส",
            description="ขณะนี้ยังไม่มีการบันทึกเวลาบอสใดๆ ในระบบ\nใช้คำสั่ง `/kill [ชื่อบอส]` เพื่อเริ่มบันทึกเวลาได้เลยครับ",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed)
        return

    now = datetime.now(TZ_THAI)
    embed = discord.Embed(
        title="📜 ตารางเวลาบอสเกิดทั้งหมด",
        description=f"อัปเดต ณ เวลา: `{now.strftime('%H:%M:%S น.')}`",
        color=discord.Color.blue()
    )

    sorted_bosses = sorted(boss_schedule.items(), key=lambda x: x[1]["spawn_time"])
    for boss, data in sorted_bosses:
        spawn_time = data["spawn_time"]
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

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="setlive", description="ตั้งค่าป้ายไฟนับถอยหลังเวลาบอสเกิด Real-time ในช่องนี้")
@has_allowed_role()
async def set_live(interaction: discord.Interaction):
    await interaction.response.defer()

    now = datetime.now(TZ_THAI)
    embed = discord.Embed(
        title="📌 [LIVE] ตารางนับถอยหลังเวลาบอสเกิด Real-time",
        description=f"อัปเดตล่าสุดเมื่อ: `{now.strftime('%H:%M:%S น.')}`",
        color=discord.Color.teal()
    )
    embed.add_field(name="📌 สถานะ", value="กำลังเริ่มต้นระบบ...", inline=False)
    embed.set_footer(text="ป้ายไฟนับถอยหลังอัตโนมัติ • อัปเดตทุกๆ 1 นาที")

    msg = await interaction.followup.send(embed=embed)

    global live_message_config
    live_message_config = {
        "channel_id": interaction.channel_id,
        "message_id": msg.id
    }
    save_live_config()

    log_details = f"📌 **ตั้งค่า Live Embed ในช่อง:** <#{interaction.channel_id}>\nMessage ID: `{msg.id}`"
    await send_audit_log(interaction.guild, interaction.user, "สร้าง Live Embed (/setlive)", log_details, discord.Color.teal())

# ==========================================
# 🚀 10. Run Bot
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
