# SKYNET Slash Command Patch

## Scope

This patch keeps the existing Firebase, Boss Timer, `/kill`, `/setvoice`, TTS, Voice and Dashboard implementation in `bot.py`.

### `start.py`

`start.py` is now the single owner of Discord slash-command synchronization.

Changes:
- registers `/status` only if `bot.py` does not already contain it;
- ACKs `/status` immediately with `interaction.response.defer()`;
- disables the old `bot.py` global command sync at runtime so it cannot race with guild sync;
- synchronizes the current command tree directly to every guild after Gateway `on_ready`;
- verifies the remote guild command list and explicitly checks `/status`, `/kill`, `/setvoice`;
- logs every incoming application-command interaction;
- keeps the existing `QuickActionsView` registration;
- keeps the safe `custom_bosses` compatibility wrapper for old Firebase values.

## Why this is needed

The Render log can show `Logged in as SKYNET` while Discord still has an old or missing guild command definition. A slash command is delivered to the bot only when Discord has a valid application-command registration. Global sync can also take time to propagate.

The new startup flow uses direct guild sync so the commands used in the server are updated immediately after the bot connects.

## Deploy

1. Open Render for `bosstimer-ry18`.
2. Use **Manual Deploy -> Deploy latest commit**.
3. Wait for the log section:

```text
🚀 SKYNET STARTING
🌐 Starting web server...
🔑 พบ DISCORD_TOKEN
🔌 กำลังเริ่ม Discord Bot...
🔌 กำลังเชื่อมต่อ Discord Gateway...
Logged in as SKYNET (...)
🟢 on_ready received by start.py
============================================================
🔄 SKYNET DISCORD COMMAND SYNC
...
📋 Local commands: ...
...
🟢 Required commands verified on ...: /status /kill /setvoice
✅ DISCORD GUILD COMMAND SYNC COMPLETE (.../... guilds)
```

4. Test in Discord in this order:
   - `/status`
   - `/kill`
   - `/setvoice`

## Diagnostic rule

When you press `/status`, Render should show:

```text
📥 INTERACTION RECEIVED | command='status' ...
```

If this line does **not** appear, the problem is still command registration/Discord delivery rather than the callback code.

If it **does** appear, the next exception in Render identifies the callback problem.

## Firebase

Do not change Firebase Rules for this command-sync patch.
Do not delete `boss_schedule` or `voice_config`.
Do not change `DISCORD_TOKEN`.
