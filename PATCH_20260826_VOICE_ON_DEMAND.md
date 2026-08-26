# SKYNET Patch — Voice On-Demand + Notification Pipeline

## เป้าหมาย

Patch นี้แก้ปัญหาที่ Bot อยู่ใน Voice Channel ตลอดเวลาแม้ไม่มีการประกาศ ทำให้ Voice Gateway มีงานต่อเนื่องโดยไม่จำเป็น

โหมดใหม่:

1. `/setvoice` = บันทึกห้อง Voice ลง Firebase เท่านั้น
2. `/kill` = บันทึกเวลาตาย → คำนวณ `spawnTimeMs` → Firebase `boss_schedule`
3. Notification loop ตรวจทุก 10 วินาที
4. ถึงเวลา advance notice → ส่งข้อความ + เรียก TTS
5. ถึงเวลา spawn → ส่งข้อความ + เรียก TTS
6. TTS runtime = connect Voice → สร้าง MP3 → เล่น → disconnect
7. พูดเสร็จแล้ว Bot ออกจาก Voice อัตโนมัติ

## ไฟล์ที่เกี่ยวข้อง

- `start.py` — เจ้าของ Discord Gateway lifecycle เพียงจุดเดียว
- `bot.py` — Firebase, Dashboard, slash commands, boss schedule และ notification logic เดิม
- `voice_patch.py` — Voice/TTS runtime แบบ On-Demand
- `admin_notification_patch.py` — Admin Ban/Unban และ startup/notification gate

## สิ่งที่แก้

### 1. ยกเลิก Persistent Voice

ก่อนหน้า `on_ready()` มีการเรียก `ensure_configured_voice()` สำหรับทุก guild ที่มี `voice_config` ทำให้บอทเข้าห้องตั้งแต่เริ่มระบบและค้างอยู่

Patch ใหม่ยังคงเก็บ `voice_config` แต่ `ensure_configured_voice()` จะถูกเรียกเฉพาะเมื่อมีงาน TTS/Voice จริง

### 2. `reconnect=True` ไม่ถูกใช้สำหรับ On-Demand connect

การเชื่อมต่อที่สร้างโดย notification ใช้ `target.connect(reconnect=False, timeout=20)` เพื่อไม่ให้ Voice client พยายามรักษา connection ถาวรหลังงานเสร็จ

### 3. TTS เป็นเจ้าของ Voice session ชั่วคราว

`speak_in_guild()` ทำงานตามลำดับ:

```text
ensure Voice
   ↓
สร้าง TTS MP3
   ↓
FFmpeg เล่นเสียง
   ↓
รอ after callback
   ↓
ลบไฟล์ชั่วคราว
   ↓
disconnect Voice
```

### 4. ไม่ให้ Notification watchdog ต่อ Voice เอง

Notification watchdog มีหน้าที่ดูแล notification tasks เท่านั้น ไม่เรียก Voice connect ทุก 20/30 วินาที

## ตรวจสอบ Boss Notification Pipeline

จากโค้ดปัจจุบัน pipeline หลักคือ:

```text
/kill
  ↓
boss_schedule ใน memory
  ↓
save_boss_data()
  ↓
Firebase /boss_schedule
  ↓
Firebase listener / startup load
  ↓
check_boss_notifications() ทุก 10 วินาที
  ↓
advance notice เมื่อ 0 < spawn-now <= notice_limit
  ↓
ข้อความ Discord + TTS
  ↓
spawn notice เมื่อ spawn-now <= 0
  ↓
ข้อความ Discord + TTS
```

`Wadangka` ใช้ advance notice 30 นาที ส่วนค่าอื่นใช้ค่าจาก `ADVANCE_NOTICE_SECONDS`/custom boss configuration

## สำคัญ: ความแม่นยำของเวลา

Loop ตรวจทุก 10 วินาที ดังนั้นการแจ้งเตือนจริงอาจเกิดช้ากว่าเวลาทฤษฎีเล็กน้อย โดยปกติไม่เกินประมาณ 10 วินาทีจากรอบตรวจ และยังขึ้นกับ Discord/Firebase/network latency และเวลาสร้าง TTS

## วิธีทดสอบหลัง Deploy

### Test A — `/setvoice`

1. ใช้ `/setvoice` เลือกห้อง Voice
2. ดู Render log
3. ต้องเห็นประมาณ:

```text
🟢 Voice mode: ON-DEMAND (setvoice saves only; TTS connects/disconnects automatically)
🔊 /setvoice saved ON-DEMAND: Eternal -> <channel>
```

และ **ต้องไม่เห็น** การเข้า Voice ทันทีจาก `/setvoice`

### Test B — Notification TTS

ใช้ `/kill` กับบอสที่มีเวลาสั้นพอสำหรับทดสอบ หรือกำหนด test boss ชั่วคราว

ต้องเห็นตามลำดับ:

```text
💾 /kill saved: ...
🔊 Voice on-demand connect สำเร็จ: ...
🔊 TTS สร้างไฟล์สำเร็จ: th (...)
▶️ กำลังเล่น TTS: th -> ...
🔌 Voice disconnected: ... (TTS finished)
```

จากนั้น Bot ต้องออกจาก Voice

### Test C — Spawn notice

รอถึง `spawn_time` ต้องมีข้อความ:

```text
⚔️ บอสเกิดแล้ว!
```

และ TTS จากนั้น Bot ต้อง disconnect หลังพูดเสร็จ

## หมายเหตุเรื่อง Error 4006

Discord voice websocket `4006` ที่เกิดระหว่าง persistent connection จะไม่ควรเกิดจากการพยายามรักษา Voice ไว้ตลอดเวลาอีกต่อไป เพราะ Patch นี้ไม่สร้าง persistent Voice connection หลัง startup

หากยังเห็น 4006 ให้ดูว่ามี command อื่น (`/join`, `/notice`, `/time`, Quick Action, หรือ code อื่น) สร้าง Voice connection พร้อมกันหรือไม่ เพราะ 4006 จากการเชื่อมต่อที่ถูกสร้างโดยงานจริงยังสามารถเกิดจาก Discord Voice/network ได้

## Deploy

Render ใช้คำสั่งเดิม:

```text
python start.py
```

ไม่ต้องสร้าง Service ใหม่ และไม่ต้องรัน `bot.py` แยก
