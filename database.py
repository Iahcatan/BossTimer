import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join("data", "boss.db")

def init_db():
    """สร้างตารางข้อมูลถ้ายังไม่มี"""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS boss_kills (
            guild_id INTEGER,
            channel_id INTEGER,
            boss_name TEXT,
            killed_time TEXT,
            respawn_time TEXT,
            killed_by TEXT,
            PRIMARY KEY (guild_id, boss_name)
        )
    ''')
    conn.commit()
    conn.close()

def record_kill(guild_id: int, channel_id: int, boss_name: str, respawn_minutes: int, killed_by: str):
    """บันทึกเวลาที่บอสถูกฆ่า และคำนวณเวลาเกิดใหม่"""
    now = datetime.now()
    respawn_at = now + timedelta(minutes=respawn_minutes)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO boss_kills 
        (guild_id, channel_id, boss_name, killed_time, respawn_time, killed_by)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        guild_id, 
        channel_id, 
        boss_name, 
        now.strftime("%Y-%m-%d %H:%M:%S"), 
        respawn_at.strftime("%Y-%m-%d %H:%M:%S"), 
        killed_by
    ))
    conn.commit()
    conn.close()
    return now, respawn_at

def get_all_bosses(guild_id: int):
    """ดึงรายการบอสทั้งหมดของกิลด์/เซิร์ฟเวอร์นั้นๆ"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT boss_name, killed_time, respawn_time, killed_by 
        FROM boss_kills 
        WHERE guild_id = ?
    ''', (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def clear_boss(guild_id: int, boss_name: str):
    """ลบข้อมูลบอสตัวที่เลือก"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM boss_kills WHERE guild_id = ? AND boss_name = ?', (guild_id, boss_name))
    conn.commit()
    conn.close()