import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from flask import Flask

# ========== КОНФИГ ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
logging.basicConfig(level=logging.INFO)

# ========== ЧАСОВОЙ ПОЯС (МСК = UTC+3) ==========
MSK = timezone(timedelta(hours=3))

def now_msk():
    return datetime.now(MSK).replace(tzinfo=None)

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
web_app = Flask(__name__)

@web_app.route('/')
@web_app.route('/health')
def health():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port, use_reloader=False)

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "notes.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            title TEXT,
            description TEXT,
            done INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            remind_at TEXT,
            repeat_daily INTEGER DEFAULT 0,
            hour INTEGER,
            minute INTEGER
        )''')
        await db.commit()

async def add_note(user_id: int, category: str, title: str, description: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO notes (user_id, category, title, description) VALUES (?, ?, ?, ?)",
            (user_id, category, title, description)
        )
        await db.commit()

async def get_notes(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, title, description, done FROM notes WHERE user_id = ? ORDER BY id",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def search_notes(user_id: int, query: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, title, description, done FROM notes WHERE user_id = ? AND (title LIKE ? OR description LIKE ?) ORDER BY id",
            (user_id, f"%{query}%", f"%{query}%")
        ) as cursor:
            return await cursor.fetchall()

async def toggle_note_done(note_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT done FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)) as cursor:
            row = await cursor.fetchone()
        if row:
            new_done = 0 if row[0] else 1
            await db.execute("UPDATE notes SET done = ? WHERE id = ? AND user_id = ?", (new_done, note_id, user_id))
            await db.commit()

async def delete_note(note_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        await db.commit()

async def add_reminder(user_id: int, text: str, remind_at: datetime, repeat_daily: int, hour: int, minute: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders (user_id, text, remind_at, repeat_daily, hour, minute) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, remind_at.isoformat(), repeat_daily, hour, minute)
        )
        await db.commit()

async def get_reminders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, remind_at, repeat_daily, hour, minute FROM reminders WHERE user_id = ? ORDER BY id",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def get_due_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        now = now_msk().isoformat()
        async with db.execute(
            "SELECT id, user_id, text, repeat_daily, hour, minute FROM reminders WHERE remind_at <= ?",
            (now,)
        ) as cursor:
            return await cursor.fetchall()

async def delete_reminder(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await db.commit()

async def update_reminder_time(reminder_id: int, new_time: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET remind_at = ? WHERE id = ?", (new_time.isoformat(), reminder_id))
        await db.commit()

# ========== СОСТОЯНИЯ (FSM) ==========
class NoteStates(StatesGroup):
    waiting_category = State()
    waiting_title = State()
    waiting_description = State()

class ReminderStates(StatesGroup):
    waiting_text = State()
    waiting_date = State()
    waiting_time = State()
    waiting_repeat = State()

class SearchStates(StatesGroup):
    waiting_query = State()

class DeleteStates(StatesGroup):
    waiting_id = State()

# ========== КЛАВИАТУРЫ ==========
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Добавить заметку", callback_data="add_note")],
        [InlineKeyboardButton(text="📋 Мои заметки", callback_data="list_notes")],
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_notes")],
        [InlineKeyboardButton(text="⏰ Добавить напоминание", callback_data="add_reminder")],
        [InlineKeyboardButton(text="🔔 Мои напоминания", callback_data="list_reminders")],
        [InlineKeyboardButton(text="🗑 Удалить заметку", callback_data="delete_note")],
        [InlineKeyboardButton(text="📤 Экспорт заметок", callback_data="export_notes")]
    ])

def back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")]
    ])

def category_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Работа", callback_data="cat_Работа")],
        [InlineKeyboardButton(text="🏠 Личное", callback_data="cat_Личное")],
        [InlineKeyboardButton(text="🛒 Покупки", callback_data="cat_Покупки")],
        [InlineKeyboardButton(text="💡 Идеи", callback_data="cat_Идеи")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])

def repeat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔂 Один раз", callback_data="repeat_no")],
        [InlineKeyboardButton(text="🔁 Каждый день", callback_data="repeat_daily")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])

# ========== РОУТЕР ==========
router = Router()

# ========== КОМАНДА /start ==========
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот для заметок и напоминаний.\n"
        "🕐 Время указывается по МСК.\n\nВыбери действие:",
        reply_markup=main_menu()
    )

# ========== ВОЗВРАТ В МЕНЮ ==========
@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🏠 Главное меню:\n\nВыбери действие:",
        reply_markup=main_menu()
    )
    await callback.answer()

# ========== ДОБАВЛЕНИЕ ЗАМЕТКИ ==========
@router.callback_query(F.data == "add_note")
async def add_note_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(NoteStates.waiting_category)
    await callback.message.edit_text(
        "📁 Выбери категорию для заметки:",
        reply_markup=category_menu()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("cat_"))
async def note_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.replace("cat_", "")
    await state.update_data(category=category)
    await state.set_state(NoteStates.waiting_title)
    await callback.message.edit_text(
        f"📝 Категория: {category}\n\nНапиши заголовок заметки:",
        reply_markup=back_button()
    )
    await callback.answer()

@router.message(NoteStates.waiting_title)
async def note_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(NoteStates.waiting_description)
    await message.answer(
        "📝 Теперь напиши описание (или нажми «Пропустить»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_description")]
        ])
    )

@router.callback_query(F.data == "skip_description")
async def skip_description(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await add_note(callback.from_user.id, data["category"], data["title"], "")
    await state.clear()
    await callback.message.edit_text("✅ Заметка добавлена без описания!", reply_markup=back_button())
    await callback.answer()

@router.message(NoteStates.waiting_description)
async def note_description(message: Message, state: FSMContext):
    data = await state.get_data()
    await add_note(message.from_user.id, data["category"], data["title"], message.text)
    await state.clear()
    await message.answer("✅ Заметка добавлена!", reply_markup=back_button())

# ========== СПИСОК ЗАМЕТОК ==========
@router.callback_query(F.data == "list_notes")
async def list_notes(callback: CallbackQuery):
    notes = await get_notes(callback.from_user.id)
    if not notes:
        await callback.message.edit_text("📋 У тебя пока нет заметок.", reply_markup=back_button())
        await callback.answer()
        return

    text = "📋 Твои заметки:\n\n"
    buttons = []
    for note_id, category, title, description, done in notes:
        status = "✅" if done else "⬜"
        text += f"{status} {note_id}. [{category}] {title}\n"
        if description:
            text += f"   └ {description}\n"
        text += "\n"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {note_id}. {title[:25]}",
            callback_data=f"toggle_{note_id}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("toggle_"))
async def toggle_note(callback: CallbackQuery):
    note_id = int(callback.data.replace("toggle_", ""))
    await toggle_note_done(note_id, callback.from_user.id)
    await list_notes(callback)

# ========== ПОИСК ==========
@router.callback_query(F.data == "search_notes")
async def search_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_query)
    await callback.message.edit_text("🔍 Напиши слово для поиска:", reply_markup=back_button())
    await callback.answer()

@router.message(SearchStates.waiting_query)
async def search_query(message: Message, state: FSMContext):
    notes = await search_notes(message.from_user.id, message.text)
    await state.clear()
    if not notes:
        await message.answer("🔍 Ничего не найдено.", reply_markup=back_button())
        return
    result = f"🔍 Найдено {len(notes)} заметок:\n\n"
    for note_id, category, title, description, done in notes:
        status = "✅" if done else "⬜"
        result += f"{status} {note_id}. [{category}] {title}\n"
        if description:
            result += f"   └ {description}\n"
        result += "\n"
    await message.answer(result, reply_markup=back_button())

# ========== НАПОМИНАНИЯ ==========
@router.callback_query(F.data == "add_reminder")
async def add_reminder_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReminderStates.waiting_text)
    await callback.message.edit_text("⏰ Напиши текст напоминания:", reply_markup=back_button())
    await callback.answer()

@router.message(ReminderStates.waiting_text)
async def reminder_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(ReminderStates.waiting_date)
    await message.answer(
        "📅 Теперь напиши дату в формате ДД.ММ.ГГГГ (например, 25.12.2026).\n"
        "Если хочешь напомнить сегодня — напиши «сегодня».",
        reply_markup=back_button()
    )

@router.message(ReminderStates.waiting_date)
async def reminder_date(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    now = now_msk()

    if text == "сегодня":
        remind_date = now.date()
    else:
        try:
            remind_date = datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            await message.answer("❌ Неверный формат. Напиши дату как 25.12.2026 или «сегодня».")
            return

    await state.update_data(remind_date=remind_date.isoformat())
    await state.set_state(ReminderStates.waiting_time)
    await message.answer("🕐 Теперь напиши время в формате ЧЧ:ММ (например, 18:30) по МСК.", reply_markup=back_button())

@router.message(ReminderStates.waiting_time)
async def reminder_time(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        hour, minute = map(int, text.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный формат. Напиши как 18:30.")
        return

    await state.update_data(hour=hour, minute=minute)
    await state.set_state(ReminderStates.waiting_repeat)
    await message.answer("🔁 Повторять напоминание?", reply_markup=repeat_menu())

@router.callback_query(F.data.startswith("repeat_"))
async def reminder_repeat(callback: CallbackQuery, state: FSMContext):
    repeat_daily = 1 if callback.data == "repeat_daily" else 0
    data = await state.get_data()
    remind_date = datetime.fromisoformat(data["remind_date"]).date()
    remind_at = datetime.combine(remind_date, datetime.min.time()).replace(
        hour=data["hour"], minute=data["minute"], second=0, microsecond=0
    )

    if remind_at <= now_msk():
        await callback.message.edit_text("❌ Это время уже прошло. Начни заново: /start", reply_markup=back_button())
        await state.clear()
        await callback.answer()
        return

    await add_reminder(
        callback.from_user.id,
        data["text"],
        remind_at,
        repeat_daily,
        data["hour"],
        data["minute"]
    )
    await state.clear()

    repeat_text = "каждый день" if repeat_daily else "один раз"
    await callback.message.edit_text(
        f"✅ Напоминание установлено на {remind_at.strftime('%d.%m.%Y %H:%M')} МСК ({repeat_text})",
        reply_markup=back_button()
    )
    await callback.answer()

# ========== СПИСОК НАПОМИНАНИЙ ==========
@router.callback_query(F.data == "list_reminders")
async def list_reminders(callback: CallbackQuery):
    reminders = await get_reminders(callback.from_user.id)
    if not reminders:
        await callback.message.edit_text("🔔 У тебя пока нет напоминаний.", reply_markup=back_button())
        await callback.answer()
        return

    text = "🔔 Твои напоминания:\n\n"
    buttons = []
    for rem_id, rem_text, remind_at, repeat_daily, hour, minute in reminders:
        dt = datetime.fromisoformat(remind_at)
        repeat = "🔁 каждый день" if repeat_daily else "🔂 один раз"
        text += f"{rem_id}. {rem_text}\n   └ {dt.strftime('%d.%m %H:%M')} ({repeat})\n\n"
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить {rem_id}", callback_data=f"delrem_{rem_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("delrem_"))
async def del_reminder(callback: CallbackQuery):
    rem_id = int(callback.data.replace("delrem_", ""))
    await delete_reminder(rem_id)
    await callback.message.edit_text("✅ Напоминание удалено.", reply_markup=back_button())
    await callback.answer()

# ========== УДАЛЕНИЕ ЗАМЕТКИ ==========
@router.callback_query(F.data == "delete_note")
async def delete_note_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DeleteStates.waiting_id)
    await callback.message.edit_text("🗑 Напиши номер заметки, которую удалить:", reply_markup=back_button())
    await callback.answer()

@router.message(DeleteStates.waiting_id)
async def delete_note_id(message: Message, state: FSMContext):
    try:
        note_id = int(message.text)
        await delete_note(note_id, message.from_user.id)
        await state.clear()
        await message.answer("✅ Заметка удалена.", reply_markup=back_button())
    except ValueError:
        await message.answer("❌ Напиши номер заметки (число).")

# ========== ЭКСПОРТ ==========
@router.callback_query(F.data == "export_notes")
async def export_notes(callback: CallbackQuery):
    notes = await get_notes(callback.from_user.id)
    if not notes:
        await callback.message.edit_text("📋 У тебя нет заметок для экспорта.", reply_markup=back_button())
        await callback.answer()
        return

    text = "📋 Мои заметки (экспорт)\n"
    text += f"Дата: {now_msk().strftime('%d.%m.%Y %H:%M')} (МСК)\n\n"
    for note_id, category, title, description, done in notes:
        status = "✅" if done else "⬜"
        text += f"{status} [{category}] {title}\n"
        if description:
            text += f"   {description}\n"
        text += "\n"

    with open("export.txt", "w", encoding="utf-8") as f:
        f.write(text)

    await callback.message.answer_document(
        document=open("export.txt", "rb"),
        filename="notes_export.txt"
    )
    await callback.message.edit_text("📤 Экспорт готов!", reply_markup=back_button())
    await callback.answer()

# ========== ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ ==========
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Используй меню:", reply_markup=main_menu())

# ========== ПРОВЕРКА НАПОМИНАНИЙ ==========
async def check_reminders(bot: Bot):
    while True:
        try:
            due = await get_due_reminders()
            for reminder_id, user_id, text, repeat_daily, hour, minute in due:
                try:
                    await bot.send_message(user_id, f"⏰ Напоминание:\n\n{text}")
                    if repeat_daily:
                        now = now_msk()
                        next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if next_time <= now:
                            next_time += timedelta(days=1)
                        await update_reminder_time(reminder_id, next_time)
                    else:
                        await delete_reminder(reminder_id)
                except Exception as e:
                    logging.error(f"Reminder error: {e}")
        except Exception as e:
            logging.error(f"Check reminders error: {e}")
        await asyncio.sleep(30)

# ========== ЗАПУСК ==========
async def main():
    await init_db()

    bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Запускаем Flask в отдельном потоке
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    # Запускаем проверку напоминаний
    asyncio.create_task(check_reminders(bot))

    logging.info("Bot started... (MSK timezone)")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == '__main__':
    asyncio.run(main())
