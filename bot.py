import discord
from discord import app_commands
from discord.ext import commands
import os
import json
import threading
from datetime import datetime, timedelta
import pytz
from flask import Flask

# ==========================================
# 🌐 1. Web Server หลอก Port สำหรับ Render (Free Tier 24/7)
# ==========================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Boss Timer Bot is online 24/7!", 200

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# รัน Web Server เบื้องหลัง
threading.Thread(target=run_web, daemon=True).start()

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone & Database เวลาเกิดของบอส
# ==========================================
TZ_THAI = pytz.timezone('Asia/Bangkok')

# 💡 กำหนดเวลาเกิดใหม่ (Respawn Time) ของบอสแต่ละตัว
# เก็บโครงสร้างเวลารีดาวน์เป็น (ชั่วโมง, นาที, วินาที)
BOSS_RESPAWN_TIMES = {
    "Wadangka": timedelta(hours=2, minutes=30),
    "Elemental Queen": timedelta(hours=2, minutes=30),
    "Tank": timedelta(minutes=58, seconds=20),
    "Bigmama": timedelta(hours=48)
}

# ข้อความแสดงระยะเวลารีดาวน์แบบอ่านง่าย
BOSS_CD_TEXT = {
    "Wadangka": "2 ชั่วโมง 30 นาที",
    "Elemental Queen": "2 ชั่วโมง 30 นาที",
    "Tank": "58 นาที 20 วินาที",
    "Bigmama": "48 ชั่วโมง"
}

# ==========================================
# 🤖 3. Discord Bot Setup
# ==========================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print("----------------------------------------")
    try:
        synced = await bot.tree.sync()
        print(f"ซิงค์ Slash Commands สำเร็จทั้งหมด {len(synced)} คำสั่ง")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")

# ==========================================
# ⚔️ 4. Slash Command: /kill (บันทึกเวลาบอสตาย)
# ==========================================
async def boss_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=boss, value=boss)
        for boss in BOSS_RESPAWN_TIMES.keys()
        if current.lower() in boss.lower()
    ]

@bot.tree.command(name="kill", description="บันทึกเวลาบอสตาย (เช่น 17:05) แล้วคำนวณเวลาเกิดให้อัตโนมัติ")
@app_commands.describe(
    boss_name="เลือกชื่อบอส",
    kill_time="ระบุเวลาที่บอสตาย (รูปแบบ HH:MM เช่น 17:05 หรือ 09:30)"
)
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str):
    # 1. เช็กว่ามีชื่อบอสในระบบไหม
    if boss_name not in BOSS_RESPAWN_TIMES:
        await interaction.response.send_message(f"❌ ไม่พบชื่อบอส `{boss_name}` ในฐานข้อมูล!", ephemeral=True)
        return

    # 2. แปลงข้อความ HH:MM เป็นเวลาไทย
    try:
        hours, minutes = map(int, kill_time.split(":"))
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError
            
        now = datetime.now(TZ_THAI)
        killed_at = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        
        # ถ้าระบุเวลาล่วงหน้าเกินเวลาปัจจุบัน ให้ถือว่าเป็นของเมื่อวาน
        if killed_at > now:
            killed_at -= timedelta(days=1)

    except ValueError:
        await interaction.response.send_message("❌ กรุณากรอกเวลาให้ถูกต้องตามรูปแบบ `ชั่วโมง:นาที` เช่น `17:05` หรือ `09:30`", ephemeral=True)
        return

    # 3. คำนวณเวลาเกิดใหม่ (Respawn Time)
    respawn_delta = BOSS_RESPAWN_TIMES[boss_name]
    next_spawn = killed_at + respawn_delta

    # แปลงเป็น Discord Timestamp สำหรับนับถอยหลังเรียลไทม์
    timestamp_unix = int(next_spawn.timestamp())
    discord_time_str = f"<t:{timestamp_unix}:F> (<t:{timestamp_unix}:R>)"

    # 4. ส่งข้อความแสดงผล
    embed = discord.Embed(
        title=f"⚔️ บันทึกเวลาบอสตายเรียบร้อย",
        color=discord.Color.red()
    )
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=killed_at.strftime("%H:%M น."), inline=True)
    embed.add_field(name="⏳ เวลาเกิดใหม่ (CD)", value=BOSS_CD_TEXT[boss_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=discord_time_str, inline=False)
    embed.set_footer(text=f"ลงเวลาโดย {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)

# ==========================================
# 🚀 5. รันบอท Discord
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
        print("❌ ไม่พบ Discord Token! กรุณาตรวจสอบ config.json หรือ Environment Variables")