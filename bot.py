import discord
from discord.ext import commands
import os
import json
import threading
from flask import Flask

# ==========================================
# 🌐 1. Web Server สำหรับหลอก Port บน Render ( Free Tier 24/7)
# ==========================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and running 24/7!", 200

def run_web():
    # ดึงค่า PORT จาก Render (ถ้าไม่มีจะใช้ 5000)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# รัน Web Server แยก Thread เบื้องหลังเพื่อไม่ให้ขัดการทำงานของบอท Discord
threading.Thread(target=run_web, daemon=True).start()

# ==========================================
# 🤖 2. Discord Bot Setup
# ==========================================
# โหลดไฟล์ config.json
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

TOKEN = config.get("token")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    print("----------------------------------------")
    print("บอท Discord ออนไลน์เรียบร้อยแล้ว!")

    # โหลด Extensions / Commands
    try:
        # สมมติว่ามีโฟลเดอร์ commands/
        if os.path.exists("./commands"):
            for filename in os.listdir("./commands"):
                if filename.endswith(".py"):
                    await bot.load_extension(f"commands.{filename[:-3]}")
                    print(f"Loaded extension: {filename}")
        
        # ซิงค์ Slash Commands
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error loading commands: {e}")

# รันบอท Discord
if __name__ == "__main__":
    bot.run(TOKEN)