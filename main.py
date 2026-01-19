import os
import json
import asyncio
import logging
import re
import time
import random
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

DEFAULT_ROLE = (
    "Kamu adalah seorang yang lagi ngebalesin chat di dalam group chat. Balasanmu harus menggunakan bahasa gaul sehari-hari ala anak gen z, sangat singkat, dan responsif. Balasan tidak perlu selalu formal atau informatif, fokus saja pada reaksi emosional atau komentar cepat. \n"
    "Jangan pernah menawarkan bantuan dan bertanya. \n"
    "Jangan ngaku bot atau AI. \n"
    "Jawaban singkat Maksimal 10 - 15 karakter. \n"
)

SIPUTZX_GPT3_URL = os.getenv("SIPUTZX_GPT3_URL", "https://api.siputzx.my.id/api/ai/gpt3").strip()
SIPUTZX_GPT_URL = os.getenv("SIPUTZX_GPT_URL", "https://apis-liart.vercel.app/api/gpt").strip()

MAX_SENTENCES = int(os.getenv("MAX_SENTENCES", "1"))
MAX_CHARS = int(os.getenv("MAX_CHARS", "15"))

MENTION_REGEX = re.compile(r"@\w+", re.UNICODE)

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://aseppp:aseppp@cluster0.bocyf5q.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0",
).strip()
MONGO_DB = os.getenv("MONGO_DB", "aseppp").strip()
MONGO_COLL = os.getenv("MONGO_COLL", "chat_cfg").strip()
if not MONGO_URI:
    raise SystemExit("ENV MONGO_URI belum diisi.")

# Rate limit (anti spam hit API)
USER_COOLDOWN_SEC = float(os.getenv("USER_COOLDOWN_SEC", "2.0"))

# Circuit breaker (kalau Siputzx down, jangan dipaksa)
CB_FAIL_THRESHOLD = int(os.getenv("CB_FAIL_THRESHOLD", "3"))          # gagal berapa kali
CB_WINDOW_SEC = int(os.getenv("CB_WINDOW_SEC", "60"))                # dalam window berapa detik
CB_COOLDOWN_SEC = int(os.getenv("CB_COOLDOWN_SEC", "180"))           # cooldown berapa detik saat down

# Retry ringan (harus tetap muat di FAST_TIMEOUT)
RETRY_BACKOFFS = [0.0, 0.6, 1.2]  # total ~ < 2 detik + jitter

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
# HELP TEXT
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
    # Default timeout dibuat long, per-request nanti pakai timeout sendiri (FAST_TIMEOUT)
    timeout = aiohttp.ClientTimeout(total=30)
    sess = aiohttp.ClientSession(timeout=timeout)
    context.application.bot_data["aiohttp_session"] = sess
    return sess

async def close_session(app: Application) -> None:
    sess = app.bot_data.get("aiohttp_session")
    if sess and not sess.closed:
        await sess.close()

# =========================
# CIRCUIT BREAKER (in-memory)
# =========================
def _cb_state(app: Application) -> Dict[str, Any]:
    st = app.bot_data.get("siputzx_cb")
    if not st:
        st = {"fail_times": [], "down_until": 0.0}
        app.bot_data["siputzx_cb"] = st
    return st

def cb_allow(app: Application) -> bool:
    st = _cb_state(app)
    return time.time() >= float(st.get("down_until", 0.0))

def cb_record_success(app: Application) -> None:
    st = _cb_state(app)
    st["fail_times"] = []
    st["down_until"] = 0.0

def cb_record_failure(app: Application) -> None:
    st = _cb_state(app)
    now = time.time()
    window = CB_WINDOW_SEC
    ft = [t for t in st.get("fail_times", []) if now - t <= window]
    ft.append(now)
    st["fail_times"] = ft
    if len(ft) >= CB_FAIL_THRESHOLD:
        st["down_until"] = now + CB_COOLDOWN_SEC

# =========================
# HELPERS
# =========================
def looks_like_html(raw: str) -> bool:
    t = (raw or "").lstrip().lower()
    return t.startswith("<!doctype") or t.startswith("<html") or t.startswith("<")

def head(raw: str, n: int = 200) -> str:
    raw = raw or ""
    raw = raw.replace("\n", " ").replace("\r", " ")
    return raw[:n]

async def http_get_text(session: aiohttp.ClientSession, url: str, total_timeout: float) -> tuple[int, str, str]:
    # return (status, text, content_type)
    req_timeout = aiohttp.ClientTimeout(total=total_timeout)
    async with session.get(url, timeout=req_timeout) as r:
        raw = await r.text(errors="ignore")
        ctype = (r.headers.get("Content-Type") or "").lower()
        return r.status, raw, ctype

# =========================
# AI CALL (Siputzx) - FIXED
# =========================
async def call_siputzx(prompt: str, role: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    prompt = (prompt or "").strip()
    role = (role or DEFAULT_ROLE).strip()
    if not prompt:
        return None

    app = context.application
    if not cb_allow(app):
        # Siputzx lagi "di-mute" karena error bertubi-tubi
        return None

    session = await get_session(context)

    # Biar retry masih muat di FAST_TIMEOUT, kita pakai timeout request lebih kecil
    # dan retry ringan. Total handling tetap dibungkus asyncio.wait_for di handler.
    per_try_timeout = max(1.2, min(FAST_TIMEOUT - 0.3, 3.0))

    # -------------------------
    # 1) gpt3: prompt(role) + content(user)
    # -------------------------
    params = {"prompt": role, "content": prompt}
    url_gpt3 = f"{SIPUTZX_GPT3_URL}?{urlencode(params)}"

    last_err: Optional[str] = None
    for d in RETRY_BACKOFFS:
        if d:
            await asyncio.sleep(d + random.uniform(0, 0.25))

        try:
            status, raw, ctype = await http_get_text(session, url_gpt3, per_try_timeout)

            # HTML guard (banyak 502 balikin halaman HTML)
            if looks_like_html(raw):
                cb_record_failure(app)
                last_err = f"gpt3 html status={status} head={head(raw)}"
                continue

            if status == 200:
                # coba parse json, kalau gagal ya raw saja
                try:
                    js = json.loads(raw)
                except Exception:
                    val = raw.strip()
                    if val:
                        cb_record_success(app)
                        return val
                    cb_record_failure(app)
                    last_err = f"gpt3 bad raw empty head={head(raw)}"
                    continue

                if isinstance(js, dict):
                    val = js.get("data")
                    if isinstance(val, str) and val.strip():
                        cb_record_success(app)
                        return val.strip()
                    if isinstance(val, dict):
                        c = val.get("content")
                        if isinstance(c, str) and c.strip():
                            cb_record_success(app)
                            return c.strip()
                    for v in js.values():
                        if isinstance(v, str) and v.strip():
                            cb_record_success(app)
                            return v.strip()

                cb_record_failure(app)
                last_err = f"gpt3 json no usable fields head={head(raw)}"
                continue

            # non-200
            cb_record_failure(app)
            log.warning("Siputzx gpt3 non-200: %s %s", status, head(raw))
            last_err = f"gpt3 non200 status={status} head={head(raw)}"
        except Exception as e:
            cb_record_failure(app)
            last_err = f"gpt3 exc {type(e).__name__}: {e}"
            log.warning("Error call_siputzx (gpt3): %s", last_err)

    # -------------------------
    # 2) fallback: /api/gpt?text=
    # -------------------------
    params = {"text": prompt}
    url_fb = f"{SIPUTZX_GPT_URL}?{urlencode(params)}"

    for d in RETRY_BACKOFFS:
        if d:
            await asyncio.sleep(d + random.uniform(0, 0.25))

        try:
            status, raw, ctype = await http_get_text(session, url_fb, per_try_timeout)

            if looks_like_html(raw):
                cb_record_failure(app)
                last_err = f"fallback html status={status} head={head(raw)}"
                continue

            if status != 200:
                cb_record_failure(app)
                log.warning("Siputzx gpt fallback non-200: %s %s", status, head(raw))
                last_err = f"fallback non200 status={status} head={head(raw)}"
                continue

            # parse json
            try:
                js = json.loads(raw)
            except Exception:
                cb_record_failure(app)
                last_err = f"fallback invalid json head={head(raw)}"
                continue

            if isinstance(js, dict):
                data_obj = js.get("data")
                if isinstance(data_obj, dict):
                    content = data_obj.get("content")
                    if isinstance(content, str) and content.strip():
                        cb_record_success(app)
                        return content.strip()

                for k in ("result", "answer", "message", "data"):
                    v = js.get(k)
                    if isinstance(v, str) and v.strip():
                        cb_record_success(app)
                        return v.strip()

            cb_record_failure(app)
            last_err = f"fallback json no usable fields head={head(raw)}"
        except Exception as e:
            cb_record_failure(app)
            last_err = f"fallback exc {type(e).__name__}: {e}"
            log.warning("Error call_siputzx (fallback): %s", last_err)

    # Kalau sampai sini, berarti Siputzx gagal.
    # Circuit breaker sudah kita update via record_failure().
    if last_err:
        log.warning("Siputzx failed: %s", last_err)
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
def _user_rate_limited(app: Application, user_id: int) -> bool:
    now = time.time()
    m: Dict[int, float] = app.bot_data.get("user_last_ts", {})
    last = float(m.get(user_id, 0.0))
    if now - last < USER_COOLDOWN_SEC:
        return True
    m[user_id] = now
    app.bot_data["user_last_ts"] = m
    return False

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

    if not cfg.get("enabled", True):
        if replied_to_bot:
            await msg.reply_text(start_text(), disable_web_page_preview=True)
        return

    # Rate limit per user (biar gak spam hit API)
    if msg.from_user and _user_rate_limited(context.application, msg.from_user.id):
        # jangan bales terus-terusan, biar bot gak spam juga
        return

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
