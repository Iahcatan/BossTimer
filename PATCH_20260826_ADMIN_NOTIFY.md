# SKYNET Patch 2026-08-26 — Voice / Notification / Admin

## Current base
Repository: `Iahcatan/BossTimer`

## What changed

### 1. Voice startup gate
`voice_patch.py` now loads `admin_notification_patch.py` after installing the Voice runtime.
The admin patch wraps the existing `ensure_configured_voice()` and the existing notification `Loop.start()` calls.

Before deterministic guild command sync completes:
- configured Voice connection is deferred
- boss/BF/Library notification loops are deferred
- live embed and auto-disconnect loops are deferred

After `start.py` completes command sync, `ensure_notification_tasks_started()` opens the runtime gate and starts the existing notification loops.

This prevents the previous race where `bot.py.on_ready()` attempted Voice and notification startup before command sync.

### 2. Voice 4006 noise
`discord.voice_state` logging is filtered for the known transient `Disconnected from voice ... 4006` record. The actual VoiceClient recovery remains `reconnect=True`; the patch does not create a second VoiceClient lifecycle.

### 3. Admin Ban / Unban
New application-level Ban system:
- `/ban @member [reason]` — Administrator only
- `/unban <Discord User ID>` — Administrator only
- Firebase path: `banned_users`
- Banned users are rejected by the global application-command check.

This is a SKYNET Bot application ban, not a Discord server ban.

### 4. Admin web panel
A protected `/admin` route is added.
It requires the Render environment variable:

`ADMIN_PANEL_KEY=<long-random-secret>`

Open:

`https://YOUR-RENDER-DOMAIN/admin?key=YOUR_KEY`

The panel can list, Ban and Unban application-banned users.

### 5. Notification pipeline diagnostics
A read-only diagnostic task logs a boss when it approaches the configured notification/spawn window:

`🧪 NOTIFY PIPELINE | Boss | left=...s | advance=... | spawn=... | TTS=ready`

It does not send a second notification and therefore does not duplicate the existing boss notification pipeline.

## Existing boss pipeline retained

`/kill`
→ local `boss_schedule`
→ Firebase `boss_schedule`
→ `spawnTimeMs`
→ existing `check_boss_notifications` (10-second loop)
→ advance notice
→ spawn notice
→ existing `speak_in_guild()` / TTS

No Firebase schema rewrite was introduced by this patch.

## Required Render environment variable

Add:

`ADMIN_PANEL_KEY`

Do not put the value into GitHub source files.

## Expected deployment log

After Gateway READY and command sync:

```text
🟢 Required commands verified: /status /kill /setvoice /notice
🟢 Runtime gate OPEN: Voice + notification loops may start
🟢 Notification tasks started: check_boss_notifications, check_bf_notifications, check_library_boss_notifications
🟢 Voice watchdog started
🟢 Notification watchdog started
🧪 Notification pipeline diagnostics started
🛡️ Admin Ban/Unban commands installed
🛡️ Admin web panel installed at /admin
```

The command list should now also contain:

`ban, unban`

## Test order

1. Deploy/Resume Render.
2. Confirm Gateway READY.
3. Confirm Guild Sync succeeds.
4. Confirm runtime gate opens.
5. Confirm `/ban` and `/unban` appear in Discord.
6. As Administrator, Ban a test user.
7. Confirm the banned user cannot use SKYNET slash commands.
8. Unban the test user.
9. Run `/kill` with a short test boss/custom boss timer.
10. Watch `🧪 NOTIFY PIPELINE` logs.
11. Confirm advance text notification + Voice TTS.
12. Confirm spawn text notification + Voice TTS.
13. Confirm Firebase flags change to `notifiedNotice=true` and `notifiedSpawn=true` after each stage.

## Important interpretation

A `4006` Voice WebSocket close is not the same as the main Discord Gateway. The patch treats Voice recovery separately and does not restart the main Gateway when Voice reconnects.
