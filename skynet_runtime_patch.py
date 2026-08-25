"""Runtime compatibility patch used by start.py.

This module intentionally does not replace bot.py. It provides a safe wrapper
for legacy Firebase custom_bosses data while preserving the existing bot,
Firebase, TTS, Voice and Dashboard implementation.
"""

from __future__ import annotations

import traceback


def patch_bot(bot_module):
    original = getattr(bot_module, "load_custom_bosses", None)
    if original is None or getattr(original, "_skynet_safe_wrapper", False):
        return

    async def safe_load_custom_bosses():
        try:
            await original()
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            print(
                "⚠️ custom_bosses มีข้อมูลเก่าหรือรูปแบบไม่ถูกต้อง "
                f"({type(exc).__name__}: {exc}) — ข้ามข้อมูลที่ผิดรูปแบบ"
            )
        except Exception as exc:
            print(f"⚠️ load_custom_bosses failed safely: {exc!r}")
            traceback.print_exc()

    safe_load_custom_bosses._skynet_safe_wrapper = True
    bot_module.load_custom_bosses = safe_load_custom_bosses
