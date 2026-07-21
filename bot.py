import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import threading
from datetime import datetime, timedelta, timezone
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

threading.Thread(target=run_web, daemon=True).start()

# ==========================================
# ⚙️ 2. ตั้งค่า Timezone ไทย & Database
# ==========================================
TZ_THAI = timezone(timedelta(hours=7))
DATA_FILE = "boss_data.json"

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

        time_left = (spawn_time - now).total_seconds()
        notice_limit = ADVANCE_NOTICE_SECONDS.get(boss_name, 300)
        notice_text = ADVANCE_NOTICE_TEXT.get(boss_name, "5 นาที")
        
        # 1. แจ้งเตือนล่วงหน้า -> @everyone
        if 0 < time_left <= notice_limit and not notified_advance:
            timestamp_unix = int(spawn_time.timestamp())
            embed = discord.Embed(
                title="⚠️ แจ้งเตือนบอสเตรียมเกิด!",
                description=f"บอส **{boss_name}** จะเกิดในอีก **{notice_text}**!\nเวลาเกิด: <t:{timestamp_unix}:F>",
                color=discord.Color.gold()
            )
            try:
                await channel.send(content="@everyone", embed=embed)
                print(f"✅ แจ้งเตือนล่วงหน้า {boss_name} สำเร็จ")
            except Exception as e:
                print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                
            boss_schedule[boss_name]["notified_advance"] = True
            changed = True

        # 2. เมื่อบอสเกิดแล้ว -> @everyone และลบออกจากตาราง
        elif time_left <= 0:
            embed = discord.Embed(
                title="⚔️ บอสเกิดแล้ว!",
                description=f"บอส **{boss_name}** เกิดแล้วในขณะนี้!",
                color=discord.Color.green()
            )
            try:
                await channel.send(content="@everyone", embed=embed)
                print(f"✅ แจ้งเตือนบอสเกิด {boss_name} สำเร็จ")
            except Exception as e:
                print(f"❌ ส่งข้อความเตือนไม่สำเร็จ: {e}")
                
            del boss_schedule[boss_name]
            changed = True

    if changed:
        save_boss_data()

# Autocomplete สำหรับเลือกชื่อบอส
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
# ⚔️ 6. Slash Commands
# ==========================================

# 1️⃣ คำสั่ง /kill : บันทึกเวลาบอสตาย
@bot.tree.command(name="kill", description="บันทึกเวลาบอสตาย (เช่น 17:05) แล้วคำนวณเวลาเกิดให้อัตโนมัติ")
@app_commands.describe(
    boss_name="เลือกชื่อบอส",
    kill_time="ระบุเวลาที่บอสตาย (รูปแบบ HH:MM เช่น 17:05 หรือ 09:30)"
)
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def kill_boss(interaction: discord.Interaction, boss_name: str, kill_time: str):
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

    embed = discord.Embed(title="⚔️ บันทึกเวลาบอสตายเรียบร้อย", color=discord.Color.red())
    embed.add_field(name="👾 ชื่อบอส", value=f"`{boss_name}`", inline=True)
    embed.add_field(name="⏱️ เวลาที่ตาย", value=killed_at.strftime("%H:%M:%S น."), inline=True)
    embed.add_field(name="⏳ เวลาเกิดใหม่ (CD)", value=BOSS_CD_TEXT[boss_name], inline=True)
    embed.add_field(name="🔔 บอสจะเกิดเวลา", value=discord_time_str, inline=False)
    embed.set_footer(text=f"ลงเวลาโดย {interaction.user.display_name} • ระบบจะแจ้งเตือน @everyone ล่วงหน้า {notice_text}")

    await interaction.followup.send(embed=embed)


# 2️⃣ คำสั่ง /list : ดูรายการตารางบอสทั้งหมด
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


# 3️⃣ คำสั่ง /clear : ลบเวลาบอสออกจากตาราง
@bot.tree.command(name="clear", description="ลบเวลาบอสออกจากตาราง")
@app_commands.describe(boss_name="เลือกชื่อบอสที่ต้องการลบ")
@app_commands.autocomplete(boss_name=boss_autocomplete)
async def clear_boss(interaction: discord.Interaction, boss_name: str):
    await interaction.response.defer()

    if boss_name in boss_schedule:
        del boss_schedule[boss_name]
        save_boss_data()
        await interaction.followup.send(f"🗑️ ลบเวลาของบอส `{boss_name}` ออกจากตารางเรียบร้อยแล้ว!")
    else:
        await interaction.followup.send(f"❌ ไม่พบข้อมูลการลงเวลาของบอส `{boss_name}`", ephemeral=True)


# 4️⃣ คำสั่ง /info : ดูรายชื่อบอสและระยะเวลารีดาวน์
@bot.tree.command(name="info", description="ดูรายชื่อบอสและระยะเวลารีดาวน์ทั้งหมด")
async def boss_info(interaction: discord.Interaction):
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