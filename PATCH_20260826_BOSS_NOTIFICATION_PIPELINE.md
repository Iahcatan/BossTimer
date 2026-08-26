# SKYNET Patch — Boss Notification Pipeline

## Problem found

After `/kill`, the Render log showed the schedule being saved and Firebase syncing, but there was no `post-/kill` diagnostic and no Voice notification at the expected time.

The existing checker also calculated the notification threshold from `get_boss_advance_notice_seconds(boss_name)` instead of the `noticeMinutes` value stored with the actual schedule. This made the Firebase/dashboard value and the bot's effective threshold capable of disagreeing.

## Patch

`boss_notification_patch.py` is now the single owner of the Boss notification state machine.

Pipeline:

```text
/kill
  ↓
local boss_schedule
  ↓
Firebase boss_schedule
  ↓
5-second notification checker
  ↓
noticeMinutes from the saved schedule
  ↓
advance text notification
  ↓
advance TTS
  ↓
spawn text notification
  ↓
spawn TTS
```

### Important changes

1. Checker interval is 5 seconds instead of 10 seconds.
2. `noticeMinutes` saved by `/kill` is the effective threshold.
3. Firebase notification flags use child `.update()` rather than replacing the entire `boss_schedule` root.
4. Advance and spawn Voice delivery have independent flags (`voiceNoticeSent`, `voiceSpawnSent`). A temporary Voice failure can therefore be retried without duplicating the text notification.
5. `/kill` triggers an immediate post-command diagnostic check.
6. Voice remains ON-DEMAND and is still owned by `voice_patch.py`.
7. Discord Gateway remains owned by `start.py`.

## Expected logs

After deployment:

```text
🛡️ Boss notification reliability patch installed
💓 NOTIFY HEALTH | schedules=2 checker_running=True
```

After `/kill`:

```text
💾 /kill saved: Yu -> 2026-08-26T14:26:00+07:00
🔎 Boss notification check (post-/kill): Yu | spawn=... | left=...s | notice=...m | advance=False | spawn_sent=False | voice_advance=False | voice_spawn=False
```

Near the advance threshold:

```text
🔎 Boss notification check (loop): Yu | spawn=... | left=...s | notice=5m | ...
🟢 Advance notice sent: Yu (5m)
🔊 Advance TTS sent: Yu
```

At spawn:

```text
🟢 Spawn notice sent: Yu
🔊 Spawn TTS sent: Yu
```

## Deployment

Render command remains:

```text
python start.py
```

Deploy the latest default-branch commit. No new Environment Variable is required for this notification patch.

## Verification order

1. Confirm Gateway READY and guild command sync complete.
2. Confirm `checker_running=True`.
3. Run `/setvoice` in each required Voice channel.
4. Run `/notice` and confirm TTS works in every configured occupied Voice channel.
5. Run `/kill` on a boss with a short enough cooldown for a quick test.
6. Confirm `post-/kill` diagnostics show the correct `spawn`, `left`, and `notice` values.
7. Confirm advance TTS.
8. Confirm spawn TTS.

Do not judge the timer from the dashboard countdown alone; the authoritative bot-side test is the `Boss notification check` log showing `left` and `notice`.
