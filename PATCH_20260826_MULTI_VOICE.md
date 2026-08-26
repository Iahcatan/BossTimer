# SKYNET Patch — Multi-Voice On-Demand

## ปัญหาที่แก้
ก่อน patch นี้ `voice_config` เก็บ Voice channel แบบเดียวต่อ guild และ `speak_in_guild()` เลือก channel เดียว ทำให้เมื่อใช้ `/setvoice` หลายครั้ง การตั้งค่าครั้งล่าสุดกลายเป็นเป้าหมายหลัก

## พฤติกรรมใหม่

`/setvoice` ไม่ได้เปลี่ยนเป็นห้องใหม่แทนห้องเดิมแล้ว แต่ **เพิ่มห้องเข้า configuration** ของ guild

ตัวอย่าง:

```text
/setvoice ห้อง A
/setvoice ห้อง B
/setvoice ห้อง C
```

จะได้:

```text
Guild
└── channels
    ├── A
    ├── B
    └── C
```

เมื่อมี `/notice` หรือ Boss notification:

```text
Notification
   ↓
สร้าง TTS 1 ชุด
   ↓
ตรวจทุก configured Voice channel
   ↓
ข้ามห้องที่ไม่มีสมาชิกมนุษย์
   ↓
เข้า A → พูด → ออก
   ↓
เข้า B → พูด → ออก
   ↓
เข้า C → พูด → ออก
```

Discord อนุญาต Voice connection เพียงหนึ่งห้องต่อ guild ในเวลาเดียวกัน ดังนั้นห้องหลายห้องใน guild เดียวต้องทำแบบ **sequential** ไม่ใช่ connect พร้อมกัน

## สำคัญ
- ไม่เปลี่ยน Discord Gateway lifecycle: `start.py` ยังเป็นเจ้าของ Gateway เพียงจุดเดียว
- ไม่เขียน Firebase / Dashboard / Boss schedule ใหม่
- ไม่ให้ `/setvoice` เข้า Voice ทันที
- TTS ยังคงเป็น ON-DEMAND
- พูดเสร็จแต่ละห้องแล้ว disconnect
- หากห้องไม่มีคน จะไม่เสียเวลาพยายาม connect
- configuration เดิมที่มีเพียง `voice_channel_id` ยังรองรับ
- configuration ใหม่ใช้ `channels` เป็นรายการ channel

## ไฟล์ที่แก้

- `voice_patch.py` — เพิ่ม multi-channel configuration และ multi-channel TTS routing
- `PATCH_20260826_MULTI_VOICE.md` — เอกสารนี้

## วิธีทดสอบหลัง Deploy

### 1. ล้างความสับสนของการตั้งค่าเดิม
ใน Discord ให้ใช้ `/setvoice` ทีละห้องที่ต้องการ เช่น:

```text
/setvoice Eternal Only
/setvoice พูดคุยทั่วไป
/setvoice Raid
```

ทุกครั้งต้องขึ้นประมาณ:

```text
🔊 /setvoice saved ON-DEMAND MULTI: Eternal -> <channel> (configured=2)
```

และ **ห้าม** เห็นว่า Bot เข้า Voice ทันทีจาก `/setvoice`

### 2. ทดสอบ `/notice`
ให้มีสมาชิกมนุษย์อยู่ในอย่างน้อย 2 ห้องที่ตั้งค่าไว้ แล้วใช้ `/notice`

Render log ควรมี:

```text
🔊 TTS targets (Eternal): ห้องA(id), ห้องB(id)
🔊 Voice on-demand connect สำเร็จ: Eternal -> ห้องA
▶️ กำลังเล่น TTS: th -> Eternal -> ห้องA
🔌 Voice disconnected: Eternal (TTS finished: ห้องA)
🔊 Voice on-demand connect สำเร็จ: Eternal -> ห้องB
▶️ กำลังเล่น TTS: th -> Eternal -> ห้องB
🔌 Voice disconnected: Eternal (TTS finished: ห้องB)
```

ถ้าห้องหนึ่งไม่มีคน:

```text
⏭️ ข้ามห้องที่ไม่มีคนแล้ว: Eternal -> ห้องC
```

### 3. ทดสอบ Boss notification
เส้นทางเดิมยังคงเป็น:

```text
/kill
 ↓
Firebase boss_schedule
 ↓
notification task
 ↓
advance notice / spawn notice
 ↓
speak_in_guild()
 ↓
ทุก configured occupied Voice channel
```

### 4. ตรวจ resource usage
เมื่อไม่มี notification/TTS:

```text
Bot ไม่ควรอยู่ใน Voice
```

ดังนั้น Voice connection ไม่ควรถูกเปิดค้างไว้ตลอด 24 ชั่วโมง

## หมายเหตุ
Patch นี้แก้ routing ของ Voice หลายห้องต่อ guild โดยตรงจากอาการที่พบล่าสุดว่า TTS เข้าเฉพาะห้องสุดท้ายที่ `/setvoice` ไว้ การยืนยันว่า notification ทุกชนิดทำงานครบตามเวลาใน production ต้องทดสอบด้วยเวลาจริงหลัง deploy เพราะ repository source เพียงอย่างเดียวไม่สามารถยืนยัน Discord/Firebase/network timing จริงได้
