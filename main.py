import os
import json
import asyncio
import logging
import re
from typing import Dict, Any, Optional
from urllib.parse import urlencode

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update
from telegram.constants import ParseMode, ChatAction
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

FAST_TIMEOUT = float(os.getenv("FAST_TIMEOUT", "4"))

DEFAULT_ROLE = ( "Kamu adalah seorang yang lagi ngebalesin chat di dalam group chat. Balasanmu harus menggunakan bahasa gaul sehari-hari ala anak gen z, sangat singkat, dan responsif. Balasan tidak perlu selalu formal atau informatif, fokus saja pada reaksi emosional atau komentar cepat. \n"
                "Jangan pernah menawarkan bantuan dan bertanya. \n" 
                "Jangan ngaku bot atau AI. \n" 
                "Jawaban singkat Maksimal 10 - 15 karakter. \n" 
               )

SIPUTZX_GPT3_URL = os.getenv("SIPUTZX_GPT3_URL", "https://api.siputzx.my.id/api/ai/gpt3").strip()
SIPUTZX_GPT_URL = os.getenv("SIPUTZX_GPT_URL", "https://apis-liart.vercel.app/api/gpt").strip()

MAX_SENTENCES = int(os.getenv("MAX_SENTENCES", "1"))
MAX_CHARS = int(os.getenv("MAX_CHARS", "15"))

MENTION_REGEX = re.compile(r"@\w+", re.UNICODE)

MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://aseppp:aseppp@cluster0.bocyf5q.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0").strip()
MONGO_DB = os.getenv("MONGO_DB", "aseppp").strip()
MONGO_COLL = os.getenv("MONGO_COLL", "chat_cfg").strip()
if not MONGO_URI:
    raise SystemExit("ENV MONGO_URI belum diisi.")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("chatbot-mongo")

# =========================
# MONGO STORAGE
# =========================
class MongoStorage:
    def __init__(self, uri: str, db: str, coll: str):
        self.client = AsyncIOMotorClient(uri)
        self.coll = self.client[db][coll]

    async def init(self) -> None:
        await self.coll.create_index("chat_id", unique=True)

    async def get_chat_cfg(self, chat_id: int) -> Dict[str, Any]:
        doc = await self.coll.find_one({"chat_id": chat_id})
        if not doc:
            # default enabled=True biar gak perlu /chat on lagi
            cfg = {"role": DEFAULT_ROLE, "enabled": True}
            await self.coll.insert_one({"chat_id": chat_id, **cfg})
            return cfg
        return {
            "role": doc.get("role") or DEFAULT_ROLE,
            "enabled": bool(doc.get("enabled", True)),
        }

    async def set_chat_cfg(self, chat_id: int, cfg: Dict[str, Any]) -> None:
        await self.coll.update_one(
            {"chat_id": chat_id},
            {"$set": {"role": cfg.get("role") or DEFAULT_ROLE, "enabled": bool(cfg.get("enabled", True))}},
            upsert=True,
        )

    async def close(self) -> None:
        self.client.close()


STORE: Optional[MongoStorage] = None

# =========================
# HELP TEXT (buat "pas bot mati")
# =========================
def start_text() -> str:
    return (
        "On.\n\n"
        "• /chat on|off\n"
        "• /setrole <teks>\n"
        "• /role"
    )

# =========================
# RESPONSE LIMITER
# =========================
def limit_response(text: str, max_sentences: int = 1, max_chars: int = 15) -> str:
    if not text:
        return text
    text = text.strip()
    text = re.sub(r"^\s*#{1,6}\s+.*$", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    short = " ".join(parts[:max_sentences]).strip() or text
    short = short.strip()
    if len(short) > max_chars:
        short = short[:max_chars].rstrip()
    return short

# =========================
# AUTO DELETE @MENTION
# =========================
async def auto_delete_mention(msg, bot_id: int) -> bool:
    try:
        if msg.from_user and msg.from_user.id == bot_id:
            return False
        text = msg.text or msg.caption or ""
        if MENTION_REGEX.search(text):
            await msg.delete()
            return True
    except Exception:
        pass
    return False

def fallback_reply(_: str) -> str:
    return "wkwk"

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

    # 1) gpt3: prompt(role) + content(user)
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
                log.warning("Siputzx gpt3 non-200: %s %s", r.status, raw[:200])
    except Exception:
        log.exception("Error call_siputzx (gpt3)")

    # 2) fallback: /api/gpt?text=
    try:
        params = {"text": prompt}
        url = f"{SIPUTZX_GPT_URL}?{urlencode(params)}"
        async with session.get(url) as r:
            raw = await r.text()
            if r.status != 200:
                log.warning("Siputzx gpt fallback non-200: %s %s", r.status, raw[:200])
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
    await update.message.reply_text(start_text(), parse_mode=ParseMode.MARKDOWN)

async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await STORE.get_chat_cfg(update.effective_chat.id)
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg not in ("on", "off"):
        return await update.message.reply_text("Pakai: /chat on atau /chat off")

    cfg["enabled"] = (arg == "on")
    await STORE.set_chat_cfg(update.effective_chat.id, cfg)
    await update.message.reply_text(f"{'AKTIF' if cfg['enabled'] else 'MATI'}")

async def setrole_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await STORE.get_chat_cfg(update.effective_chat.id)
    role = " ".join(context.args).strip()
    if not role:
        return await update.message.reply_text("Pakai: /setrole ...")

    cfg["role"] = role[:3000]
    await STORE.set_chat_cfg(update.effective_chat.id, cfg)
    await update.message.reply_text("Ok.")

async def role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await STORE.get_chat_cfg(update.effective_chat.id)
    role = cfg.get("role") or DEFAULT_ROLE
    await update.message.reply_text(f"Role:\n\n{role}")

# =========================
# MESSAGE HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    msg = update.message
    chat_id = update.effective_chat.id

    # delete kalau ada @mention
    if await auto_delete_mention(msg, bot_id=context.bot.id):
        return

    user_text = (msg.text or msg.caption or "").strip()
    if not user_text:
        return

    cfg = await STORE.get_chat_cfg(chat_id)

    replied_to_bot = False
    if msg.reply_to_message and msg.reply_to_message.from_user:
        replied_to_bot = (msg.reply_to_message.from_user.id == context.bot.id)

    # Kalau mati:
    # - jangan jawab random chat (biar gak spam)
    # - tapi kalau user reply ke bot, kasih teks awal (help)
    if not cfg.get("enabled", True):
        if replied_to_bot:
            await msg.reply_text(start_text(), disable_web_page_preview=True)
        return

    # Kalau aktif:
    # jawab kalau user reply bot, atau chat enabled (aktif default True)
    # (di sini enabled True berarti boleh jawab semua non-command; sesuai request "gak perlu /chat on lagi")
    role = cfg.get("role") or DEFAULT_ROLE

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        answer = await asyncio.wait_for(
            call_siputzx(prompt=user_text, role=role, context=context),
            timeout=FAST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        answer = None

    if not answer:
        answer = fallback_reply(user_text)

    answer = limit_response(answer, max_sentences=MAX_SENTENCES, max_chars=MAX_CHARS)
    await msg.reply_text(answer, disable_web_page_preview=True)

# =========================
# ERROR HANDLER
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)

# =========================
# APP LIFECYCLE
# =========================
async def post_init(app: Application) -> None:
    global STORE
    STORE = MongoStorage(MONGO_URI, MONGO_DB, MONGO_COLL)
    await STORE.init()
    log.info("Mongo connected: %s / %s", MONGO_DB, MONGO_COLL)

async def post_shutdown(app: Application) -> None:
    await close_session(app)
    if STORE:
        await STORE.close()

# =========================
# MAIN
# =========================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("setrole", setrole_cmd))
    app.add_handler(CommandHandler("role", role_cmd))

    app.add_handler(
        MessageHandler(((filters.TEXT & ~filters.COMMAND) | filters.Caption), handle_message)
    )

    app.add_error_handler(on_error)

    log.info("Bot running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
