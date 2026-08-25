# SKYNET Discord Gateway Patch — 2026-08-26

## Purpose
This patch targets the actual failure shown in Render logs: repeated Discord Gateway startup failures and the old `Discord 429 — รอ 10 วินาที` retry loop.

## Repository findings
- `start.py` is the Render entrypoint and already owns the runtime Gateway wrapper.
- `start.py` replaces `bot.run_bot_with_backoff` before `main()` starts the bot.
- `bot.py` still contains the legacy retry loop with `backoff = 10` and calls `bot.close()` before retrying.
- The old loop can reuse a closed discord.py Client without clearing its state.

## Important diagnosis
If Render logs still literally show:

`🛑 Discord 429 — รอ 10 วินาทีก่อนเชื่อมต่อใหม่`

then Render is NOT executing the current `start.py` from `main`, because the current `main/start.py` wrapper uses a 30-second minimum backoff and logs:

`🛑 Discord HTTP 429 / rate limit | retry_after=...s | รอ ...s ก่อนเริ่ม session ใหม่`

This distinction is intentional and is useful to prove which commit Render is actually running.

## Correct Gateway lifecycle

`bot.start(token, reconnect=True)`

handles normal Discord reconnect/resume itself.

Only startup/login failures should reach the outer retry loop:

`failure -> close if needed -> bot.clear() -> wait -> start again`

Never repeatedly call `start()` on a Client that has been closed without clearing its internal state.

## Required Render verification
After deploying the current `main`, the first Gateway line must be:

`🔌 กำลังเชื่อมต่อ Discord Gateway...`

If rate-limited, it must say:

`🛑 Discord HTTP 429 / rate limit ...`

NOT:

`🛑 Discord 429 — รอ 10 วินาที...`

Also verify the Render service is connected to repository `Iahcatan/BossTimer`, branch `main`, and has auto-deploy enabled or manually deploy the latest commit.
