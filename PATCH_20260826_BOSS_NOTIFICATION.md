# SKYNET Patch — Boss Notification Reliability

## Problem observed

`/kill` successfully writes a boss schedule to Firebase, but the advance/spawn notification was not observed in Render logs or Voice.

The existing architecture already has a canonical `check_boss_notifications` loop running every 10 seconds. This patch does **not** create a second notification sender. Instead it:

1. Installs a reliability/diagnostic layer from `start.py`.
2. Detects a Discord slash-command `/kill` using Discord Interaction type `2`.
3. Triggers the existing canonical boss checker shortly after `/kill`.
4. Logs `spawn`, seconds remaining, effective `noticeMinutes`, and notification flags.
5. Adds a 30-second health probe showing whether the canonical checker is still running.

## Important ownership rule

`bot.py` remains the owner of notification side effects:

`/kill -> boss_schedule -> check_boss_notifications -> text notice + speak_in_guild -> Voice ON-DEMAND TTS`

`boss_notification_patch.py` only diagnoses and triggers the existing checker. It must not send a second copy of the notification.

## Files

- `boss_notification_patch.py` — reliability layer.
- `start.py` — installs the patch and starts the health probe after command sync.
- `bot.py` — existing canonical notification logic is intentionally preserved.
- `voice_patch.py` — existing multi-channel ON-DEMAND TTS is preserved.

## Expected Render logs after deploy

Startup:

```text
🛡️ Boss notification reliability patch installed
...
🟢 Notification tasks started: check_boss_notifications, check_bf_notifications, check_library_boss_notifications
💓 NOTIFY HEALTH | schedules=2 checker_running=True
```

After `/kill`:

```text
💾 /kill saved: Yu -> 2026-08-26T...
🔎 Boss notification check (post-/kill): Yu | spawn=... | left=...s | notice=5m | advance=False | spawn_sent=False
🟢 Boss notification checker executed (post-/kill)
```

Near the advance-notice threshold:

```text
... left=300.xs ...
🔊 TTS targets (Eternal): ...
🔊 Voice on-demand connect สำเร็จ: ...
▶️ กำลังเล่น TTS: ...
🔌 Voice disconnected: ...
```

At spawn:

```text
... left=-x.xs ... spawn_sent=False
▶️ กำลังเล่น TTS: ...
```

## Test procedure

1. Deploy the latest commit.
2. Wait for `DISCORD GUILD COMMAND SYNC COMPLETE`.
3. Confirm `checker_running=True` in `NOTIFY HEALTH`.
4. Use `/kill` with a boss whose advance notice is known.
5. Check the post-/kill diagnostic line for the calculated spawn and `notice=` value.
6. Do not change the Firebase schedule manually during this test.
7. Wait for the actual advance threshold and then the spawn time.
8. Confirm the Voice TTS logs for every occupied configured Voice channel.

If `NOTIFY HEALTH` says `checker_running=False`, the next patch should focus on task lifecycle. If it says `True` but no notification occurs when `left` crosses the threshold, the next patch should focus specifically on the canonical `check_boss_notifications` condition/data path.
