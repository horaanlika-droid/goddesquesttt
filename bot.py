import asyncio
import html
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("Заполните BOT_TOKEN и ADMIN_ID в .env")

DISCLAIMER = "\n\n<i>Напоминание: к Хозяйке обращаться строго на Вы</i>"

bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

DB = "game.db"


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

db.executescript("""
CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    password TEXT NOT NULL,
    time_limit INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level_id INTEGER NOT NULL,
    task_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    password TEXT NOT NULL,
    UNIQUE(level_id, task_number)
);

CREATE TABLE IF NOT EXISTS players (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    current_level INTEGER DEFAULT 1,
    current_task INTEGER DEFAULT 1,
    level_unlocked INTEGER DEFAULT 0,
    task_unlocked INTEGER DEFAULT 0,
    task_started_at TEXT,
    waiting_admin INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")


def now():
    return datetime.now()


def iso(dt):
    return dt.isoformat()


def parse_dt(value):
    return datetime.fromisoformat(value) if value else None


def setting(key, default=""):
    row = db.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()

    if not row:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?)",
            (key, default)
        )
        db.commit()
        return default

    return row["value"]


def set_setting(key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, value)
    )
    db.commit()


if not setting("gift"):
    set_setting(
        "gift",
        "Поздравляем! Вы успешно прошли игру. Ваш подарок: 🎁"
    )


# =========================================================
# DEFAULT GAME
# =========================================================

DEFAULT_TIMES = [
    60 * 60,
    45 * 60,
    35 * 60,
    25 * 60,
    20 * 60,
    15 * 60,
    10 * 60,
    7 * 60,
    5 * 60,
    3 * 60,
]

for level in range(1, 11):
    db.execute(
        """
        INSERT OR IGNORE INTO levels(id,title,password,time_limit)
        VALUES(?,?,?,?,?)
        """.replace("VALUES(?,?,?,?,?)", "VALUES(?,?,?,?)"),
        (
            level,
            f"Уровень {level}",
            f"LEVEL{level}",
            DEFAULT_TIMES[level - 1],
        )
    )

    for task in range(1, 4):
        db.execute(
            """
            INSERT OR IGNORE INTO tasks
            (level_id,task_number,text,password)
            VALUES(?,?,?,?)
            """,
            (
                level,
                task,
                f"Задание {task} уровня {level}. Отредактируйте его в админ-панели.",
                f"TASK{level}_{task}",
            )
        )

db.commit()


# =========================================================
# HELPERS
# =========================================================

def disclaimer(text):
    return text + DISCLAIMER


def player(user_id):
    return db.execute(
        "SELECT * FROM players WHERE user_id=?",
        (user_id,)
    ).fetchone()


def ensure_player(user):
    p = player(user.id)

    if not p:
        db.execute(
            """
            INSERT INTO players(user_id,username,first_name)
            VALUES(?,?,?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
            )
        )
        db.commit()
    else:
        db.execute(
            """
            UPDATE players
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id,
            )
        )
        db.commit()

    return player(user.id)


def level(level_id):
    return db.execute(
        "SELECT * FROM levels WHERE id=?",
        (level_id,)
    ).fetchone()


def task(level_id, task_number):
    return db.execute(
        """
        SELECT * FROM tasks
        WHERE level_id=? AND task_number=?
        """,
        (level_id, task_number)
    ).fetchone()


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)

    if h:
        return f"{h}:{m:02}:{s:02}"

    return f"{m}:{s:02}"


def player_name(p):
    if p["username"]:
        return f"@{p['username']}"

    return html.escape(p["first_name"] or str(p["user_id"]))


async def report(text):
    try:
        await bot.send_message(
            ADMIN_ID,
            disclaimer(text)
        )
    except Exception:
        pass


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🎮 Играть",
                callback_data="play"
            )],
            [
                InlineKeyboardButton(
                    text="📊 Мой прогресс",
                    callback_data="progress"
                ),
                InlineKeyboardButton(
                    text="📖 FAQ",
                    callback_data="faq"
                )
            ],
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📚 Уровни",
                callback_data="admin_levels"
            )],
            [InlineKeyboardButton(
                text="👥 Игроки",
                callback_data="admin_players"
            )],
            [InlineKeyboardButton(
                text="🎁 Подарок",
                callback_data="admin_gift"
            )],
        ]
    )


# =========================================================
# FSM
# =========================================================

class AdminState(StatesGroup):
    waiting_value = State()


# =========================================================
# START / FAQ
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    ensure_player(message.from_user)

    text = (
        "🎮 <b>Добро пожаловать!</b>\n\n"
        "Вас ждёт интерактивная игра из 10 уровней.\n"
        "В каждом уровне — 3 задания.\n\n"
        "Для доступа к уровням и заданиям понадобятся пароли."
    )

    if message.from_user.id == ADMIN_ID:
        text += "\n\n⚙️ Вы также можете открыть админ-панель командой /admin."

    await message.answer(
        disclaimer(text),
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "faq")
async def faq(callback: CallbackQuery):
    text = (
        "📖 <b>FAQ</b>\n\n"
        "<b>Как начать?</b>\n"
        "Нажмите «🎮 Играть» и введите пароль.\n\n"
        "<b>Где взять пароль?</b>\n"
        "Пароли выдаёт @milayaqueen.\n\n"
        "<b>Что делать после получения задания?</b>\n"
        "Выполните его и нажмите «✅ Я выполнил задание».\n\n"
        "<b>Что происходит дальше?</b>\n"
        "Хозяйка проверяет выполнение. После подтверждения "
        "открывается следующее задание.\n\n"
        "<b>Что будет, если время закончится?</b>\n"
        "Задание считается невыполненным.\n\n"
        "<b>Как пройти следующий уровень?</b>\n"
        "Нужно выполнить все три задания текущего уровня.\n\n"
        "<b>Что после 10-го уровня?</b>\n"
        "Вы получите подарок 🎁"
    )

    await callback.message.edit_text(
        disclaimer(text),
        reply_markup=main_menu()
    )
    await callback.answer()


# =========================================================
# PLAYER PROGRESS
# =========================================================

@dp.callback_query(F.data == "progress")
async def progress(callback: CallbackQuery):
    p = ensure_player(callback.from_user)

    if p["finished"]:
        text = "🏆 <b>Игра пройдена!</b>"
    else:
        text = (
            f"📊 <b>Ваш прогресс</b>\n\n"
            f"Уровень: <b>{p['current_level']}/10</b>\n"
            f"Задание: <b>{p['current_task']}/3</b>"
        )

    await callback.message.edit_text(
        disclaimer(text),
        reply_markup=main_menu()
    )
    await callback.answer()


# =========================================================
# PLAY
# =========================================================

@dp.callback_query(F.data == "play")
async def play(callback: CallbackQuery):
    p = ensure_player(callback.from_user)

    if p["finished"]:
        await callback.message.edit_text(
            disclaimer(
                "🏆 <b>Вы уже прошли игру!</b>\n\n" +
                setting("gift")
            ),
            reply_markup=main_menu()
        )
        await callback.answer()
        return

    lvl = level(p["current_level"])

    if not p["level_unlocked"]:
        text = (
            f"🔐 <b>{html.escape(lvl['title'])}</b>\n\n"
            "Введите пароль уровня."
        )
    elif not p["task_unlocked"]:
        t = task(p["current_level"], p["current_task"])

        text = (
            f"🔐 <b>Задание {p['current_task']}/3</b>\n\n"
            "Введите пароль задания."
        )
    else:
        await show_task(callback.message, callback.from_user.id)
        await callback.answer()
        return

    await callback.message.answer(disclaimer(text))
    await callback.answer()


@dp.message()
async def player_text(message: Message):
    if message.from_user.id == ADMIN_ID:
        return

    p = ensure_player(message.from_user)

    if p["finished"]:
        return

    lvl = level(p["current_level"])

    # Пароль уровня
    if not p["level_unlocked"]:
        if message.text.strip() == lvl["password"]:
            db.execute(
                """
                UPDATE players
                SET level_unlocked=1
                WHERE user_id=?
                """,
                (message.from_user.id,)
            )
            db.commit()

            await message.answer(
                disclaimer(
                    f"🔓 <b>{html.escape(lvl['title'])}</b>\n\n"
                    "Пароль принят.\n"
                    "Теперь введите пароль первого задания."
                )
            )
        else:
            await message.answer(
                disclaimer("❌ Неверный пароль.")
            )
        return

    # Пароль задания
    if not p["task_unlocked"]:
        t = task(p["current_level"], p["current_task"])

        if message.text.strip() == t["password"]:
            db.execute(
                """
                UPDATE players
                SET task_unlocked=1,
                    task_started_at=?
                WHERE user_id=?
                """,
                (
                    iso(now()),
                    message.from_user.id,
                )
            )
            db.commit()

            await show_task(message, message.from_user.id)
        else:
            await message.answer(
                disclaimer("❌ Неверный пароль задания.")
            )


async def show_task(message, user_id):
    p = player(user_id)
    lvl = level(p["current_level"])
    t = task(p["current_level"], p["current_task"])

    if not p["task_started_at"]:
        start_time = now()

        db.execute(
            """
            UPDATE players
            SET task_started_at=?
            WHERE user_id=?
            """,
            (iso(start_time), user_id)
        )
        db.commit()

    else:
        start_time = parse_dt(p["task_started_at"])

    elapsed = (now() - start_time).total_seconds()
    remaining = lvl["time_limit"] - elapsed

    if remaining <= 0:
        await timeout_player(user_id)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Я выполнил задание",
                callback_data="done"
            )],
            [InlineKeyboardButton(
                text="⏱ Сколько времени осталось?",
                callback_data="time"
            )],
        ]
    )

    text = (
        f"🎯 <b>Задание {p['current_task']}/3</b>\n\n"
        f"{html.escape(t['text'])}\n\n"
        f"⏱ <b>Осталось: {fmt_time(remaining)}</b>"
    )

    await message.answer(
        disclaimer(text),
        reply_markup=kb
    )


@dp.callback_query(F.data == "time")
async def time_left(callback: CallbackQuery):
    p = player(callback.from_user.id)

    if not p or not p["task_started_at"]:
        await callback.answer("Задание ещё не запущено.")
        return

    lvl = level(p["current_level"])
    started = parse_dt(p["task_started_at"])

    remaining = lvl["time_limit"] - (
        now() - started
    ).total_seconds()

    if remaining <= 0:
        await timeout_player(callback.from_user.id)
        await callback.answer()
        return

    await callback.answer(
        f"Осталось: {fmt_time(remaining)}",
        show_alert=True
    )


@dp.callback_query(F.data == "done")
async def done(callback: CallbackQuery):
    p = player(callback.from_user.id)

    if not p or not p["task_unlocked"]:
        await callback.answer("Задание ещё не открыто.")
        return

    lvl = level(p["current_level"])
    started = parse_dt(p["task_started_at"])

    remaining = lvl["time_limit"] - (
        now() - started
    ).total_seconds()

    if remaining <= 0:
        await timeout_player(callback.from_user.id)
        await callback.answer()
        return

    db.execute(
        """
        UPDATE players
        SET waiting_admin=1
        WHERE user_id=?
        """,
        (callback.from_user.id,)
    )
    db.commit()

    p = player(callback.from_user.id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"approve:{p['user_id']}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{p['user_id']}"
                ),
            ]
        ]
    )

    await bot.send_message(
        ADMIN_ID,
        disclaimer(
            "🔔 <b>Нужно подтвердить выполнение</b>\n\n"
            f"👤 Игрок: {player_name(p)}\n"
            f"🆔 ID: <code>{p['user_id']}</code>\n\n"
            f"📚 Уровень: {p['current_level']}\n"
            f"🎯 Задание: {p['current_task']}\n"
            f"⏱ Осталось: {fmt_time(remaining)}"
        ),
        reply_markup=kb
    )

    await callback.message.edit_text(
        disclaimer(
            "⏳ <b>Задание отправлено на проверку.</b>\n\n"
            "Ожидайте подтверждения Хозяйки."
        )
    )
    await callback.answer()


async def timeout_player(user_id):
    p = player(user_id)

    if not p:
        return

    db.execute(
        """
        UPDATE players
        SET task_started_at=NULL,
            task_unlocked=0,
            waiting_admin=0
        WHERE user_id=?
        """,
        (user_id,)
    )
    db.commit()

    await bot.send_message(
        user_id,
        disclaimer(
            "⏰ <b>Время вышло.</b>\n\n"
            "Задание считается невыполненным."
        )
    )

    await report(
        "⏰ <b>Время вышло</b>\n\n"
        f"👤 Игрок: {player_name(p)}\n"
        f"📚 Уровень: {p['current_level']}\n"
        f"🎯 Задание: {p['current_task']}"
    )


# =========================================================
# ADMIN
# =========================================================

@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        disclaimer("⚙️ <b>Админ-панель</b>"),
        reply_markup=admin_menu()
    )


@dp.callback_query(F.data == "admin_levels")
async def admin_levels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    rows = db.execute(
        "SELECT id,title FROM levels ORDER BY id"
    ).fetchall()

    buttons = []

    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                text=f"{row['id']}. {row['title']}",
                callback_data=f"edit_level:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="admin_back"
        )
    ])

    await callback.message.edit_text(
        disclaimer("📚 <b>Уровни</b>"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_level:"))
async def edit_level(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    level_id = int(callback.data.split(":")[1])
    lvl = level(level_id)

    text = (
        f"📖 <b>Уровень {level_id}</b>\n\n"
        f"Название: <b>{html.escape(lvl['title'])}</b>\n"
        f"Пароль: <code>{html.escape(lvl['password'])}</code>\n"
        f"Время: <b>{fmt_time(lvl['time_limit'])}</b>\n\n"
        "Выберите, что изменить:"
    )

    buttons = [
        [InlineKeyboardButton(
            text="✏️ Название",
            callback_data=f"set:title:{level_id}"
        )],
        [InlineKeyboardButton(
            text="🔐 Пароль уровня",
            callback_data=f"set:lpass:{level_id}"
        )],
        [InlineKeyboardButton(
            text="⏱ Время",
            callback_data=f"set:time:{level_id}"
        )],
    ]

    for n in range(1, 4):
        t = task(level_id, n)

        buttons.append([
            InlineKeyboardButton(
                text=f"📝 Задание {n}",
                callback_data=f"set:task:{level_id}:{n}"
            )
        ])

        buttons.append([
            InlineKeyboardButton(
                text=f"🔐 Пароль задания {n}",
                callback_data=f"set:tpass:{level_id}:{n}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="admin_levels"
        )
    ])

    await callback.message.edit_text(
        disclaimer(text),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set:"))
async def admin_set(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    parts = callback.data.split(":")
    kind = parts[1]
    level_id = int(parts[2])

    await state.update_data(
        kind=kind,
        level_id=level_id,
        task_number=int(parts[3]) if len(parts) > 3 else None
    )

    prompts = {
        "title": "Введите новое название уровня.",
        "lpass": "Введите новый пароль уровня.",
        "time": (
            "Введите время выполнения в секундах.\n"
            "Например: 3600 = 1 час."
        ),
        "task": "Введите новый текст задания.",
        "tpass": "Введите новый пароль задания.",
    }

    await state.set_state(AdminState.waiting_value)

    await callback.message.answer(
        disclaimer(f"✏️ {prompts[kind]}")
    )
    await callback.answer()


@dp.message(AdminState.waiting_value)
async def admin_value(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    kind = data["kind"]
    level_id = data["level_id"]
    task_number = data.get("task_number")

    value = message.text.strip()

    if not value:
        await message.answer(
            disclaimer("Значение не может быть пустым.")
        )
        return

    if kind == "title":
        db.execute(
            "UPDATE levels SET title=? WHERE id=?",
            (value, level_id)
        )

    elif kind == "lpass":
        db.execute(
            "UPDATE levels SET password=? WHERE id=?",
            (value, level_id)
        )

    elif kind == "time":
        try:
            seconds = int(value)

            if seconds < 1:
                raise ValueError

        except ValueError:
            await message.answer(
                disclaimer("Введите положительное число секунд.")
            )
            return

        db.execute(
            "UPDATE levels SET time_limit=? WHERE id=?",
            (seconds, level_id)
        )

    elif kind == "task":
        db.execute(
            """
            UPDATE tasks
            SET text=?
            WHERE level_id=? AND task_number=?
            """,
            (value, level_id, task_number)
        )

    elif kind == "tpass":
        db.execute(
            """
            UPDATE tasks
            SET password=?
            WHERE level_id=? AND task_number=?
            """,
            (value, level_id, task_number)
        )

    db.commit()

    await state.clear()

    await message.answer(
        disclaimer("✅ Сохранено."),
        reply_markup=admin_menu()
    )


# =========================================================
# ADMIN: APPROVE / REJECT
# =========================================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    user_id = int(callback.data.split(":")[1])
    p = player(user_id)

    if not p:
        await callback.answer("Игрок не найден.")
        return

    if not p["waiting_admin"]:
        await callback.answer("Это подтверждение уже обработано.")
        return

    level_id = p["current_level"]
    task_number = p["current_task"]

    if task_number < 3:
        db.execute(
            """
            UPDATE players
            SET current_task=?,
                task_unlocked=0,
                task_started_at=NULL,
                waiting_admin=0
            WHERE user_id=?
            """,
            (
                task_number + 1,
                user_id
            )
        )

        db.commit()

        await bot.send_message(
            user_id,
            disclaimer(
                f"✅ <b>Задание {task_number} подтверждено!</b>\n\n"
                f"Теперь откройте задание {task_number + 1}/3."
            )
        )

    else:
        if level_id < 10:
            db.execute(
                """
                UPDATE players
                SET current_level=?,
                    current_task=1,
                    level_unlocked=0,
                    task_unlocked=0,
                    task_started_at=NULL,
                    waiting_admin=0
                WHERE user_id=?
                """,
                (
                    level_id + 1,
                    user_id
                )
            )

            db.commit()

            next_level = level(level_id + 1)

            await bot.send_message(
                user_id,
                disclaimer(
                    f"🎉 <b>Уровень {level_id} пройден!</b>\n\n"
                    f"Следующий уровень: "
                    f"<b>{html.escape(next_level['title'])}</b>\n\n"
                    "Введите пароль уровня."
                )
            )

        else:
            db.execute(
                """
                UPDATE players
                SET finished=1,
                    waiting_admin=0,
                    task_unlocked=0,
                    task_started_at=NULL
                WHERE user_id=?
                """,
                (user_id,)
            )
            db.commit()

            gift = setting("gift")

            await bot.send_message(
                user_id,
                disclaimer(
                    "🏆 <b>ПОЗДРАВЛЯЕМ!</b>\n\n"
                    "Вы прошли все 10 уровней!\n\n"
                    f"{gift}"
                ),
                reply_markup=main_menu()
            )

            await report(
                "🏆 <b>ИГРА ПРОЙДЕНА</b>\n\n"
                f"👤 Игрок: {player_name(p)}\n"
                f"🆔 ID: <code>{user_id}</code>"
            )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Подтверждено ✅")


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    user_id = int(callback.data.split(":")[1])
    p = player(user_id)

    if not p:
        await callback.answer("Игрок не найден.")
        return

    db.execute(
        """
        UPDATE players
        SET waiting_admin=0
        WHERE user_id=?
        """,
        (user_id,)
    )
    db.commit()

    await bot.send_message(
        user_id,
        disclaimer(
            "❌ <b>Выполнение пока не подтверждено.</b>\n\n"
            "Обратитесь к Хозяйке."
        )
    )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отклонено")


# =========================================================
# ADMIN: GIFT
# =========================================================

@dp.callback_query(F.data == "admin_gift")
async def admin_gift(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.update_data(
        kind="gift"
    )
    await state.set_state(AdminState.waiting_value)

    await callback.message.answer(
        disclaimer(
            "🎁 Введите новый текст подарка."
        )
    )
    await callback.answer()


# Обрабатываем gift отдельно в общем FSM
_old_admin_value = admin_value


# =========================================================
# ADMIN: PLAYERS
# =========================================================

@dp.callback_query(F.data == "admin_players")
async def admin_players(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    rows = db.execute(
        """
        SELECT * FROM players
        ORDER BY finished DESC, current_level DESC
        """
    ).fetchall()

    if not rows:
        text = "👥 Игроков пока нет."
    else:
        lines = ["👥 <b>Игроки</b>\n"]

        for p in rows[:50]:
            status = "🏆" if p["finished"] else "🎮"

            lines.append(
                f"{status} {player_name(p)} — "
                f"{p['current_level']}/10, "
                f"{p['current_task']}/3"
            )

        text = "\n".join(lines)

    await callback.message.edit_text(
        disclaimer(text),
        reply_markup=admin_menu()
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.edit_text(
        disclaimer("⚙️ <b>Админ-панель</b>"),
        reply_markup=admin_menu()
    )
    await callback.answer()


# =========================================================
# FIX GIFT IN FSM
# =========================================================

@dp.message(AdminState.waiting_value)
async def admin_value_override(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    if data.get("kind") == "gift":
        value = message.text.strip()

        if not value:
            await message.answer(
                disclaimer("Текст подарка не может быть пустым.")
            )
            return

        set_setting("gift", value)

        await state.clear()

        await message.answer(
            disclaimer("🎁 Подарок обновлён."),
            reply_markup=admin_menu()
        )
        return

    kind = data["kind"]
    level_id = data["level_id"]
    task_number = data.get("task_number")

    value = message.text.strip()

    if not value:
        await message.answer(
            disclaimer("Значение не может быть пустым.")
        )
        return

    if kind == "title":
        db.execute(
            "UPDATE levels SET title=? WHERE id=?",
            (value, level_id)
        )

    elif kind == "lpass":
        db.execute(
            "UPDATE levels SET password=? WHERE id=?",
            (value, level_id)
        )

    elif kind == "time":
        try:
            seconds = int(value)

            if seconds < 1:
                raise ValueError

        except ValueError:
            await message.answer(
                disclaimer("Введите положительное число секунд.")
            )
            return

        db.execute(
            "UPDATE levels SET time_limit=? WHERE id=?",
            (seconds, level_id)
        )

    elif kind == "task":
        db.execute(
            """
            UPDATE tasks
            SET text=?
            WHERE level_id=? AND task_number=?
            """,
            (value, level_id, task_number)
        )

    elif kind == "tpass":
        db.execute(
            """
            UPDATE tasks
            SET password=?
            WHERE level_id=? AND task_number=?
            """,
            (value, level_id, task_number)
        )

    db.commit()

    await state.clear()

    await message.answer(
        disclaimer("✅ Сохранено."),
        reply_markup=admin_menu()
    )


# =========================================================
# RESET PLAYER
# =========================================================

@dp.message(Command("reset"))
async def reset_player(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            disclaimer(
                "Использование:\n"
                "<code>/reset TELEGRAM_ID</code>"
            )
        )
        return

    user_id = int(parts[1])

    db.execute(
        """
        UPDATE players
        SET current_level=1,
            current_task=1,
            level_unlocked=0,
            task_unlocked=0,
            task_started_at=NULL,
            waiting_admin=0,
            finished=0
        WHERE user_id=?
        """,
        (user_id,)
    )
    db.commit()

    await message.answer(
        disclaimer(f"🔄 Прогресс игрока <code>{user_id}</code> сброшен.")
    )

    try:
        await bot.send_message(
            user_id,
            disclaimer(
                "🔄 Ваш прогресс был сброшен.\n\n"
                "Игра начинается заново."
            ),
            reply_markup=main_menu()
        )
    except Exception:
        pass


# =========================================================
# TIMER WATCHDOG
# =========================================================

async def timer_watchdog():
    while True:
        try:
            rows = db.execute(
                """
                SELECT * FROM players
                WHERE task_unlocked=1
                  AND task_started_at IS NOT NULL
                  AND waiting_admin=0
                  AND finished=0
                """
            ).fetchall()

            for p in rows:
                lvl = level(p["current_level"])
                started = parse_dt(p["task_started_at"])

                if not started:
                    continue

                if (now() - started).total_seconds() >= lvl["time_limit"]:
                    await timeout_player(p["user_id"])

        except Exception as e:
            print("Timer error:", e)

        await asyncio.sleep(5)


# =========================================================
# START
# =========================================================

async def main():
    asyncio.create_task(timer_watchdog())

    print("Bot started")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())