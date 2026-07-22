import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string
from waitress import serve

# ==========================================
# 🌐 1. Web Dashboard & Server สำหรับ Render
# ==========================================
app = Flask(__name__)

# Template HTML/CSS หน้า Dashboard
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Boss Timer Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <meta http-equiv="refresh" content="10"> <!-- รีเฟรชหน้าเว็บอัตโนมัติทุก 10 วินาที -->
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
    
    # ดึงข้อมูลบอสจัดเรียงตามเวลาเกิด
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

TARGET_ROLE_NAMES = ["Eternal", "Meaw", "Anti"]

BOSS_RESPAWN_TIMES = {
    "Wadangka": timedelta(hours=2, minutes=30),
    "Elemental Queen": timedelta(hours=2, minutes=30),
    "Tank": timedelta(minutes=58, seconds=20),
    "Bigmama": timedelta(hours=48),
    "CHIEF MAGIEF": timedelta(minutes=30),
    "Faith": timedelta(hours=5, minutes=53),
    "Apapa": timedelta(minutes=15)
}

BOSS_CD_TEXT = {
    "Wadangka": "2 ชั่วโมง 30 นาที",
    "Elemental Queen": "2 ชั่วโมง 30 นาที",
    "Tank": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง",
    "CHIEF MAGIEF": "30 นาที",
    "Faith": "5 ชั่วโมง 53 นาที",
    "Apapa": "15 นาที"
}

ADVANCE_NOTICE_SECONDS = {
    "Wadangka": 1800,       # 30 นาที
    "Elemental Queen": 300, # 5 นาที
    "Tank": 300,            # 5 นาที
    "Bigmama": 300,         # 5 นาที
    "CHIEF MAGIEF": 300,    # 5 นาที
    "Faith": 300,           # 5 นาที
    "Apapa": 300            # 5 นาที
}

ADVANCE_NOTICE_TEXT = {
    "Wadangka": "30 นาที",
    "Elemental Queen": "5 นาที",
    "Tank": "5 นาที",
    "Bigmama": "5 นาที",
    "CHIEF MAGIEF": "5 นาที",
    "Faith": "5 นาที",
    "Apapa": "5 นาที"
}

boss_schedule = {}

# ==========================================
# 💾 3. ระบบบันทึกและโหลดข้อมูลแบบปลอดภัย (JSON File)
# ==========================================
def save_boss_data():
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
        print(f"❌ บันทึกข้อมูลไม่สำเร็จ: {e}")

def load_boss_data():
    global boss_schedule
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
            for boss_name, data in saved_data.items():
                spawn_time = datetime.fromisoformat(data["spawn_time"])
                boss_schedule[boss_name] = {
                    "spawn_time": spawn_time,
                    "channel_id": data.get("channel_id"),
                    "notified_advance": data.get("notified_advance", False)
                }
        print("✅ โหลดข้อมูลตารางบอสเรียบร้อยแล้ว")
    except Exception as e:
        print(f"❌ โหลดข้อมูลไม่สำเร็จ: {e}")

# ==========================================
# 🤖 4. Discord Bot Setup
# ==========================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print("----------------------------------------")
    load_boss_data()
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")
    
    if not check_boss_notifications.is_running():
        check_boss_notifications.start()

# ==========================================
# ⏰ 5. Task เช็กเวลาแจ้งเตือนอัตโนมัติ (ทำงานทุก 10 วินาที)
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
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None

        if not channel:
            continue

        mentions = []
        if hasattr(channel, "guild") and channel.guild:
            for role_name in TARGET_ROLE_NAMES:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        
        mention_target = " ".join(mentions) if mentions else "@everyone"

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
                print(f"✅ แจ้งเตือนล่วงหน้า {boss_name} สำเร็จ")
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
                print(f"✅ แจ้งเตือนบอสเกิด {boss_name} สำเร็จ")
            except Exception as e:
                print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                
            del boss_schedule[boss_name]
            changed = True

    if changed:
        save_boss_data()

async def boss_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=boss, value=boss)
        for boss in BOSS_RESPAWN_TIMES.keys()
        if current.lower() in boss.lower()
    ]
    return choices[:25]

# ==========================================
# ⚔️ 6. Slash Commands (เพิ่ม defer() บรรทัดแรกทุกคำสั่ง)
# ==========================================

@bot.tree.command(name="kill", description="บันทึกเวลาบอสตาย (เช่น 17:05) แล้วคำนวณเวลาเกิดให้อัตโนมัติ")
@app_commands.describe(
    boss_name="เลือกชื่อบอส",
    kill_time="ระบุเวลาที่บอสตาย (รูปแบบ HH:MM เช่น 17:05 หรือ 09:30)"
)
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str):
    # 1. ตอบรับ Discord ทันทีเพื่อกัน Error 10062
    await interaction.response.defer()

    if boss_name not in BOSS_RESPAWN_TIMES:
        await interaction.followup.send(f"❌ ไม่พบชื่อบอส `{boss_name}` ในฐานข้อมูล!", ephemeral=True)
        return

    try:
        hours, minutes = map(int, kill_time.split(":"))
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError
            
        now = datetime.now(TZ_THAI)
        killed_at = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        
        if killed_at > now:
            killed_at -= timedelta(days=1)

    except ValueError:
        await interaction.followup.send("❌ กรุณากรอกเวลาให้ถูกต้องตามรูปแบบ `ชั่วโมง:นาที` เช่น `17:05` หรือ `09:30`", ephemeral=True)
        return

    respawn_delta = BOSS_RESPAWN_TIMES[boss_name]
    next_spawn = killed_at + respawn_delta
    
    boss_schedule[boss_name] = {
        "spawn_time": next_spawn,
        "channel_id": interaction.channel_id,
        "notified_advance": False
    }
    save_boss_data()

    timestamp_unix = int(next_spawn.timestamp())
    discord_time_str = f"`{next_spawn.strftime('%H:%M:%S น.')}` (<t:{timestamp_unix}:R>)"
    notice_text = ADVANCE_NOTICE_TEXT.get(boss_name, "5 นาที")
    roles_display = ", ".join([f"`{r}`" for r in TARGET_ROLE_NAMES])

    embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายเรียบร้อย", color=discord.Color.red())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=killed_at.strftime("%H:%M:%S น."), inline=True)
    embed.add_field(name="⏳ เวลาเกิดใหม่ (CD)", value=BOSS_CD_TEXT[boss_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=discord_time_str, inline=False)
    embed.set_footer(text=f"ลงเวลาโดย {interaction.user.display_name} • ระบบจะแจ้งเตือนยศ {roles_display} ล่วงหน้า {notice_text}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="list", description="ดูตารางเวลาเกิดของบอสทั้งหมด")
async def list_bosses(interaction: discord.Interaction):
    # 1. ตอบรับ Discord ทันทีเพื่อกัน Error 10062
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
@app_commands.describe(boss_name="เลือกชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def clear_boss(interaction: discord.Interaction, boss_name: str):
    # 1. ตอบรับ Discord ทันทีเพื่อกัน Error 10062
    await interaction.response.defer()

    if boss_name in boss_schedule:
        del boss_schedule[boss_name]
        save_boss_data()
        await interaction.followup.send(f"🗑️ ลบเวลาของบอส `{boss_name}` ออกจากตารางเรียบร้อยแล้ว!")
    else:
        await interaction.followup.send(f"❌ ไม่พบข้อมูลการลงเวลาของบอส `{boss_name}`", ephemeral=True)

@bot.tree.command(name="info", description="ดูรายชื่อบอสและระยะเวลารีดาวน์ทั้งหมด")
async def boss_info(interaction: discord.Interaction):
    # 1. ตอบรับ Discord ทันทีเพื่อกัน Error 10062
    await interaction.response.defer()

    embed = discord.Embed(title="ℹ️ รายชื่อบอสและเวลารีดาวน์ (Respawn Time)", color=discord.Color.green())
    for boss, cd_text in BOSS_CD_TEXT.items():
        notice_text = ADVANCE_NOTICE_TEXT.get(boss, "5 นาที")
        embed.add_field(name=f"👾 {boss}", value=f"⏳ เกิดทุกๆ: **{cd_text}** (เตือนล่วงหน้า {notice_text})", inline=False)
    
    await interaction.followup.send(embed=embed)

# ==========================================
# 🚀 7. รันบอท Discord
# ==========================================
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN and os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            TOKEN = config.get("token")

    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ ไม่พบ Discord Token!")