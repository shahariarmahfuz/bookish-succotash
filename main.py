import logging
import re
import asyncio
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser

from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, BASE_URL, WEBHOOK_PATH, INBOUND_SECRET, DOMAIN
from db import (
    init_db,
    upsert_user,
    deactivate_email,
    get_user_by_address,
    get_chat_id,
    list_emails,
    seed_names_from_file,
    create_email_for_user,
)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

TELEGRAM_WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# =========================
# UI: Reply Keyboard (Menu)
# =========================
MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ new email"), KeyboardButton(text="📮 my emails")],
        [KeyboardButton(text="🗑 delete email"), KeyboardButton(text="ℹ️ help")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="↩️ back to menu")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# =========================
# FSM States
# =========================
class States(StatesGroup):
    waiting_delete_email = State()

# =========================
# Text helpers
# =========================
def _clean_text(s: str) -> str:
    s = s.replace("\x00", "")
    s = re.sub(r"\r\n", "\n", s)
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    return s.strip()

def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n\n", html)
    html = re.sub(r"(?is)<.*?>", "", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&lt;", "<", html)
    html = re.sub(r"&gt;", ">", html)
    html = re.sub(r"&amp;", "&", html)
    return _clean_text(html)

def parse_raw_email(raw_text: str):
    """
    Returns:
      (from_addr, subject, date_str, body_text, image_urls, truncated_guess)
    """
    if not raw_text:
        return ("", "", "", "", [], True)

    truncated_guess = len(raw_text) >= 3400
    image_urls = []

    try:
        raw_bytes = raw_text.encode("utf-8", errors="replace")
        msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

        from_addr = (msg.get("From") or "").strip()
        subject = (msg.get("Subject") or "").strip()
        date_str = (msg.get("Date") or "").strip()

        plain_parts = []
        html_parts = []

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = (part.get_content_disposition() or "").lower()
                if disp == "attachment":
                    continue
                if ctype == "text/plain":
                    try:
                        plain_parts.append(part.get_content())
                    except Exception:
                        pass
                elif ctype == "text/html":
                    try:
                        html_parts.append(part.get_content())
                    except Exception:
                        pass
        else:
            ctype = msg.get_content_type()
            try:
                content = msg.get_content()
            except Exception:
                content = ""
            if ctype == "text/plain":
                plain_parts.append(content)
            elif ctype == "text/html":
                html_parts.append(content)

        # external image urls from HTML
        if html_parts:
            html_all = "\n".join([str(h) for h in html_parts if str(h).strip()])
            found = re.findall(r'(?is)<img[^>]+src=["\'](https?://[^"\']+)["\']', html_all)
            seen = set()
            for u in found:
                u = u.strip()
                if u and u not in seen:
                    seen.add(u)
                    image_urls.append(u)
            image_urls = image_urls[:5]

        # body select
        body_text = ""
        if plain_parts and any(str(p).strip() for p in plain_parts):
            body_text = "\n\n".join([str(p) for p in plain_parts if str(p).strip()])
            body_text = _clean_text(body_text)
        elif html_parts and any(str(h).strip() for h in html_parts):
            body_text = _html_to_text("\n".join([str(h) for h in html_parts if str(h).strip()]))
        else:
            if "\n\n" in raw_text:
                body_text = _clean_text(raw_text.split("\n\n", 1)[1])
            else:
                body_text = ""

        return (from_addr, subject, date_str, body_text, image_urls, truncated_guess)

    except Exception:
        m_from = re.search(r"(?im)^from:\s*(.+)$", raw_text)
        m_sub = re.search(r"(?im)^subject:\s*(.+)$", raw_text)
        m_date = re.search(r"(?im)^date:\s*(.+)$", raw_text)

        from_addr = m_from.group(1).strip() if m_from else ""
        subject = m_sub.group(1).strip() if m_sub else ""
        date_str = m_date.group(1).strip() if m_date else ""

        body_text = ""
        if "\n\n" in raw_text:
            body_text = _clean_text(raw_text.split("\n\n", 1)[1])

        return (from_addr, subject, date_str, body_text, [], True)

async def send_multipart_email(chat_id: int, header: str, body: str, max_len: int = 3900):
    header = (header or "").strip()
    body = (body or "").strip()

    if not body:
        await bot.send_message(chat_id, header[:max_len])
        return

    def chunk_text(txt: str, size: int):
        return [txt[i:i+size] for i in range(0, len(txt), size)]

    part_label_template = "📩 New Email (9/9)\n\n"
    first_prefix = part_label_template + header + "\n\n"
    first_size = max_len - len(first_prefix)

    if first_size < 500:
        header = header[:1200]
        first_prefix = part_label_template + header + "\n\n"
        first_size = max_len - len(first_prefix)

    other_prefix = "📩 New Email (9/9)\n\n"
    other_size = max_len - len(other_prefix)

    first_size = max(first_size, 500)
    other_size = max(other_size, 1000)

    first_chunk = body[:first_size]
    rest = body[first_size:]
    rest_chunks = chunk_text(rest, other_size) if rest else []
    chunks = [first_chunk] + rest_chunks

    total = len(chunks)
    for i, ch in enumerate(chunks, start=1):
        label = f"📩 New Email ({i}/{total})\n\n"
        if i == 1:
            msg = label + header + "\n\n" + ch
        else:
            msg = label + ch
        await bot.send_message(chat_id, msg)

# =========================
# Lifespan (Railway friendly)
# =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB init + seed (try but don't block startup)
    try:
        init_db()
        seed_names_from_file("name.txt")
    except Exception as e:
        logging.exception("DB init/seed failed (continuing): %s", e)

    # webhook set in background (so app responds immediately)
    async def _set_webhook():
        try:
            await bot.set_webhook(TELEGRAM_WEBHOOK_URL, drop_pending_updates=True)
            logging.info(f"✅ Telegram webhook set: {TELEGRAM_WEBHOOK_URL}")
        except Exception as e:
            logging.exception("Webhook set failed: %s", e)

    asyncio.create_task(_set_webhook())

    yield

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# =========================
# BOT: Menu actions
# =========================
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    upsert_user(message.from_user.id, message.chat.id)
    await state.clear()
    await message.answer("✅ Ready!\nনিচের menu বাটন দিয়ে চালাও 👇", reply_markup=MENU_KB)

@router.message(F.text.lower() == "↩️ back to menu")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Menu", reply_markup=MENU_KB)

@router.message(F.text.lower() == "ℹ️ help")
async def help_menu(message: Message):
    await message.answer(
        "ℹ️ Help\n\n"
        "➕ new email → নতুন email বানাবে (name.txt থেকে)\n"
        "📮 my emails → তোমার সব email দেখাবে\n"
        "🗑 delete email → email disable করবে\n\n"
        "📩 ওই email এ কিছু এলে এখানে পাঠানো হবে ✅",
        reply_markup=MENU_KB,
    )

@router.message(F.text.lower() == "➕ new email")
async def new_email_btn(message: Message):
    upsert_user(message.from_user.id, message.chat.id)

    address = create_email_for_user(message.from_user.id, DOMAIN)
    if not address:
        await message.answer(
            "❌ name.txt এর নামগুলো শেষ হয়ে গেছে!\nনতুন নাম যোগ করে আবার চেষ্টা করো।",
            reply_markup=MENU_KB,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ create another", callback_data="new_again")],
            [InlineKeyboardButton(text="📮 my emails", callback_data="show_list")],
        ]
    )
    await message.answer(f"✅ New email created:\n`{address}`", parse_mode="Markdown", reply_markup=kb)

@router.callback_query(F.data == "new_again")
async def new_again_cb(call: CallbackQuery):
    address = create_email_for_user(call.from_user.id, DOMAIN)
    if not address:
        await call.message.answer(
            "❌ name.txt এর নামগুলো শেষ হয়ে গেছে!\nনতুন নাম যোগ করে আবার চেষ্টা করো।",
            reply_markup=MENU_KB,
        )
        await call.answer()
        return

    await call.message.answer(f"✅ New email created:\n`{address}`", parse_mode="Markdown", reply_markup=MENU_KB)
    await call.answer()

def build_list_inline(rows):
    b = InlineKeyboardBuilder()
    for addr, active, _ in rows[:15]:
        if active:
            b.button(text=f"🗑 {addr}", callback_data=f"del:{addr}")
    b.adjust(1)
    b.button(text="↩️ back to menu", callback_data="back_menu")
    b.adjust(1)
    return b.as_markup()

@router.message(F.text.lower() == "📮 my emails")
async def my_emails_btn(message: Message):
    upsert_user(message.from_user.id, message.chat.id)
    rows = list_emails(message.from_user.id, limit=50)
    if not rows:
        await message.answer("তোমার কোনো email নেই। ➕ new email চাপো।", reply_markup=MENU_KB)
        return

    lines = []
    for addr, is_active, created_at in rows[:20]:
        status = "✅" if is_active else "❌"
        lines.append(f"{status} {addr}  ({created_at})")

    await message.answer(
        "📮 তোমার emails (সর্বোচ্চ ২০টা দেখাচ্ছে):\n" + "\n".join(lines),
        reply_markup=build_list_inline(rows),
    )

@router.callback_query(F.data == "show_list")
async def show_list_cb(call: CallbackQuery):
    rows = list_emails(call.from_user.id, limit=50)
    if not rows:
        await call.message.answer("তোমার কোনো email নেই। ➕ new email চাপো।", reply_markup=MENU_KB)
        await call.answer()
        return

    lines = []
    for addr, is_active, created_at in rows[:20]:
        status = "✅" if is_active else "❌"
        lines.append(f"{status} {addr}  ({created_at})")

    await call.message.answer("📮 তোমার emails:\n" + "\n".join(lines), reply_markup=build_list_inline(rows))
    await call.answer()

@router.callback_query(F.data.startswith("del:"))
async def inline_delete_cb(call: CallbackQuery):
    addr = call.data.split("del:", 1)[1].strip().lower()
    ok = deactivate_email(addr, call.from_user.id)
    await call.message.answer("✅ Deleted/disabled" if ok else "❌ এই email তোমার না বা নাই।", reply_markup=MENU_KB)
    await call.answer()

@router.callback_query(F.data == "back_menu")
async def back_menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("✅ Menu", reply_markup=MENU_KB)
    await call.answer()

@router.message(F.text.lower() == "🗑 delete email")
async def delete_email_btn(message: Message, state: FSMContext):
    await state.set_state(States.waiting_delete_email)
    await message.answer(
        "🗑 কোন email ডিলিট/ডিসেবল করতে চাও?\nএখন ওই email address টা পাঠাও।",
        reply_markup=CANCEL_KB,
    )

@router.message(States.waiting_delete_email, F.text)
async def delete_email_input(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text == "↩️ back to menu":
        await state.clear()
        await message.answer("✅ Menu", reply_markup=MENU_KB)
        return

    if "@" not in text or "." not in text:
        await message.answer("❌ সঠিক email দাও (example: hasem1234@xneko.xyz) অথবা ↩️ back to menu চাপো।")
        return

    ok = deactivate_email(text, message.from_user.id)
    await state.clear()
    await message.answer("✅ Deleted/disabled" if ok else "❌ এই email তোমার না বা নাই।", reply_markup=MENU_KB)

dp.include_router(router)

# =========================
# Telegram webhook endpoint
# =========================
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid telegram update")
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "running"}

# =========================
# Inbound Email endpoint (Worker -> Railway)
# =========================
@app.post("/api/inbound-email")
async def inbound_email(request: Request):
    secret = request.headers.get("x-inbound-secret", "")
    if secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    to_addr = (payload.get("to") or "").lower().strip()
    from_payload = (payload.get("from") or "").strip()
    subject_payload = (payload.get("subject") or "").strip()
    raw_text = payload.get("text") or ""

    if not to_addr:
        raise HTTPException(status_code=400, detail="Missing 'to'")

    telegram_id = get_user_by_address(to_addr)
    if not telegram_id:
        return {"ok": True, "ignored": True}

    chat_id = get_chat_id(telegram_id)
    if not chat_id:
        return {"ok": True, "ignored": True}

    from_parsed, subject_parsed, date_parsed, body_text, image_urls, truncated = parse_raw_email(raw_text)

    from_addr = from_parsed or from_payload or "unknown"
    subject = subject_parsed or subject_payload or "(no subject)"
    date_line = f"🕒 Date: {date_parsed}\n" if date_parsed else ""

    if not body_text:
        body_text = "⚠️ Email body পাওয়া যায়নি (সম্ভবত header বড় ছিল এবং worker raw ট্রিম করেছে)।"
    if truncated:
        body_text += "\n\nℹ️ Note: raw email trimmed, full body নাও আসতে পারে।"

    header = (
        f"📬 To: {to_addr}\n"
        f"👤 From: {from_addr}\n"
        f"📝 Subject: {subject}\n"
        f"{date_line}".strip()
    )

    await send_multipart_email(chat_id, header, body_text)

    for url in image_urls[:5]:
        try:
            await bot.send_photo(chat_id, photo=url, caption="🖼️ Image from email")
        except Exception:
            pass

    return {"ok": True}
