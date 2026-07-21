from flask import Flask, render_template_string
import database
import json
import os
from datetime import datetime

app = Flask(__name__)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TWOM Boss Timer Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e2e; color: #cdd6f4; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; }
        h1 { text-align: center; color: #89b4fa; }
        .boss-card { background: #313244; padding: 15px; margin-bottom: 12px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; }
        .boss-name { font-size: 1.2em; font-weight: bold; }
        .status-ready { color: #a6e3a1; font-weight: bold; }
        .status-wait { color: #f9e2af; }
        .footer { text-align: center; font-size: 0.8em; color: #a6adc8; margin-top: 20px; }
    </style>
    <script>
        setInterval(() => location.reload(), 15000);
    </script>
</head>
<body>
    <div class="container">
        <h1>⚔️ TWOM Boss Timer Dashboard</h1>
        {% if bosses %}
            {% for b in bosses %}
            <div class="boss-card">
                <div>
                    <div class="boss-name">{{ b.icon }} {{ b.name }}</div>
                    <div style="font-size:0.9em; color:#ba48d8;">คนลงเวลา: {{ b.killed_by }}</div>
                </div>
                <div>
                    {% if b.is_ready %}
                        <span class="status-ready">🚨 READY (เกิดแล้ว!)</span>
                    {% else %}
                        <span class="status-wait">⏳ เหลือ {{ b.remaining }} (เกิด {{ b.respawn_time }})</span>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div style="text-align:center; padding: 40px; background:#313244; border-radius:10px;">
                ❌ ยังไม่มีข้อมูลการลงเวลาบอส
            </div>
        {% endif %}
        <div class="footer">หน้าเว็บจะอัปเดตอัตโนมัติทุกๆ 15 วินาที</div>
    </div>
</body>
</html>
'''

def get_boss_data():
    if not os.path.exists("data/boss.db"):
        return []
    
    rows = database.get_all_bosses(guild_id=database.sqlite3.connect("data/boss.db").cursor().execute("SELECT DISTINCT guild_id FROM boss_kills").fetchone()[0] if os.path.exists("data/boss.db") else 0)
    
    bosses_info = {}
    if os.path.exists("bosses.json"):
        with open("bosses.json", "r", encoding="utf-8") as f:
            bosses_info = json.load(f)

    result = []
    for boss_name, killed_time, respawn_time, killed_by in rows:
        icon = bosses_info.get(boss_name, {}).get("icon", "⚔️")
        respawn_dt = datetime.strptime(respawn_time, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()

        is_ready = now >= respawn_dt
        remaining = ""
        if not is_ready:
            diff = respawn_dt - now
            hours, remainder = divmod(int(diff.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            remaining = f"{hours}h {minutes}m"

        result.append({
            "name": boss_name,
            "icon": icon,
            "respawn_time": respawn_dt.strftime("%H:%M"),
            "killed_by": killed_by,
            "is_ready": is_ready,
            "remaining": remaining
        })
    return result

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, bosses=get_boss_data())

def run_web():
    # รองรับ PORT บน Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)