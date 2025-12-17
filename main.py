import os
import json
import asyncio
import logging
import re
from typing import Dict, Any, Optional
from urllib.parse import urlencode

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8310675088:AAFCjnUBSwwQi8ADjITPOe6hMcdBgWInRZU").strip()
if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi.")

FAST_TIMEOUT = float(os.getenv("FAST_TIMEOUT", "4"))  # detik, 4-8 recommended
DATA_FILE = os.getenv("DATA_FILE", "chatbot_data.json")

DEFAULT_ROLE = (
    "Nama kamu Yan.\n"
    "Kamu manusia biasa yang lagi nongkrong di grup Telegram.\n"
    "Bukan bot, bukan AI, dan gak pernah bahas soal itu.\n\n"
    "Aturan main:\n"
    "- Jawaban singkat, idealnya 1 kalimat (maks 2–3 kalau kepepet).\n"
    "- Gak suka jelasin panjang kecuali diminta.\n"
    "- Kalau bisa jawab pendek, jangan dipanjangin.\n\n"
    "Gaya ngobrol:\n"
    "- Santai, gen-z, natural kayak chat harian.\n"
    "- Kadang cuek, kadang becanda tipis.\n"
    "- Boleh 0–1 emoji, jangan lebay.\n"
    "- Gak kaku, gak baku, gak sok pinter.\n\n"
    "Kalau ditanya 'kamu bot?' atau sejenisnya:\n"
    "- Ngeledek ringan, ngeles santai, ganti topik.\n\n"
    "Intinya:\n"
    "Balas kayak orang asli yang lagi bales chat, bukan kayak sistem."
)

SIPUTZX_GPT3_URL = os.getenv("SIPUTZX_GPT3_URL", "https://api.siputzx.my.id/api/ai/gpt3").strip()
SIPUTZX_GPT_URL = os.getenv("SIPUTZX_GPT_URL", "https://apis-liart.vercel.app/api/gpt").strip()

# Hard limiter settings (biar selalu singkat)
MAX_SENTENCES = int(os.getenv("MAX_SENTENCES", "2"))   # 1 atau 2 recommended
MAX_CHARS = int(os.getenv("MAX_CHARS", "280"))         # 200-320 enak buat chat

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
        with open(DATA_FILE", "w", encoding="utf-8") as f:
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
# RESPONSE LIMITER
# =========================
def limit_response(text: str, max_sentences: int = 2, max_chars: int = 280) -> str:
    if not text:
        return text

    # buang markdown heading / list yang bikin jadi "artikel"
    text = re.sub(r"^\s*#{1,6}\s+.*$", "", text, flags=re.MULTILINE).strip()

    # buang bullet list yang suka bikin kepanjangan
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE).strip()

    # potong karakter dulu
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()

    # split kalimat
    parts = re.split(r'(?<=[.!?])\s+', text)
    short = " ".join(parts[:max_sentences]).strip()

    return short if short else text


# =========================
# FALLBACK LOCAL REPLY
# =========================
def fallback_reply(user_text: str) -> str:
    t = (user_text or "").strip().lower()
    if not t:
        return "Ketik dulu dong."

    if t in {"hai", "halo", "hi", "p"}:
        return "Halo. Kenapa?"

    return "Lagi error bentar, coba ulang ya."


# =========================
# HTTP SESSION (REUSE)
# =========================
async def get_session(context: ContextTypes.DEFAULT_TYPE) -> aiohttp.ClientSession:
    sess = context.application.bot_data.get("aiohttp_session")
    if sess and not sess.closed:
        return sess

    timeout = aiohttp.ClientTimeout(total=30)
    sess = aiohttp.ClientSession(timeout=timeout)
    context.application.bot_data["aiohttp_session"] = sess
    return sess


async def close_session(app: Application) -> None:
    sess = app.bot_data.get("aiohttp_session")
    if sess and not sess.closed:
        await sess.close()


# =========================
# AI CALL (Siputzx)
# =========================
async def call_siputzx(prompt: str, role: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    prompt = (prompt or "").strip()
    role = (role or DEFAULT_ROLE).strip()
    if not prompt:
        return None

    session = await get_session(context)

    # 1) gpt3 query: prompt + content
    try:
        params = {"prompt": role, "content": prompt}
        url = f"{SIPUTZX_GPT3_URL}?{urlencode(params)}"
        async with session.get(url) as r:
            raw = await r.text()
            if r.status == 200:
                try:
                    js = json.loads(raw)
                except Exception:
                    return raw.strip() if raw.strip() else None

                if isinstance(js, dict):
                    val = js.get("data")
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                    if isinstance(val, dict):
                        c = val.get("content")
                        if isinstance(c, str) and c.strip():
                            return c.strip()

                    for v in js.values():
                        if isinstance(v, str) and v.strip():
                            return v.strip()
            else:
                log.warning("Siputzx gpt3 non-200: %s %s", r.status, raw[:300])
    except Exception:
        log.exception("Error call_siputzx (gpt3)")

    # 2) fallback demo: /api/gpt?text=
    try:
        params = {"text": prompt}
        url = f"{SIPUTZX_GPT_URL}?{urlencode(params)}"
        async with session.get(url) as r:
            raw = await r.text()
            if r.status != 200:
                log.warning("Siputzx gpt fallback non-200: %s %s", r.status, raw[:300])
                return None

            js = json.loads(raw)
            if isinstance(js, dict):
                data_obj = js.get("data")
                if isinstance(data_obj, dict):
                    content = data_obj.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

                for k in ("result", "answer", "message", "data"):
                    v = js.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            return None
    except Exception:
        log.exception("Error call_siputzx (fallback)")
        return None


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "On.\n\n"
        "• /chat on|off\n"
        "• /setrole <teks>\n"
        "• /role"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg not in ("on", "off"):
        return await update.message.reply_text("Pakai: /chat on atau /chat off")

    cfg["enabled"] = (arg == "on")
    save_data()
    await update.message.reply_text(f"{'AKTIF' if cfg['enabled'] else 'MATI'}")


async def setrole_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    role = " ".join(context.args).strip()
    if not role:
        return await update.message.reply_text("Pakai: /setrole ...")

    cfg["role"] = role[:3000]
    save_data()
    await update.message.reply_text("Ok.")


async def role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat_cfg(update.effective_chat.id)
    role = cfg.get("role") or DEFAULT_ROLE
    await update.message.reply_text(f"Role:\n\n{role}")


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

    replied_to_bot = False
    if msg.reply_to_message and msg.reply_to_message.from_user:
        replied_to_bot = (msg.reply_to_message.from_user.id == context.bot.id)

    # hanya jawab kalau /chat on ATAU user reply ke bot
    if not replied_to_bot and not cfg.get("enabled", False):
        return

    role = cfg.get("role") or DEFAULT_ROLE

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        answer = await asyncio.wait_for(
            call_siputzx(prompt=user_text, role=role, context=context),
            timeout=FAST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        answer = None

    if not answer:
        answer = fallback_reply(user_text)

    # paksa pendek
    answer = limit_response(answer, max_sentences=MAX_SENTENCES, max_chars=MAX_CHARS)

    await msg.reply_text(answer, disable_web_page_preview=True)


# =========================
# ERROR HANDLER
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)


# =========================
# MAIN
# =========================
def main():
    load_data()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_shutdown(close_session)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("setrole", setrole_cmd))
    app.add_handler(CommandHandler("role", role_cmd))

    # penting: jangan nangkep command sebagai chat biasa
    app.add_handler(
        MessageHandler(((filters.TEXT & ~filters.COMMAND) | filters.Caption), handle_message)
    )

    app.add_error_handler(on_error)

    log.info("Bot running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
