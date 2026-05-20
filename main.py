import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@echoesapp")
DB_PATH = os.getenv("DB_PATH", "capsules.db")
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow")

WELCOME_IMAGE = "welcome.png"
SUCCESS_IMAGE = "success.png"
DELIVERY_IMAGE = "delivery.png"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

STATE_WAITING_MEMORY = "waiting_memory"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capsules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                memory_text TEXT,
                photo_file_id TEXT,
                send_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_sent INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                memory_text TEXT,
                photo_file_id TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        capsule_columns = [row[1] for row in conn.execute("PRAGMA table_info(capsules)").fetchall()]
        if "photo_file_id" not in capsule_columns:
            conn.execute("ALTER TABLE capsules ADD COLUMN photo_file_id TEXT")

        state_columns = [row[1] for row in conn.execute("PRAGMA table_info(user_states)").fetchall()]
        if "photo_file_id" not in state_columns:
            conn.execute("ALTER TABLE user_states ADD COLUMN photo_file_id TEXT")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def image_exists(path: str) -> bool:
    return os.path.exists(path)


def format_dt(iso_dt: str) -> str:
    dt = datetime.fromisoformat(iso_dt)
    local_dt = dt.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    return local_dt.strftime("%d.%m.%Y в %H:%M")


def set_state(user_id: int, state: str | None, memory_text: str | None = None, photo_file_id: str | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if state is None:
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        else:
            conn.execute("""
                INSERT INTO user_states (user_id, state, memory_text, photo_file_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET state = excluded.state,
                              memory_text = excluded.memory_text,
                              photo_file_id = excluded.photo_file_id,
                              updated_at = excluded.updated_at
            """, (user_id, state, memory_text, photo_file_id, now_utc().isoformat()))


def get_state(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT state, memory_text, photo_file_id FROM user_states WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row if row else (None, None, None)


def save_capsule(user_id: int, username: str | None, memory_text: str | None, photo_file_id: str | None, send_at: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO capsules (user_id, username, memory_text, photo_file_id, send_at, created_at, is_sent)
            VALUES (?, ?, ?, ?, ?, ?, 0)
        """, (user_id, username, memory_text or "", photo_file_id, send_at.isoformat(), now_utc().isoformat()))


def get_due_capsules():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("""
            SELECT id, user_id, memory_text, photo_file_id, created_at
            FROM capsules
            WHERE is_sent = 0 AND send_at <= ?
            ORDER BY send_at ASC
        """, (now_utc().isoformat(),)).fetchall()


def mark_sent(capsule_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE capsules SET is_sent = 1 WHERE id = ?", (capsule_id,))


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("Subscription check failed: %s", e)
        return False


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подписаться на канал 🤍", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("Я подписалась / подписался ✨", callback_data="check_subscription")],
    ])


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Создать капсулу 💌", callback_data="create_capsule")]])


def delay_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Через 1 минуту — тест", callback_data="delay_1m")],
        [InlineKeyboardButton("Через 1 день", callback_data="delay_1d")],
        [InlineKeyboardButton("Через неделю", callback_data="delay_7d")],
        [InlineKeyboardButton("Через месяц", callback_data="delay_30d")],
        [InlineKeyboardButton("Через год", callback_data="delay_365d")],
    ])


async def reply_with_png(message, text: str, image_path: str, reply_markup=None) -> None:
    if image_exists(image_path):
        with open(image_path, "rb") as image:
            await message.reply_photo(photo=image, caption=text, reply_markup=reply_markup)
    else:
        await message.reply_text(text, reply_markup=reply_markup)


async def send_success_with_png(query, text: str, reply_markup=None) -> None:
    if image_exists(SUCCESS_IMAGE):
        try:
            await query.delete_message()
        except Exception:
            pass
        with open(SUCCESS_IMAGE, "rb") as image:
            await query.message.chat.send_photo(photo=image, caption=text, reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    base_text = (
        "Привет 🤍\\n\\n"
        "Это маленькая капсула времени для важных воспоминаний. "
        "Оставь здесь мысль, момент, фото или сообщение, которое не хочешь потерять — "
        "и я бережно верну его тебе позже ✨"
    )

    if not await is_subscribed(context, user_id):
        await reply_with_png(
            update.effective_message,
            base_text + "\\n\\nЧтобы пользоваться ботом, подпишись на канал ēchoēs.",
            WELCOME_IMAGE,
            subscribe_keyboard(),
        )
        return

    await reply_with_png(update.effective_message, base_text, WELCOME_IMAGE, start_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_welcome(update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "check_subscription":
        if await is_subscribed(context, user_id):
            await query.edit_message_text(
                "Спасибо, подписка есть 🤍\\n\\nТеперь можно создать капсулу времени.",
                reply_markup=start_keyboard(),
            )
        else:
            await query.edit_message_text(
                "Похоже, подписки пока нет 🥲\\n\\nПодпишись на канал, а потом нажми кнопку проверки ещё раз.",
                reply_markup=subscribe_keyboard(),
            )
        return

    if not await is_subscribed(context, user_id):
        await query.edit_message_text("Чтобы пользоваться ботом, подпишись на канал ēchoēs 🤍", reply_markup=subscribe_keyboard())
        return

    if query.data == "create_capsule":
        set_state(user_id, STATE_WAITING_MEMORY)
        await query.edit_message_text(
            "Что хочешь сохранить? 💌\\n\\n"
            "Это может быть текст, фото, фото с подписью, маленькое послание себе "
            "или что-то, что не хочется потерять."
        )
        return

    delays = {
        "delay_1m": timedelta(minutes=1),
        "delay_1d": timedelta(days=1),
        "delay_7d": timedelta(days=7),
        "delay_30d": timedelta(days=30),
        "delay_365d": timedelta(days=365),
    }

    if query.data in delays:
        _, memory_text, photo_file_id = get_state(user_id)

        if not memory_text and not photo_file_id:
            await query.edit_message_text("Я не нашла содержимое капсулы 🥲\\n\\nДавай создадим её заново.", reply_markup=start_keyboard())
            set_state(user_id, None)
            return

        save_capsule(user_id, query.from_user.username, memory_text, photo_file_id, now_utc() + delays[query.data])
        set_state(user_id, None)

        readable = {
            "delay_1m": "через 1 минуту",
            "delay_1d": "через 1 день",
            "delay_7d": "через неделю",
            "delay_30d": "через месяц",
            "delay_365d": "через год",
        }[query.data]

        await send_success_with_png(
            query,
            f"Готово ✨\\n\\nЯ бережно сохраню это и верну тебе {readable} 💌",
            InlineKeyboardMarkup([[InlineKeyboardButton("Создать ещё одну капсулу", callback_data="create_capsule")]]),
        )


async def handle_text_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not await is_subscribed(context, user_id):
        await update.message.reply_text("Чтобы пользоваться ботом, подпишись на канал ēchoēs 🤍", reply_markup=subscribe_keyboard())
        return

    state, _, _ = get_state(user_id)

    if state == STATE_WAITING_MEMORY:
        memory_text = None
        photo_file_id = None

        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            memory_text = update.message.caption.strip() if update.message.caption else ""
        elif update.message.text:
            memory_text = update.message.text.strip()

        if not memory_text and not photo_file_id:
            await update.message.reply_text("Кажется, я не смогл сохранить это 🥲 Попробуй отправить текст или фото.")
            return

        set_state(user_id, STATE_WAITING_MEMORY, memory_text, photo_file_id)
        await update.message.reply_text("Воспоминание сохранено\\n\\nТеперь выбери, когда вернуть это тебе.", reply_markup=delay_keyboard())
        return

    await update.message.reply_text("Я могу сохранить для тебя капсулу времени 💌", reply_markup=start_keyboard())


async def send_capsule(app: Application, user_id: int, memory_text: str, photo_file_id: str | None, created_at: str) -> None:
    saved_date = format_dt(created_at)

    if memory_text:
        caption = (
            "Твоя капсула времени 💌\\n\\n"
            f"Ты сохранил(а) это {saved_date}:\\n\\n"
            f"“{memory_text}”\\n\\n"
            "Иногда важные вещи просто стоит услышать снова"
        )
    else:
        caption = (
            "Твоя капсула времени 💌\\n\\n"
            f"Ты сохранил(а) это {saved_date}.\\n\\n"
            "Иногда важные вещи просто стоит увидеть снова"
        )

    # If user saved their own photo, return that photo with the capsule text.
    # If capsule is text-only, send branded delivery.png with the capsule text.
    if photo_file_id:
        await app.bot.send_photo(chat_id=user_id, photo=photo_file_id, caption=caption)
    elif image_exists(DELIVERY_IMAGE):
        with open(DELIVERY_IMAGE, "rb") as image:
            await app.bot.send_photo(chat_id=user_id, photo=image, caption=caption)
    else:
        await app.bot.send_message(chat_id=user_id, text=caption)


async def capsule_sender(app: Application) -> None:
    while True:
        for capsule_id, user_id, memory_text, photo_file_id, created_at in get_due_capsules():
            try:
                await send_capsule(app, user_id, memory_text, photo_file_id, created_at)
                mark_sent(capsule_id)
            except Exception as e:
                logging.warning("Failed to send capsule %s: %s", capsule_id, e)

        await asyncio.sleep(30)


async def post_init(app: Application) -> None:
    app.create_task(capsule_sender(app))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add it to Railway Environment Variables.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_text_or_photo))
    logging.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
