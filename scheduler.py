import asyncio
import json
import os
from datetime import datetime
import discord
import database

def load_config():
    if os.path.exists("config.json"):
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def load_bosses():
    if os.path.exists("bosses.json"):
        with open("bosses.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

notified_30m = set()
notified_spawned = set()

async def update_live_board(bot):
    """ฟังก์ชันคอยอัปเดตข้อความ Live Board ใน Discord ทุกๆ นาที"""
    config = load_config()
    channel_id = config.get("board_channel_id")
    message_id = config.get("board_message_id")

    if not channel_id or not message_id:
        return

    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            return

        msg = await channel.fetch_message(message_id)
        rows = database.get_all_bosses(channel.guild.id)
        bosses = load_bosses()

        embed = discord.Embed(
            title="📊 ตารางนับถอยหลังเวลาบอส TWOM (Live Update)",
            color=discord.Color.blue()
        )

        if not rows:
            embed.description = "❌ ยังไม่มีการบันทึกเวลาตายของบอสตัวไหนเลย"
        else:
            embed.description = "ข้อความนี้จะอัปเดตเวลานับถอยหลังโดยอัตโนมัติ"
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

        embed.set_footer(text=f"อัปเดตล่าสุดเมื่อ: {datetime.now().strftime('%H:%M:%S')}")
        await msg.edit(embed=embed)
    except Exception as e:
        print(f"Error updating live board: {e}")

async def check_boss_timers(bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            bosses = load_bosses()
            mention_str = "@everyone"

            # อัปเดตตาราง Live Board
            await update_live_board(bot)

            # ตรวจสอบการแจ้งเตือนบอสเกิด
            for guild in bot.guilds:
                rows = database.get_all_bosses(guild.id)
                for boss_name, killed_time, respawn_time, killed_by in rows:
                    respawn_dt = datetime.strptime(respawn_time, "%Y-%m-%d %H:%M:%S")
                    now = datetime.now()
                    time_left = (respawn_dt - now).total_seconds()
                    
                    key_30m = f"{guild.id}_{boss_name}_{respawn_time}_30m"
                    key_spawn = f"{guild.id}_{boss_name}_{respawn_time}_spawn"
                    icon = bosses.get(boss_name, {}).get("icon", "⚔️")

                    channel = None
                    for ch in guild.text_channels:
                        if ch.permissions_for(guild.me).send_messages:
                            channel = ch
                            break

                    if not channel:
                        continue

                    # 1. แจ้งเตือนล่วงหน้า 30 นาที
                    if 1740 <= time_left <= 1800 and key_30m not in notified_30m:
                        embed = discord.Embed(
                            title=f"🔔 แจ้งเตือนบอสใกล้เกิด!",
                            description=f"{icon} **{boss_name}** จะเกิดในอีก **30 นาที** ({respawn_dt.strftime('%H:%M')})",
                            color=discord.Color.gold()
                        )
                        await channel.send(content=mention_str, embed=embed)
                        notified_30m.add(key_30m)

                    # 2. แจ้งเตือนตอนบอสเกิดแล้ว
                    elif time_left <= 0 and key_spawn not in notified_spawned:
                        embed = discord.Embed(
                            title=f"🚨 บอสเกิดแล้ว!",
                            description=f"{icon} **{boss_name}** เกิดเรียบร้อยแล้ว ลุยเลย! ⚔️",
                            color=discord.Color.green()
                        )
                        await channel.send(content=mention_str, embed=embed)
                        notified_spawned.add(key_spawn)

        except Exception as e:
            print(f"Error in scheduler: {e}")

        # ตรวจสอบทุกๆ 30 วินาที
        await asyncio.sleep(30)

def start_scheduler(bot):
    bot.loop.create_task(check_boss_timers(bot))