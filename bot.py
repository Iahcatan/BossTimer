import json
import os
import discord
from discord.ext import commands
import database
import scheduler

if not os.path.exists("config.json"):
    raise FileNotFoundError("ไม่พบไฟล์ config.json กรุณาสร้างไฟล์ก่อนเริ่มทำงาน")

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.get("prefix", "!"), intents=intents)

@bot.event
async def on_ready():
    database.init_db()
    
    # โหลดไฟล์คำสั่ง Slash Commands
    try:
        await bot.load_extension("commands.boss")
        synced = await bot.tree.sync()
        print(f"ซิงค์คำสั่ง Slash Commands สำเร็จ ({len(synced)} คำสั่ง)")
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการโหลดคำสั่ง: {e}")

    # เริ่มระบบ Scheduler แจ้งเตือนบอส
    scheduler.start_scheduler(bot)
    print(f"ระบบแจ้งเตือนเวลาบอส (Scheduler) : พร้อมใช้งาน")

    print(f"------------------------------------")
    print(f"Logged in as : {bot.user.name} (ID: {bot.user.id})")
    print(f"ระบบฐานข้อมูล (Database) : พร้อมใช้งาน")
    print(f"Boss Timer Bot v1.0 พร้อมใช้งานแล้ว!")
    print(f"------------------------------------")

if __name__ == "__main__":
    token = config.get("token")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("Error: กรุณาใส่ Bot Token ในไฟล์ config.json ให้ถูกต้อง")
    else:
        bot.run(token)