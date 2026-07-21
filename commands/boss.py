import json
import os
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import database

def load_bosses():
    if os.path.exists("bosses.json"):
        with open("bosses.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# --- Dropdown Menu สำหรับเลือกบอสที่โดนจัดการ (/kill) ---
class BossSelect(discord.ui.Select):
    def __init__(self):
        bosses = load_bosses()
        options = []
        for boss_name, info in bosses.items():
            icon = info.get("icon", "⚔️")
            respawn = info.get("respawn_time_minutes", 0)
            options.append(
                discord.SelectOption(
                    label=boss_name,
                    description=f"เกิดใหม่ใน {respawn} นาที",
                    emoji=icon
                )
            )
        super().__init__(placeholder="▼ เลือกบอสที่เพิ่งโดนจัดการ...", options=options)

    async def callback(self, interaction: discord.Interaction):
        boss_name = self.values[0]
        bosses = load_bosses()
        boss_info = bosses.get(boss_name, {})
        respawn_minutes = boss_info.get("respawn_time_minutes", 120)
        icon = boss_info.get("icon", "⚔️")

        now, respawn_at = database.record_kill(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            boss_name=boss_name,
            respawn_minutes=respawn_minutes,
            killed_by=interaction.user.display_name
        )

        embed = discord.Embed(
            title=f"{icon} {boss_name}",
            color=discord.Color.red()
        )
        embed.add_field(name="Killed (เวลาที่ตาย)", value=now.strftime("%H:%M"), inline=True)
        embed.add_field(name="Respawn (เวลาเกิด)", value=respawn_at.strftime("%H:%M"), inline=True)
        
        hours = respawn_minutes // 60
        mins = respawn_minutes % 60
        time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        embed.add_field(name="Remaining (นับถอยหลัง)", value=time_str, inline=True)
        embed.set_footer(text=f"บันทึกโดย: {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

class BossSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(BossSelect())

# --- Dropdown Menu สำหรับเลือกบอสที่จะลบ (/clear) ---
class BossClearSelect(discord.ui.Select):
    def __init__(self):
        bosses = load_bosses()
        options = []
        for boss_name, info in bosses.items():
            icon = info.get("icon", "🗑️")
            options.append(
                discord.SelectOption(
                    label=boss_name,
                    description=f"ลบข้อมูลเวลาตายของ {boss_name}",
                    emoji=icon
                )
            )
        super().__init__(placeholder="▼ เลือกบอสที่ต้องการลบข้อมูล...", options=options)

    async def callback(self, interaction: discord.Interaction):
        boss_name = self.values[0]
        database.clear_boss(interaction.guild_id, boss_name)
        await interaction.response.send_message(f"🗑️ ลบข้อมูลเวลาตายของ **{boss_name}** เรียบร้อยแล้ว!", ephemeral=True)

class BossClearSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(BossClearSelect())

# --- รวมคำสั่งหลักของบอท ---
class BossCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="kill", description="เลือกบอสที่เพิ่งถูกจัดการ (มี Dropdown ให้เลือก)")
    async def kill(self, interaction: discord.Interaction):
        await interaction.response.send_message("กรุณาเลือกบอสจากรายการด้านล่าง:", view=BossSelectView(), ephemeral=True)

    @app_commands.command(name="bosslist", description="ดูรายการเวลาเกิดบอสทั้งหมด")
    async def bosslist(self, interaction: discord.Interaction):
        rows = database.get_all_bosses(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("❌ ยังไม่มีการบันทึกเวลาตายของบอสตัวไหนเลย", ephemeral=True)
            return

        embed = discord.Embed(title="📜 รายการบอสนับถอยหลัง", color=discord.Color.blue())
        bosses = load_bosses()

        for boss_name, killed_time, respawn_time, killed_by in rows:
            icon = bosses.get(boss_name, {}).get("icon", "⚔️")
            respawn_dt = datetime.strptime(respawn_time, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()

            if now >= respawn_dt:
                status = "🚨 **READY (เกิดแล้ว!)**"
            else:
                diff = respawn_dt - now
                hours, remainder = divmod(int(diff.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                status = f"⏳ เหลืออีก **{hours}h {minutes}m** (เกิด {respawn_dt.strftime('%H:%M')})"

            embed.add_field(name=f"{icon} {boss_name}", value=f"สถานะ: {status}\nคนลงเวลา: {killed_by}", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clear", description="ลบข้อมูลเวลาตายของบอส (มี Dropdown ให้เลือก)")
    async def clear(self, interaction: discord.Interaction):
        await interaction.response.send_message("เลือกบอสที่ต้องการลบเวลาตาย:", view=BossClearSelectView(), ephemeral=True)

    @app_commands.command(name="reloadboss", description="โหลดไฟล์ bosses.json ใหม่โดยไม่ต้องรีสตาร์ทบอท")
    async def reloadboss(self, interaction: discord.Interaction):
        try:
            load_bosses()
            await interaction.response.send_message("🔄 โหลดข้อมูลบอสจาก `bosses.json` ใหม่สำเร็จแล้ว!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ เกิดข้อผิดพลาดในการโหลดไฟล์: {e}", ephemeral=True)

    @app_commands.command(name="setup_board", description="สร้างกระดานสถานะบอสแบบเรียลไทม์ (อัปเดตอัตโนมัติ)")
    async def setup_board(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📊 ตารางนับถอยหลังเวลาบอส TWOM (Live Update)",
            description="ข้อความนี้จะอัปเดตเวลานับถอยหลังโดยอัตโนมัติทุกๆ 1 นาที",
            color=discord.Color.gold()
        )
        embed.set_footer(text="กำลังเริ่มการซิงค์ข้อมูล...")
        msg = await interaction.channel.send(embed=embed)
        
        # บันทึก ID ช่องและ ID ข้อความลง config.json
        config = {}
        if os.path.exists("config.json"):
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
        
        config["board_channel_id"] = interaction.channel_id
        config["board_message_id"] = msg.id

        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        await interaction.response.send_message("✅ สร้างกระดานเรียลไทม์สำเร็จ! คุณสามารถปักหมุดข้อความด้านบนไว้ได้เลยครับ", ephemeral=True)

async def setup(bot):
    await bot.add_cog(BossCog(bot))