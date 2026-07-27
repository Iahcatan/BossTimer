import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import threading
import requests
import base64
import asyncio
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string
from waitress import serve
from gtts import gTTS

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
    
    sorted_bosses = sorted(boss_schedule.items(), key=lambda x: x[1]["spawn_time"])
    
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
# ⚙️ 2. ตั้งค่า Timezone ไทย & Database
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))
DATA_FILE = "boss_data.json"
CUSTOM_BOSSES_FILE = "custom_bosses.json"

TARGET_ROLE_NAMES = ["Eternal", "Meaw", "Anti"]

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
    "RECLUSE": timedelta(hours=11, minutes=23),
    "BLACKSKULL": timedelta(minutes=56, seconds=50),
    "Sleepy Kooii": timedelta(minutes=20),
    "AWAKEN KOOII": timedelta(hours=1, minutes=3),
    "EEHEEHEE": timedelta(hours=1, minutes=6, seconds=48),
    "OOHEEHEEK": timedelta(hours=1, minutes=8, seconds=3),
    "OOHEHE": timedelta(hours=1, minutes=5, seconds=8),
    "GUARDIAN IMP": timedelta(hours=1, minutes=3),
    "DEVILANG": timedelta(hours=5, minutes=33),
    "BLACKJUNO": timedelta(minutes=35),
    "BLACKSKY": timedelta(minutes=35),
    "Red Fox": timedelta(minutes=20),
    "7tailfox": timedelta(minutes=20),
    "777TAILFOX": timedelta(minutes=30),
    "Sunrise Flower": timedelta(minutes=20),
    "Magma Senior Thief": timedelta(minutes=20),
    "Bbinikjoe": timedelta(minutes=20),
    "Bigmouse": timedelta(minutes=20),
    "CALIGO": timedelta(days=7),
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
    "Barslaf": timedelta(hours=48)
}

BOSS_CD_TEXT = {
    "Wadangka": "2 ชั่วโมง 30 นาที",
    "Elemental Queen": "2 ชั่วโมง 30 นาที",
    "Tank": "58 นาที 20 วินาที",
    "Swirl Flame": "58 นาที 20 วินาที",
    "Maelstrom": "58 นาที 20 วินาที",
    "Twister": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง",
    "CHIEF MAGIEF": "30 นาที",
    "Faith": "5 ชั่วโมง 53 นาที",
    "Apapa": "15 นาที",
    "Corrupt Forest Keeper": "58 นาที",
    "RECLUSE": "11 ชั่วโมง 23 นาที",
    "BLACKSKULL": "56 นาที 50 วินาที",
    "Sleepy Kooii": "20 นาที",
    "AWAKEN KOOII": "1 ชั่วโมง 3 นาที",
    "EEHEEHEE": "1 ชั่วโมง 6 นาที 48 วินาที",
    "OOHEEHEEK": "1 ชั่วโมง 8 นาที 3 วินาที",
    "OOHEHE": "1 ชั่วโมง 5 นาที 8 วินาที",
    "GUARDIAN IMP": "1 ชั่วโมง 3 นาที",
    "DEVILANG": "5 ชั่วโมง 33 นาที",
    "BLACKJUNO": "35 นาที",
    "BLACKSKY": "35 นาที",
    "Red Fox": "20 นาที",
    "7tailfox": "20 นาที",
    "777TAILFOX": "30 นาที",
    "Sunrise Flower": "20 นาที",
    "Magma Senior Thief": "20 นาที",
    "Bbinikjoe": "20 นาที",
    "Bigmouse": "20 นาที",
    "CALIGO": "7 วัน",
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
    "Barslaf": "48 ชั่วโมง"
}

ADVANCE_NOTICE_SECONDS = {
    "Wadangka": 1800, "Elemental Queen": 300, "Tank": 300, 
    "Swirl Flame": 300, "Maelstrom": 300, "Twister": 300,
    "Bigmama": 1800, "CHIEF MAGIEF": 300, "Faith": 1800, "Apapa": 300, 
    "Corrupt Forest Keeper": 300, "RECLUSE": 1800, "BLACKSKULL": 300, 
    "Sleepy Kooii": 300, "AWAKEN KOOII": 300, "EEHEEHEE": 300, 
    "OOHEEHEEK": 300, "OOHEHE": 300, "GUARDIAN IMP": 300, "DEVILANG": 1800, 
    "BLACKJUNO": 300, "BLACKSKY": 300, "Red Fox": 300, "7tailfox": 300, 
    "777TAILFOX": 300, "Sunrise Flower": 300, "Magma Senior Thief": 300,
    "Bbinikjoe": 300, "Bigmouse": 300, "CALIGO": 3600, "Poison Root Flower": 300,
    "Contaminated Queen Bee": 300, "Rotten Pudding": 300, "Swamp Flower Monster": 300,
    "Ukpana": 1800, "Darlene the Witch": 1800, "Illust": 1800, "Actaemon": 1800,
    "Aiyo's Protector": 1800, "Glucose": 300, "Overload": 300, "Soul Lich": 1800,
    "Platanista": 3600, "Barslaf": 1800
}

ADVANCE_NOTICE_TEXT = {
    "Wadangka": "30 นาที", "Elemental Queen": "5 นาที", "Tank": "5 นาที", 
    "Swirl Flame": "5 นาที", "Maelstrom": "5 นาที", "Twister": "5 นาที",
    "Bigmama": "30 นาที", "CHIEF MAGIEF": "5 นาที", "Faith": "30 นาที", 
    "Apapa": "5 นาที", "Corrupt Forest Keeper": "5 นาที", "RECLUSE": "30 นาที", 
    "BLACKSKULL": "5 นาที", "Sleepy Kooii": "5 นาที", "AWAKEN KOOII": "5 นาที",
    "EEHEEHEE": "5 นาที", "OOHEEHEEK": "5 นาที", "OOHEHE": "5 นาที", 
    "GUARDIAN IMP": "5 นาที", "DEVILANG": "30 นาที", "BLACKJUNO": "5 นาที", 
    "BLACKSKY": "5 นาที", "Red Fox": "5 นาที", "7tailfox": "5 นาที", 
    "777TAILFOX": "5 นาที", "Sunrise Flower": "5 นาที", "Magma Senior Thief": "5 นาที",
    "Bbinikjoe": "5 นาที", "Bigmouse": "5 นาที", "CALIGO": "1 ชั่วโมง", 
    "Poison Root Flower": "5 นาที", "Contaminated Queen Bee": "5 นาที", 
    "Rotten Pudding": "5 นาที", "Swamp Flower Monster": "5 นาที",
    "Ukpana": "30 นาที", "Darlene the Witch": "30 นาที", "Illust": "30 นาที", 
    "Actaemon": "30 นาที", "Aiyo's Protector": "30 นาที", "Glucose": "5 นาที", 
    "Overload": "5 นาที", "Soul Lich": "30 นาที", "Platanista": "1 ชั่วโมง", 
    "Barslaf": "30 นาที"
}

boss_schedule = {}

# ==========================================
# 💾 3. ระบบโหลดและเซฟไฟล์ JSON + GitHub Sync
# ==========================================
def save_custom_bosses_to_github():
    default_bosses = list(BOSS_RESPAWN_TIMES.keys())
    custom_data = {}
    for name in list(BOSS_RESPAWN_TIMES.keys()):
        if name not in default_bosses:
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
                boss_schedule[boss_name] = {
                    "spawn_time": datetime.fromisoformat(data["spawn_time"]),
                    "channel_id": data.get("channel_id"),
                    "notified_advance": data.get("notified_advance", False)
                }
    except Exception as e:
        print(f"❌ โหลดข้อมูลไม่สำเร็จ: {e}")

# ==========================================
# 🤖 4. Discord Bot Setup & Voice Helper (Auto-Join & Leave)
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def speak_in_guild(guild: discord.Guild, text: str):
    """
    ระบบเสียงอัจฉริยะ:
    1. ถ้าอยู่ในห้องเสียงอยู่แล้ว -> พูดได้เลย
    2. ถ้าไม่อยู่ -> สแกนหาห้องเสียงที่มีคนนั่งอยู่แล้วกระโดดเข้าไปพูด -> พูดเสร็จแล้วออกให้อัตโนมัติ!
    """
    if not guild: return

    vc = guild.voice_client
    should_disconnect = False

    # ถ้าบอทไม่อยู่ในห้องเสียง ให้ค้นหาห้องที่มีสมาชิกอยู่
    if not vc or not vc.is_connected():
        target_vc = None
        for channel in guild.voice_channels:
            # คัดกรองเอาสมาชิกที่ไม่ใช่บอท
            human_members = [m for m in channel.members if not m.bot]
            if len(human_members) > 0:
                target_vc = channel
                break
        
        if target_vc:
            try:
                vc = await target_vc.connect()
                should_disconnect = True
            except Exception as e:
                print(f"❌ เชื่อมต่อห้องเสียงไม่สำเร็จ: {e}")
                return
        else:
            # ไม่มีใครอยู่ในห้องเสียงใดๆ เลย ไม่ต้องเล่นเสียง
            return

    try:
        filename = "temp_notice.mp3"

        # 🇹🇭 เสียงพากย์ภาษาไทย gTTS
        tts = gTTS(text=text, lang='th')
        tts.save(filename)

        if vc.is_playing():
            vc.stop()

        audio_source = discord.FFmpegPCMAudio(filename)
        vc.play(audio_source)

        while vc.is_playing():
            await asyncio.sleep(1)

        if os.path.exists(filename):
            os.remove(filename)

    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการเล่นเสียงพูด: {e}")

    finally:
        # พูดเสร็จแล้วออกจากห้องทันที ป้องกันบอทหลุดค้าง
        if should_disconnect and vc and vc.is_connected():
            await vc.disconnect()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    load_custom_bosses()
    load_boss_data()
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    
    if not check_boss_notifications.is_running():
        check_boss_notifications.start()

# ==========================================
# ⏰ 5. Task เช็กเวลาแจ้งเตือนอัตโนมัติ
# ==========================================
@tasks.loop(seconds=10)
async def check_boss_notifications():
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
        
        # 1. แจ้งเตือนล่วงหน้า
        if 0 < time_left <= notice_limit and not notified_advance:
            timestamp_unix = int(spawn_time.timestamp())
            embed = discord.Embed(
                title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
                description=f"บอส **{boss_name}** จะเกิดในอีก **{notice_text}**!\nเวลาเกิด: <t:{timestamp_unix}:F>",
                color=discord.Color.gold()
            )
            try:
                await channel.send(content=mention_target, embed=embed)
                if guild:
                    # 🔊 เข้าห้องพูดภาษาไทยเสร็จแล้วออกอัตโนมัติ
                    asyncio.create_task(speak_in_guild(guild, f"บอส {boss_name} จะเกิดในอีก {notice_text} ค่ะ"))
            except Exception as e:
                print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                
            boss_schedule[boss_name]["notified_advance"] = True
            changed = True

        # 2. เมื่อบอสเกิดแล้ว
        elif time_left <= 0:
            embed = discord.Embed(
                title="⚔️ บอสเกิดแล้ว!",
                description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
                color=discord.Color.green()
            )
            try:
                await channel.send(content=mention_target, embed=embed)
                if guild:
                    # 🔊 เข้าห้องพูดภาษาไทยเสร็จแล้วออกอัตโนมัติ
                    asyncio.create_task(speak_in_guild(guild, f"บอส {boss_name} เกิดแล้วค่ะ"))
            except Exception as e:
                print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                
            del boss_schedule[boss_name]
            changed = True

    if changed:
        save_boss_data()

async def boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=boss, value=boss)
        for boss in BOSS_RESPAWN_TIMES.keys()
        if current.lower() in boss.lower()
    ]
    return choices[:25]

# ==========================================
# 🔊 6. Voice Commands (/join & /leave)
# ==========================================
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
        description=f"บอทเข้าสู่ห้องเสียง **{voice_channel.name}** เรียบร้อยแล้ว!\nระบบพร้อมส่งเสียงแจ้งเตือนภาษาไทยเมื่อถึงเวลาครับ",
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

# ==========================================
# ⚔️ 7. Boss Slash Commands
# ==========================================
@bot.tree.command(name="addboss", description="เพิ่มบอสใหม่เข้าสู่ระบบ")
@app_commands.describe(boss_name="ชื่อบอส", respawn_hours="ชั่วโมง", respawn_minutes="นาที", notice_minutes="เตือนล่วงหน้า (นาที)")
async def add_boss(interaction: discord.Interaction, boss_name: str, respawn_hours: int, respawn_minutes: int, notice_minutes: int = 5):
    await interaction.response.defer()
    if respawn_hours < 0 or respawn_minutes < 0 or notice_minutes < 0:
        await interaction.followup.send("❌ เวลาต้องเป็นจำนวนเต็มบวก!", ephemeral=True)
        return

    total_delta = timedelta(hours=respawn_hours, minutes=respawn_minutes)
    cd_parts = []
    if respawn_hours > 0: cd_parts.append(f"{respawn_hours} ชั่วโมง")
    if respawn_minutes > 0: cd_parts.append(f"{respawn_minutes} นาที")
    cd_text = " ".join(cd_parts)

    BOSS_RESPAWN_TIMES[boss_name] = total_delta
    BOSS_CD_TEXT[boss_name] = cd_text
    ADVANCE_NOTICE_SECONDS[boss_name] = notice_minutes * 60
    ADVANCE_NOTICE_TEXT[boss_name] = f"{notice_minutes} นาที"

    save_custom_bosses_to_github()

    embed = discord.Embed(title="✅ เพิ่มบอสใหม่เข้าสู่ระบบเรียบร้อย", color=discord.Color.green())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="⏳ เวลาเกิดใหม่ (CD)", value=cd_text, inline=True)
    embed.add_field(name="🔔 แจ้งเตือนล่วงหน้า", value=f"{notice_minutes} นาที", inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="kill", description="บันทึกเวลาบอสตาย (เช่น 17:05)")
@app_commands.describe(boss_name="เลือกชื่อบอส", kill_time="รูปแบบ HH:MM เช่น 17:05")
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str):
    await interaction.response.defer()
    if boss_name not in BOSS_RESPAWN_TIMES:
        await interaction.followup.send(f"❌ ไม่พบชื่อบอส `{boss_name}` ในฐานข้อมูล!", ephemeral=True)
        return

    try:
        hours, minutes = map(int, kill_time.split(":"))
        if not (0 <= hours <= 23 and 0 <= minutes <= 59): raise ValueError
        now = datetime.now(TZ_THAI)
        killed_at = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if killed_at > now: killed_at -= timedelta(days=1)
    except ValueError:
        await interaction.followup.send("❌ กรุณากรอกเวลาให้ถูกต้องตามรูปแบบ `ชั่วโมง:นาที` เช่น `17:05`", ephemeral=True)
        return

    next_spawn = killed_at + BOSS_RESPAWN_TIMES[boss_name]
    boss_schedule[boss_name] = {
        "spawn_time": next_spawn,
        "channel_id": interaction.channel_id,
        "notified_advance": False
    }
    save_boss_data()

    timestamp_unix = int(next_spawn.timestamp())
    discord_time_str = f"`{next_spawn.strftime('%H:%M:%S น.')}` (<t:{timestamp_unix}:R>)"

    embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายเรียบร้อย", color=discord.Color.red())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=killed_at.strftime("%H:%M:%S น."), inline=True)
    embed.add_field(name="⏳ เวลาเกิดใหม่ (CD)", value=BOSS_CD_TEXT[boss_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=discord_time_str, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="list", description="ดูตารางเวลาเกิดของบอสทั้งหมด")
async def list_bosses(interaction: discord.Interaction):
    await interaction.response.defer()
    if not boss_schedule:
        await interaction.followup.send("📌 ยังไม่มีการบันทึกเวลาบอสใดๆ ในขณะนี้", ephemeral=True)
        return

    embed = discord.Embed(title="📜 ตารางเวลาเกิดบอสล่าสุด", color=discord.Color.blue())
    sorted_bosses = sorted(boss_schedule.items(), key=lambda x: x[1]["spawn_time"])
    for boss, data in sorted_bosses:
        spawn_time = data["spawn_time"]
        timestamp_unix = int(spawn_time.timestamp())
        notice_text = ADVANCE_NOTICE_TEXT.get(boss, "5 นาที")
        embed.add_field(
            name=f"👾 {boss}",
            value=f"เกิดเวลา: `{spawn_time.strftime('%H:%M:%S น.')}`\nนับถอยหลัง: <t:{timestamp_unix}:R>\n*(เตือนล่วงหน้า {notice_text})*",
            inline=False
        )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="clear", description="ลบเวลาบอสออกจากตาราง")
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def clear_boss(interaction: discord.Interaction, boss_name: str):
    await interaction.response.defer()
    if boss_name in boss_schedule:
        del boss_schedule[boss_name]
        save_boss_data()
        await interaction.followup.send(f"🗑️ ลบเวลาของบอส `{boss_name}` ออกจากตารางเรียบร้อยแล้ว!")
    else:
        await interaction.followup.send(f"❌ ไม่พบข้อมูลการลงเวลาของบอส `{boss_name}`", ephemeral=True)

@bot.tree.command(name="info", description="ดูรายชื่อบอสและระยะเวลารีดาวน์ทั้งหมด")
async def boss_info(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(title="ℹ️ รายชื่อบอสและเวลารีดาวน์ (Respawn Time)", color=discord.Color.green())
    for boss, cd_text in BOSS_CD_TEXT.items():
        notice_text = ADVANCE_NOTICE_TEXT.get(boss, "5 นาที")
        embed.add_field(name=f"👾 {boss}", value=f"⏳ เกิดทุกๆ: **{cd_text}** (เตือนล่วงหน้า {notice_text})", inline=False)
    await interaction.followup.send(embed=embed)

# ==========================================
# 🚀 8. รันบอท Discord
# ==========================================
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ ไม่พบ Discord Token!")
