import os
import json
import asyncio
import logging
from typing import Dict, Any, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
# Disarankan: JANGAN hardcode token. Pake ENV BOT_TOKEN.
BOT_TOKEN = os.getenv("BOT_TOKEN", "7714224903:AAEI8X6z_A34C5mDlOQcCaTXBkKnp5Q0uTs").strip()
if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi.")

DATA_FILE = os.getenv("DATA_FILE", "chatbot_data.json")

DEFAULT_ROLE = (
    "Kamu adalah chatbot Telegram yang gaya jawabnya santai, to-the-point, sedikit humor cerdas kalau pas, "
    "dan bantuin user dengan solusi yang jelas. Jangan pakai emoji berlebihan."
)

# Endpoint Siputzx (sesuaikan kalau berubah)
SIPUTZX_URL = os.getenv("SIPUTZX_URL", "https://api.siputzx.my.id/api/ai/gpt3")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("chatbot-bot")

# =========================
# STORAGE
# =========================
data: Dict[str, Any] = {"chats": {}}


def load_data() -> None:
    global data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "chats" not in data or not isinstance(data["chats"], dict):
                data = {"chats": {}}
        except Exception:
            log.exception("Gagal load data, reset.")
            data = {"chats": {}}


def save_data() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Gagal save data.")


def get_chat_cfg(chat_id: int) -> Dict[str, Any]:
    cid = str(chat_id)
    if cid not in data["chats"]:
        data["chats"][cid] = {"role": DEFAULT_ROLE, "enabled": False}
        save_data()
    return data["chats"][cid]


# =========================
# FALLBACK
# =========================
def fallback_reply(user_text: str) -> str:
    return (
        "Backend AI lagi error / endpoint lagi berubah. Jadi gue fallback dulu.\n\n"
        f"Pesan kamu: {user_text[:1500]}"
    )


# =========================
# AI CALL
# =========================
async def call_siputzx(prompt: str, role: str, timeout_s: int = 25) -> Optional[str]:
    """
    Siputzx endpoint yang butuh format 'messages' (chat-style).
    """
    payload = {
        "messages": [
            {"role": "system", "content": role},
            {"role": "user", "content": prompt},
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SIPUTZX_URL, json=payload, timeout=timeout_s) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("Siputzx non-200: %s %s", r.status, raw[:500])
                    return None

                try:
                    js = json.loads(raw)
                except Exception:
                    return raw.strip() if raw.strip() else None

        # Normalisasi output (format bisa beda-beda)
        if isinstance(js, dict):
            # OpenAI-ish: choices[0].message.content
            choices = js.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                msg = choices[0].get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

            # Format lain yang sering dipakai
            for key_path in [
                ("result",),
                ("data", "result"),
                ("data", "answer"),
                ("answer",),
                ("message",),
            ]:
                cur = js
                ok = True
                for k in key_path:
                    if isinstance(cur, dict) and k in cur:
                        cur = cur[k]
                    else:
                        ok = False
                        break
                if ok and isinstance(cur, str) and cur.strip():
                    return cur.strip()

            # Last resort: string value pertama
            for v in js.values():
                if isinstance(v, str) and v.strip():
                    return v.strip()

        return None

    except asyncio.TimeoutError:
        return None
    except Exception:
        log.exception("Error call_siputzx")
        return None


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ = get_chat_cfg(update.effective_chat.id)
    text = (
        "Oke, gue online.\n\n"
        "Perintah:\n"
        "• /chat on  — auto-reply semua pesan di chat ini\n"
        "• /chat off — matiin auto-reply\n"
        "• /setrole <teks> — atur gaya/role chatbot\n"
        "• /role — lihat role saat ini\n\n"
        "Mode default: bot cuma bales kalau kamu reply ke pesan bot."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    arg = (context.args[0].lower() if context.args else "").strip()

    if arg not in ("on", "off"):
        return await update.message.reply_text("Pakai: /chat on atau /chat off")

    cfg["enabled"] = (arg == "on")
    save_data()

    await update.message.reply_text(
        f"Mode auto-reply: {'AKTIF' if cfg['enabled'] else 'MATI'}"
    )


async def setrole_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    role = " ".join(context.args).strip()
    if not role:
        return await update.message.reply_text("Pakai: /setrole kamu adalah ...")

    cfg["role"] = role[:3000]
    save_data()
    await update.message.reply_text("Role di-update. Sekarang bot bakal jawab sesuai style itu.")


async def role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    role = cfg.get("role") or DEFAULT_ROLE
    await update.message.reply_text(f"Role saat ini:\n\n{role}")


# =========================
# MESSAGE HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    cfg = get_chat_cfg(chat_id)

    msg = update.message
    user_text = (msg.text or msg.caption or "").strip()
    if not user_text:
        return

    # Trigger:
    # 1) user reply ke pesan bot
    replied_to_bot = False
    if msg.reply_to_message and msg.reply_to_message.from_user:
        replied_to_bot = (msg.reply_to_message.from_user.id == context.bot.id)

    # 2) atau mode auto-reply aktif
    if not replied_to_bot and not cfg.get("enabled", False):
        return

    role = cfg.get("role") or DEFAULT_ROLE

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    answer = await call_siputzx(prompt=user_text, role=role)
    if not answer:
        answer = fallback_reply(user_text)

    try:
        await msg.reply_text(answer[:4000], disable_web_page_preview=True)
    except Exception:
        await msg.reply_text(answer[:3500], disable_web_page_preview=True)


# =========================
# ERROR HANDLER
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


# =========================
# MAIN
# =========================
def main():
    load_data()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("setrole", setrole_cmd))
    app.add_handler(CommandHandler("role", role_cmd))

    app.add_handler(MessageHandler(filters.TEXT | filters.Caption(), handle_message))

    app.add_error_handler(on_error)

    log.info("Bot running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
