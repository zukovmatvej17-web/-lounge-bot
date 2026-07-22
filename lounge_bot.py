import asyncio
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================= НАСТРОЙКИ =======================
# Значения берутся из переменных окружения (.env локально / Variables на Railway).
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MANAGER_GROUP_ID = int(os.getenv("MANAGER_GROUP_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
PAYMENT_PHONE = os.getenv("PAYMENT_PHONE", "+7 900 000-00-00")
PRICE_PER_PERSON = int(os.getenv("PRICE_PER_PERSON", "2500"))
DB_PATH = os.getenv("DB_PATH", "orders.db")
# =========================================================

logging.basicConfig(level=logging.INFO)
router = Router()

STATUS_RU = {
    "new": "🆕 новая",
    "in_work": "🙋 в работе",
    "paid": "✅ оплачено",
    "rejected": "❌ отклонено",
}


class OrderForm(StatesGroup):
    airport = State()
    flight_date = State()
    persons = State()
    passengers = State()
    phone = State()
    comment = State()
    confirm = State()


# ----------------------- БАЗА -----------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                airport TEXT,
                price INTEGER,
                flight_date TEXT,
                persons TEXT,
                passengers TEXT,
                phone TEXT,
                comment TEXT,
                total INTEGER,
                status TEXT DEFAULT 'new',
                manager TEXT,
                group_message_id INTEGER,
                created_at TEXT
            )
        """)
        await db.commit()


async def create_order(data, user) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO orders
               (user_id, username, airport, price, flight_date,
                persons, passengers, phone, comment, total, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user.id, user.username or "", data["airport"], PRICE_PER_PERSON,
                data["flight_date"], data["persons"], data["passengers"],
                data.get("phone", ""), data.get("comment", ""), data["total"],
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def set_group_message(order_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET group_message_id=? WHERE id=?", (message_id, order_id))
        await db.commit()


async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        return await cur.fetchone()


async def get_order_by_group_msg(message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE group_message_id=?", (message_id,))
        return await cur.fetchone()


async def get_last_order_by_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        return await cur.fetchone()


async def list_orders(only_unpaid=True):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if only_unpaid:
            q = "SELECT * FROM orders WHERE status IN ('new','in_work') ORDER BY id DESC"
        else:
            q = "SELECT * FROM orders ORDER BY id DESC LIMIT 50"
        cur = await db.execute(q)
        return await cur.fetchall()


async def update_status(order_id: int, status: str, manager: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if manager is not None:
            await db.execute(
                "UPDATE orders SET status=?, manager=? WHERE id=?", (status, manager, order_id)
            )
        else:
            await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        await db.commit()


# ----------------------- ХЕЛПЕРЫ -----------------------
def build_summary(data) -> str:
    return (
        f"✈️ Аэропорт: {data['airport']}\n"
        f"📅 Дата вылета: {data['flight_date']}\n"
        f"👥 Человек: {data['persons']}\n"
        f"👤 Пассажиры:\n{data['passengers']}\n"
        f"📞 Телефон: {data.get('phone', '-')}\n"
        f"📝 Комментарий: {data.get('comment') or '-'}\n"
        f"💰 Итого: <b>{data['total']}₽</b>"
    )


def manager_keyboard(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🙋 Взять в работу", callback_data=f"take:{order_id}")
    b.button(text="💳 Отправить сумму", callback_data=f"pay:{order_id}")
    b.button(text="✔️ Оплата получена", callback_data=f"paid:{order_id}")
    b.button(text="❌ Отклонить", callback_data=f"reject:{order_id}")
    b.adjust(1, 1, 2)
    return b.as_markup()


def fmt_client(o) -> str:
    return f"@{o['username']}" if o["username"] else f"id {o['user_id']}"


def is_admin(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else None
    return message.chat.id == MANAGER_GROUP_ID or uid in ADMIN_IDS


async def send_lines(message: Message, lines):
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > 3800:
            await message.answer(buf)
            buf = ""
        buf += ln + "\n"
    if buf:
        await message.answer(buf)


# ----------------------- ФОРМА КЛИЕНТА -----------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! 👋\nЭто бот для оформления прохода в бизнес-зал аэропорта.\n\n"
        "Нажмите кнопку ниже, чтобы оставить заявку.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заявка отменена. Можно начать заново кнопкой ниже 👇",
                         reply_markup=main_menu_kb())



async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                COUNT(*) AS cnt_all,
                SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS cnt_paid,
                SUM(CASE WHEN status IN ('new','in_work') THEN 1 ELSE 0 END) AS cnt_open,
                SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS cnt_rej,
                COALESCE(SUM(CASE WHEN status='paid' THEN total ELSE 0 END),0) AS sum_paid,
                COALESCE(SUM(CASE WHEN status IN ('new','in_work') THEN total ELSE 0 END),0) AS sum_open,
                COALESCE(SUM(CASE WHEN status='paid' THEN CAST(persons AS INTEGER) ELSE 0 END),0) AS persons_paid,
                COUNT(DISTINCT user_id) AS users
            FROM orders
        """)
        overall = await cur.fetchone()
        cur = await db.execute("""
            SELECT
                COUNT(*) AS cnt,
                COALESCE(SUM(CASE WHEN status='paid' THEN total ELSE 0 END),0) AS sum_paid
            FROM orders
            WHERE date(created_at) >= date('now','-6 days')
        """)
        week = await cur.fetchone()
        cur = await db.execute("""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(CASE WHEN status='paid' THEN total ELSE 0 END),0) AS sum_paid
            FROM orders
            WHERE date(created_at) = date('now')
        """)
        today = await cur.fetchone()
        return overall, week, today


# --- команды для тебя/менеджеров ---
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    overall, week, today = await get_stats()
    conv = 0
    if overall["cnt_all"]:
        conv = round(100 * (overall["cnt_paid"] or 0) / overall["cnt_all"])
    avg = 0
    if overall["cnt_paid"]:
        avg = round(overall["sum_paid"] / overall["cnt_paid"])
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"<b>Сегодня:</b> заявок {today['cnt']}, оплачено на {today['sum_paid']}₽\n"
        f"<b>За 7 дней:</b> заявок {week['cnt']}, оплачено на {week['sum_paid']}₽\n\n"
        f"<b>За всё время:</b>\n"
        f"• Заявок: {overall['cnt_all']} (клиентов: {overall['users']})\n"
        f"• Оплачено: {overall['cnt_paid'] or 0} на <b>{overall['sum_paid']}₽</b>\n"
        f"• Человек обслужено: {overall['persons_paid']}\n"
        f"• В ожидании: {overall['cnt_open'] or 0} на {overall['sum_open']}₽\n"
        f"• Отклонено: {overall['cnt_rej'] or 0}\n"
        f"• Конверсия в оплату: {conv}%\n"
        f"• Средний чек: {avg}₽"
    )
    await message.answer(text)


@router.message(Command("unpaid"))
async def cmd_unpaid(message: Message):
    if not is_admin(message):
        return
    rows = await list_orders(only_unpaid=True)
    if not rows:
        await message.answer("Нет неоплаченных заявок 🎉")
        return
    lines = ["💰 <b>К оплате (не оплачено)</b>\n"]
    total = 0
    for o in rows:
        total += o["total"] or 0
        lines.append(
            f"#{o['id']} {fmt_client(o)} — {o['airport']} ×{o['persons']} — "
            f"<b>{o['total']}₽</b> — 📞{o['phone']} — {STATUS_RU.get(o['status'], o['status'])}"
        )
    lines.append("—")
    lines.append(f"Итого к получению: <b>{total}₽</b>")
    await send_lines(message, lines)


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    if not is_admin(message):
        return
    rows = await list_orders(only_unpaid=False)
    if not rows:
        await message.answer("Заявок пока нет.")
        return
    lines = ["📋 <b>Последние заявки</b>\n"]
    for o in rows:
        lines.append(
            f"#{o['id']} {fmt_client(o)} — {o['airport']} ×{o['persons']} — "
            f"{o['total']}₽ — {STATUS_RU.get(o['status'], o['status'])}"
        )
    await send_lines(message, lines)


STEP_ORDER = ["airport", "flight_date", "persons", "passengers", "phone", "comment"]

QUESTIONS = {
    "airport": "✈️ В каком аэропорту нужен проход? (город / название / терминал)",
    "flight_date": "📅 Укажите дату вылета (например 25.07.2026) и по возможности время рейса.",
    "persons": "👥 Сколько человек? Укажите число.",
    "passengers": "✍️ Напишите ФИО всех пассажиров, каждого с новой строки (как в загранпаспорте).",
    "phone": "📞 Оставьте номер телефона для связи (или отправьте «-», если не нужно).",
    "comment": "📝 Комментарий к заявке (пожелания, пометки). Если не нужно — отправьте «-».",
}


def step_kb(step: str) -> InlineKeyboardMarkup | None:
    """Кнопка Назад для всех шагов, кроме первого."""
    idx = STEP_ORDER.index(step)
    if idx == 0:
        return None
    prev = STEP_ORDER[idx - 1]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"back:{prev}")]]
    )


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛎 Оформить проход в бизнес-зал")]],
        resize_keyboard=True,
    )


async def ask_step(target: Message, state: FSMContext, step: str):
    await state.set_state(getattr(OrderForm, step))
    await target.answer(QUESTIONS[step], reply_markup=step_kb(step))


@router.message(F.text == "🛎 Оформить проход в бизнес-зал")
@router.message(Command("order"))
async def start_order(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Начинаем оформление 👇", reply_markup=ReplyKeyboardRemove())
    await ask_step(message, state, "airport")


@router.callback_query(F.data.startswith("back:"))
async def go_back(cb: CallbackQuery, state: FSMContext):
    step = cb.data.split(":")[1]
    await ask_step(cb.message, state, step)
    await cb.answer()


@router.message(OrderForm.airport)
async def form_airport(message: Message, state: FSMContext):
    await state.update_data(airport=message.text.strip())
    await ask_step(message, state, "flight_date")


@router.message(OrderForm.flight_date)
async def form_date(message: Message, state: FSMContext):
    await state.update_data(flight_date=message.text.strip())
    await ask_step(message, state, "persons")


@router.message(OrderForm.persons)
async def form_persons(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("Введите количество человек числом (например 2).", reply_markup=step_kb("persons"))
        return
    await state.update_data(persons=text)
    await ask_step(message, state, "passengers")


@router.message(OrderForm.passengers)
async def form_passengers(message: Message, state: FSMContext):
    await state.update_data(passengers=message.text.strip())
    await ask_step(message, state, "phone")


@router.message(OrderForm.phone)
async def form_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await ask_step(message, state, "comment")


@router.message(OrderForm.comment)
async def form_comment(message: Message, state: FSMContext):
    comment = message.text.strip()
    await state.update_data(comment="" if comment == "-" else comment)
    data = await state.get_data()
    total = PRICE_PER_PERSON * int(data["persons"])
    await state.update_data(total=total)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="submit")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back:comment")],
            [InlineKeyboardButton(text="🔄 Заполнить заново", callback_data="restart_form")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_form")],
        ]
    )
    await state.set_state(OrderForm.confirm)
    await message.answer(f"Проверьте заявку:\n\n{build_summary(data)}", reply_markup=kb)


@router.callback_query(F.data == "restart_form")
async def restart_form(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Начнём заново 👇")
    await ask_step(cb.message, state, "airport")
    await cb.answer()


@router.callback_query(F.data == "cancel_form")
async def cancel_form(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Заявка отменена.")
    await cb.message.answer("Можно оформить новую заявку кнопкой ниже 👇", reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "submit", OrderForm.confirm)
async def submit_order(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = await create_order(data, cb.from_user)
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет username"
    manager_text = (
        f"🆕 <b>Заявка #{order_id}</b>\n\n"
        f"{build_summary(data)}\n\n"
        f"👤 Клиент: {uname} (id {cb.from_user.id})\n"
        f"🕐 {datetime.now().strftime('%d.%m %H:%M')}\n\n"
        f"💬 Ответить клиенту — <b>ответьте (reply)</b> на это сообщение."
    )
    sent = await bot.send_message(MANAGER_GROUP_ID, manager_text, reply_markup=manager_keyboard(order_id))
    await set_group_message(order_id, sent.message_id)
    await cb.message.edit_text(
        f"✅ Заявка #{order_id} отправлена!\nСумма к оплате: <b>{data['total']}₽</b>.\n\n"
        f"Менеджер свяжется с вами здесь, в этом чате. Ожидайте, пожалуйста."
    )
    await cb.message.answer("Если нужен ещё один проход — кнопка ниже 👇", reply_markup=main_menu_kb())
    await state.clear()
    await cb.answer()


# ----------------------- ДЕЙСТВИЯ МЕНЕДЖЕРА -----------------------
@router.callback_query(F.data.startswith("take:"))
async def cb_take(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[1])
    await update_status(order_id, "in_work", manager=cb.from_user.full_name)
    order = await get_order(order_id)
    await cb.message.edit_text(
        cb.message.html_text + f"\n\n🙋 <b>В работе:</b> {cb.from_user.full_name}",
        reply_markup=cb.message.reply_markup,
    )
    await bot.send_message(order["user_id"], "🙋 Ваша заявка принята в работу. Менеджер скоро свяжется с вами.")
    await cb.answer("Взято в работу")


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[1])
    order = await get_order(order_id)
    await bot.send_message(
        order["user_id"],
        f"💳 <b>К оплате: {order['total']}₽</b>\n"
        f"Перевод по номеру телефона: <code>{PAYMENT_PHONE}</code>\n\n"
        f"После оплаты отправьте, пожалуйста, чек сюда.",
    )
    await cb.answer("Сумма отправлена клиенту")


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[1])
    await update_status(order_id, "paid")
    order = await get_order(order_id)
    await bot.send_message(order["user_id"], "✅ Оплата получена! Ваш проход оформляется. Спасибо 🙏")
    await cb.answer("Отмечено как оплачено")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[1])
    await update_status(order_id, "rejected")
    order = await get_order(order_id)
    await bot.send_message(
        order["user_id"],
        "❌ К сожалению, по вашей заявке пришёл отказ. Напишите нам для уточнения деталей.",
    )
    await cb.answer("Заявка отклонена")


# Ответ менеджера reply-ем на заявку → копируется клиенту
@router.message(F.chat.id == MANAGER_GROUP_ID, F.reply_to_message)
async def manager_reply(message: Message, bot: Bot):
    order = await get_order_by_group_msg(message.reply_to_message.message_id)
    if not order:
        return
    try:
        await bot.copy_message(
            chat_id=order["user_id"],
            from_chat_id=MANAGER_GROUP_ID,
            message_id=message.message_id,
        )
        await message.reply("✅ Отправлено клиенту")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось отправить: {e}")


# Любое сообщение клиента вне формы (например чек) → в группу к его заявке
@router.message(F.chat.type == "private", StateFilter(None))
async def client_message(message: Message, bot: Bot):
    order = await get_last_order_by_user(message.from_user.id)
    if not order or not order["group_message_id"]:
        await message.answer("Чтобы оставить заявку, нажмите кнопку ниже или наберите /order 👇", reply_markup=main_menu_kb())
        return
    await bot.send_message(
        MANAGER_GROUP_ID,
        f"💬 Сообщение по заявке #{order['id']} от клиента:",
        reply_to_message_id=order["group_message_id"],
    )
    await bot.copy_message(MANAGER_GROUP_ID, message.chat.id, message.message_id)
    await message.answer("✅ Сообщение передано менеджеру.")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполни .env (локально) или Variables (Railway).")
    if not MANAGER_GROUP_ID:
        raise SystemExit("MANAGER_GROUP_ID не задан.")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="order", description="🛎 Оформить проход"),
        BotCommand(command="cancel", description="❌ Отменить заявку"),
        BotCommand(command="start", description="🔄 Перезапустить бота"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
