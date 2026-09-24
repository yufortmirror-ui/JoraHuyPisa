import asyncio
import logging
import json
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "123123"
CHECK_INTERVAL_SECONDS = 5 * 60 * 60  # 5 часов
BASE_URL = "https://xn--c1aexnm.xn--p1ai"
GROUPS_PER_PAGE = 6  # Количество групп на одной странице кнопок

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилища
user_states: Dict[int, str] = {}
user_groups: Dict[int, str] = {}
user_pages: Dict[int, int] = {}  # {chat_id: current_page}

# Полный список всех групп университета
ALL_GROUPS = [
    # 1 курс (26-27)
    "НГД-26-1бКФ", "НГТ-26-1сКФ", "НГТ-26-2сКФ", "НГТ-26-3сКФ", "НДИИ-26-1бКФ",
    # 2 курс (25-26)
    "ГМНГ-25-1сКФ", "НГД-25-1бКФ", "НГТ-25-1сКФ", "НГТ-25-1с3", "НГТ-25-5с", "НГТ-25-6с", "НДИИ-25-1бКФ",
    # 3 курс (24-25)
    "НГД-24-4б", "НГТ-24-3с", "НГТ-24-4с", "НГТ-24-5с",
    # 4 курс (23-24)
    "НГД-23-4б", "НГД-22-4б", "НГТ-23-4с", "НГТ-23-5с",
    # 5 курс (22-23)
    "НГТ-22-3с", "НГТ-22-4с", "ГМНГ-21-2с",
]

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

SCHEDULE_BELLS = """<b>Расписание звонков</b>

1 пара — с 8:00 до 9:30

2 пара — с 9:40 до 11:10

Обеденный перерыв — 20 минут

3 пара — с 11:30 до 13:00

Обеденный перерыв — 20 минут

4 пара — с 13:20 до 14:50

5 пара — с 15:00 до 16:30

6 пара — с 16:40 до 18:10

7 пара — с 18:20 до 19:50

8 пара — с 20:00 до 21:30"""


def get_target_date(day_type: str) -> datetime:
    today = datetime.now()
    if day_type == "today":
        return today
    elif day_type == "tomorrow":
        return today + timedelta(days=1)
    elif day_type in WEEKDAYS_SHORT:
        delta = WEEKDAYS_SHORT.index(day_type) - today.weekday()
        return today + timedelta(days=delta)
    return today


def get_day_key(date: datetime) -> str:
    return f"{WEEKDAYS[date.weekday()]}{date.day:02d}.{date.month:02d}"


def get_date_str(date: datetime) -> str:
    months = {i: m for i, m in enumerate([
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ], start=1)}
    return f"{date.day} {months[date.month]} {date.year}"


async def get_schedule_html(group: str, year: str = "26-27") -> str:
    url = f"{BASE_URL}/schedule_hub/schedules?group={group}&year={year}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                return await resp.text() if resp.status == 200 else ""
        except Exception as e:
            logger.error(f"Request error: {e}")
            return ""


def parse_schedule_from_html(html: str, target_date: Optional[datetime] = None) -> str:
    """Парсит HTML и извлекает расписание на указанную дату"""
    if not html:
        return "❌ Не удалось получить данные."
    if target_date is None:
        target_date = datetime.now()

    soup = BeautifulSoup(html, 'lxml')
    table = soup.find('table', id='scheduleTable')
    if not table:
        return "⚠️ Структура сайта изменилась, парсер требует обновления."

    date_str = get_date_str(target_date)
    weekday = WEEKDAYS[target_date.weekday()]
    today_key = get_day_key(target_date)
    result = f"📅 {date_str}, {weekday}\n\n"

    # Находим все строки для целевого дня
    rows = table.find_all('tr', attrs={'data-day': today_key})
    if not rows:
        return f"📅 {date_str}, {weekday}\n\n✅ Пар нет."

    pair_num = 0

    for row in rows:
        # Пропускаем служебные строки
        if 'empty-row' in row.get('class', []) or row.find('div', class_='window-slot'):
            continue
        time_cell = row.find('td', class_='time-col')
        subject_cell = row.find('td', class_='subject-cell')
        if not time_cell or not subject_cell:
            continue
        time_text = time_cell.get_text(strip=True)

        single_class = subject_cell.find('div', class_='single-class')
        subgroup_container = subject_cell.find('div', class_='subgroup-container')

        # Функция для обработки текста одной пары (общая для одиночных и подгрупп)
        def format_pair_text(text: str, suffix: str = "") -> str:
            nonlocal pair_num
            room_match = re.search(r'ауд\.\s*(\d+)', text)
            # Очищаем текст от аудитории для получения предмета и преподавателя
            clean_text = text[:room_match.start()].strip() if room_match else text
            room = room_match.group(1) if room_match else ""

            # Разделяем предмет и преподавателя по типу занятия
            type_match = re.search(r'\((пр|лек|лаб)\)', clean_text)
            if type_match:
                subject = clean_text[:type_match.start()].strip()
                teacher_raw = clean_text[type_match.end():].strip()
            else:
                subject = clean_text
                teacher_raw = ""

            # Убираем сокращения должностей
            teacher = re.sub(r'\b(доц\.|асс\.|проф\.|зав\.каф\.|ст\.преп\.)\s*', '', teacher_raw).strip()

            pair_num += 1
            output = f"{pair_num}{suffix} пара | {time_text}\n"
            output += f"📚 {subject}\n"
            if room:
                output += f"🚪 {room}\n"
            if teacher:
                output += f"👤 {teacher}\n"
            return output + "\n"

        if single_class and (span := single_class.find('span')):
            result += format_pair_text(span.get_text(strip=True))
        elif subgroup_container:
            subgroup_columns = subgroup_container.find_all('div', class_='subgroup-column')
            for col in subgroup_columns:
                span = col.find('span')
                # Пропускаем пустые подгруппы или окна отдыха
                if span and 'fa-mug-hot' not in str(col):
                    result += format_pair_text(span.get_text(strip=True), " (подгр.)")

    if pair_num == 0:
        return f" {date_str}, {weekday}\n\n✅ Пар нет."

    return result[:4000]


# --- КЛАВИАТУРЫ ---

def get_group_selection_keyboard(page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    total_pages = max(1, (len(ALL_GROUPS) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = min(max(0, page), total_pages - 1)

    start_idx = page * GROUPS_PER_PAGE
    end_idx = min(start_idx + GROUPS_PER_PAGE, len(ALL_GROUPS))

    for i in range(start_idx, end_idx, 2):
        btns = [InlineKeyboardButton(text=g, callback_data=f"group_{g}") for g in ALL_GROUPS[i:i+2]]
        builder.row(*btns)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page_{page+1}"))
    if nav_row:
        builder.row(*nav_row)

    builder.row(InlineKeyboardButton(text="🔄 Сменить группу", callback_data="change_group"))
    return builder.as_markup()


def get_days_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 Сегодня", callback_data="day_today"),
        InlineKeyboardButton(text="📅 Завтра", callback_data="day_tomorrow")
    )
    builder.row(
        InlineKeyboardButton(text="Пн", callback_data="day_пн"),
        InlineKeyboardButton(text="Вт", callback_data="day_вт"),
        InlineKeyboardButton(text="Ср", callback_data="day_ср")
    )
    builder.row(
        InlineKeyboardButton(text="Чт", callback_data="day_чт"),
        InlineKeyboardButton(text="Пт", callback_data="day_пт"),
        InlineKeyboardButton(text="Сб", callback_data="day_сб")
    )
    builder.row(InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="bells"))
    builder.row(InlineKeyboardButton(text="🔄 Сменить группу", callback_data="change_group"))  # ← новая кнопка
    builder.row(InlineKeyboardButton(text="💰 Поддержать автора", callback_data="support"))
    return builder.as_markup()


def generate_hash(content: str) -> str:
    return hashlib.md5(content.encode('utf-8')).hexdigest()


async def send_schedule_for_day(chat_id: int, group: str, day_type: str):
    html = await get_schedule_html(group)
    schedule_text = parse_schedule_from_html(html, get_target_date(day_type))

    labels = {
        "today": "Сегодня", "tomorrow": "Завтра",
        "пн": "Пн", "вт": "Вт", "ср": "Ср",
        "чт": "Чт", "пт": "Пт", "сб": "Сб"
    }
    day_label = labels.get(day_type, day_type)

    await bot.send_message(
        chat_id,
        f"<b>{group}</b> ({day_label}):\n\n{schedule_text}",
        reply_markup=get_days_keyboard(),
        parse_mode="HTML"
    )


# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_pages[message.from_user.id] = 0
    await message.answer(
        "Привет! Выберите свою группу:",
        reply_markup=get_group_selection_keyboard(0)
    )


@dp.callback_query(F.data.startswith("page_"))
async def paginate_callback(callback: types.CallbackQuery):
    page = int(callback.data.replace("page_", ""))
    user_pages[callback.from_user.id] = page
    await callback.message.edit_reply_markup(reply_markup=get_group_selection_keyboard(page))
    await callback.answer()


@dp.callback_query(F.data.startswith("group_"))
async def select_group_callback(callback: types.CallbackQuery):
    group = callback.data.replace("group_", "")
    user_groups[callback.from_user.id] = group

    await callback.message.edit_text(f"✅ Выбрана группа: <b>{group}</b>", parse_mode="HTML")
    await send_schedule_for_day(callback.from_user.id, group, "today")

    html = await get_schedule_html(group)
    if html:
        user_states[callback.from_user.id] = generate_hash(parse_schedule_from_html(html))
    await callback.answer()


@dp.callback_query(F.data.startswith("day_"))
async def select_day_callback(callback: types.CallbackQuery):
    group = user_groups.get(callback.from_user.id)
    if not group:
        await callback.answer("Сначала выберите группу через /start", show_alert=True)
        return
    await send_schedule_for_day(callback.from_user.id, group, callback.data.replace("day_", ""))
    await callback.answer()


@dp.callback_query(F.data == "bells")
async def bells_callback(callback: types.CallbackQuery):
    await callback.message.answer(SCHEDULE_BELLS, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "💰 <b>Поддержать автора</b>\n\n"
        "Для поддержания бота нужно оплачивать хостинг, будет приятно от любой помощи.\n\n"
        "💳 <b>Номер карты:</b>\n<code>2200 7008 4077 5158</code>\n\n"
        "Спасибо за вашу поддержку! 🙏",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "change_group")
async def change_group_callback(callback: types.CallbackQuery):
    user_groups.pop(callback.from_user.id, None)
    user_pages[callback.from_user.id] = 0
    await callback.message.edit_text(
        "Выберите новую группу:",
        reply_markup=get_group_selection_keyboard(0)
    )
    await callback.answer()


@dp.callback_query(F.data == "view_changes")
async def view_changes_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    group = user_groups.get(chat_id)
    if not group:
        await callback.answer("Группа не выбрана.", show_alert=True)
        return
    await send_schedule_for_day(chat_id, group, "today")
    html = await get_schedule_html(group)
    if html:
        user_states[chat_id] = generate_hash(parse_schedule_from_html(html))
    await callback.answer()


# --- ФОНОВАЯ ПРОВЕРКА ---

async def check_updates_background():
    logger.info("Background checker started.")
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        processed = {}
        for chat_id, old_hash in list(user_states.items()):
            group = user_groups.get(chat_id)
            if not group:
                continue
            if group not in processed:
                html = await get_schedule_html(group)
                if not html:
                    continue
                content = parse_schedule_from_html(html)
                processed[group] = generate_hash(content)
            new_hash = processed[group]
            if new_hash != old_hash:
                kb = InlineKeyboardBuilder().button(
                    text="👀 Посмотреть", callback_data="view_changes"
                ).as_markup()
                try:
                    await bot.send_message(
                        chat_id,
                        f"⚠️ Расписание для <b>{group}</b> обновилось!",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                    user_states[chat_id] = new_hash
                except Exception as e:
                    logger.error(f"Notify failed for {chat_id}: {e}")


async def main():
    asyncio.create_task(check_updates_background())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")