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
                persons, passengers, phone, total, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                user.id, user.username or "", data["airport"], PRICE_PER_PERSON,
                data["flight_date"], data["persons"], data["passengers"],
                data.get("phone", ""), data["total"],
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
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛎 Оформить проход в бизнес-зал")]],
        resize_keyboard=True,
    )
    await message.answer(
        "Здравствуйте! 👋\nЭто бот для оформления прохода в бизнес-зал аэропорта.\n\n"
        "Нажмите кнопку ниже, чтобы оставить заявку.",
        reply_markup=kb,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заявка отменена. Наберите /start, чтобы начать заново.",
                         reply_markup=ReplyKeyboardRemove())


# --- команды для тебя/менеджеров ---
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


@router.message(F.text == "🛎 Оформить проход в бизнес-зал")
@router.message(Command("order"))
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.airport)
    await message.answer(
        "В каком аэропорту нужен проход? (город / название / терминал)",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(OrderForm.airport)
async def form_airport(message: Message, state: FSMContext):
    await state.update_data(airport=message.text.strip())
    await state.set_state(OrderForm.flight_date)
    await message.answer("📅 Укажите дату вылета (например 25.07.2026) и по возможности время рейса.")


@router.message(OrderForm.flight_date)
async def form_date(message: Message, state: FSMContext):
    await state.update_data(flight_date=message.text.strip())
    await state.set_state(OrderForm.persons)
    await message.answer("👥 Сколько человек? Укажите число.")


@router.message(OrderForm.persons)
async def form_persons(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("Введите количество человек числом (например 2).")
        return
    await state.update_data(persons=text)
    await state.set_state(OrderForm.passengers)
    await message.answer("✍️ Напишите ФИО всех пассажиров, каждого с новой строки (как в загранпаспорте).")


@router.message(OrderForm.passengers)
async def form_passengers(message: Message, state: FSMContext):
    await state.update_data(passengers=message.text.strip())
    await state.set_state(OrderForm.phone)
    await message.answer("📞 Оставьте номер телефона для связи (или отправьте «-», если не нужно).")


@router.message(OrderForm.phone)
async def form_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    data = await state.get_data()
    total = PRICE_PER_PERSON * int(data["persons"])
    await state.update_data(total=total)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="submit")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_form")],
        ]
    )
    await state.set_state(OrderForm.confirm)
    await message.answer(f"Проверьте заявку:\n\n{build_summary(data)}", reply_markup=kb)


@router.callback_query(F.data == "cancel_form")
async def cancel_form(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Заявка отменена. Наберите /start, чтобы начать заново.")
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
        await message.answer("Чтобы оставить заявку, наберите /start")
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
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
