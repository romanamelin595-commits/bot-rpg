import asyncio
import io
import math
import random
import time
import aiosqlite

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery
)
from PIL import Image, ImageDraw, ImageFont
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = "8945500731:AAHi6-eF7xkVpE1eU80X-Z8K0ltVB8Ksq40"
DB_NAME = "levels.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# =====================================================================
# 1. ТУРБО-БАЗА ДАННЫХ (WAL, RAM-КЭШ 64 МБ, ПОСТОЯННОЕ СОЕДИНЕНИЕ)
# =====================================================================

class TurboDatabase:
    def __init__(self, db_path: str = DB_NAME):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")
        await self._conn.execute("PRAGMA cache_size = -64000;")
        await self._conn.execute("PRAGMA temp_store = MEMORY;")

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                chat_id INTEGER,
                username TEXT,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                item_name TEXT,
                tier TEXT,
                base_xp INTEGER
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS market (
                lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                seller_id INTEGER,
                seller_name TEXT,
                item_name TEXT,
                tier TEXT,
                base_xp INTEGER,
                price_xp INTEGER
            )
        """)
        await self._conn.commit()
        print("⚡ [DB] SQLite ускорен: WAL-режим и 64 МБ RAM-кэша активны!")

    async def get_user(self, user_id: int, chat_id: int):
        async with self._conn.execute(
            "SELECT xp, level, username FROM users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cur:
            return await cur.fetchone()

    async def add_xp(self, user_id: int, chat_id: int, username: str, amount: int):
        user = await self.get_user(user_id, chat_id)
        if user:
            xp = user["xp"] + amount
            lvl = user["level"]
            leveled_up = False
            while xp >= lvl * 100:
                xp -= lvl * 100
                lvl += 1
                leveled_up = True

            await self._conn.execute(
                "UPDATE users SET xp = ?, level = ?, username = ? WHERE user_id = ? AND chat_id = ?",
                (xp, lvl, username, user_id, chat_id)
            )
            await self._conn.commit()
            return leveled_up, lvl, xp
        else:
            await self._conn.execute(
                "INSERT INTO users (user_id, chat_id, username, xp, level) VALUES (?, ?, ?, ?, 1)",
                (user_id, chat_id, username, amount)
            )
            await self._conn.commit()
            return False, 1, amount

    async def get_inventory_count(self, user_id: int, chat_id: int) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def add_item(self, user_id: int, chat_id: int, item_name: str, tier: str, base_xp: int):
        await self._conn.execute(
            "INSERT INTO inventory (user_id, chat_id, item_name, tier, base_xp) VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, item_name, tier, base_xp)
        )
        await self._conn.commit()

    async def get_items(self, user_id: int, chat_id: int):
        async with self._conn.execute(
            "SELECT id, item_name, tier, base_xp FROM inventory WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cur:
            return await cur.fetchall()

    async def get_item_by_id(self, item_id: int, user_id: int, chat_id: int):
        async with self._conn.execute(
            "SELECT item_name, tier, base_xp FROM inventory WHERE id = ? AND user_id = ? AND chat_id = ?",
            (item_id, user_id, chat_id)
        ) as cur:
            return await cur.fetchone()

    async def delete_item(self, item_id: int):
        await self._conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
        await self._conn.commit()

    async def add_market_lot(self, chat_id: int, seller_id: int, seller_name: str, item_name: str, tier: str, base_xp: int, price: int):
        await self._conn.execute(
            "INSERT INTO market (chat_id, seller_id, seller_name, item_name, tier, base_xp, price_xp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, seller_id, seller_name, item_name, tier, base_xp, price)
        )
        await self._conn.commit()

    async def get_market_lots(self, chat_id: int):
        async with self._conn.execute(
            "SELECT lot_id, seller_name, item_name, tier, price_xp FROM market WHERE chat_id = ?",
            (chat_id,)
        ) as cur:
            return await cur.fetchall()

    async def get_lot(self, lot_id: int, chat_id: int):
        async with self._conn.execute(
            "SELECT seller_id, seller_name, item_name, tier, base_xp, price_xp FROM market WHERE lot_id = ? AND chat_id = ?",
            (lot_id, chat_id)
        ) as cur:
            return await cur.fetchone()

    async def delete_lot(self, lot_id: int):
        await self._conn.execute("DELETE FROM market WHERE lot_id = ?", (lot_id,))
        await self._conn.commit()

    async def transfer_xp(self, from_id: int, to_id: int, chat_id: int, amount: int):
        await self._conn.execute("UPDATE users SET xp = MAX(0, xp - ?) WHERE user_id = ? AND chat_id = ?", (amount, from_id, chat_id))
        await self._conn.execute("UPDATE users SET xp = xp + ? WHERE user_id = ? AND chat_id = ?", (amount, to_id, chat_id))
        await self._conn.commit()

    async def get_top(self, chat_id: int, limit: int = 10):
        async with self._conn.execute(
            "SELECT username, level, xp FROM users WHERE chat_id = ? ORDER BY level DESC, xp DESC LIMIT ?",
            (chat_id, limit)
        ) as cur:
            return await cur.fetchall()

db = TurboDatabase()

# =====================================================================
# 2. ОРУЖИЕ, ШАНСЫ И ГЕНЕРАЦИЯ ГРАФИКИ (PILLOW)
# =====================================================================

TIER_COLORS = {
    "Обычный": (148, 163, 184),      # Серый
    "Редкий": (59, 130, 246),        # Синий
    "Эпический": (168, 85, 247),     # Фиолетовый
    "ЛЕГЕНДАРНЫЙ": (239, 68, 68),    # Красный
    "ЭКСКЛЮЗИВ": (245, 158, 11)      # Золотой
}

# Список оружия с названиями для рулетки и полными названиями в инвентарь
CASE_ITEMS = [
    {
        "name": "P250 Песок",
        "full_name": "P250 | Песчаная буря",
        "xp": 15,
        "tier": "Обычный",
        "icon": "🔫"
    },
    {
        "name": "MP9 Угроза",
        "full_name": "MP9 | Скромная угроза",
        "xp": 25,
        "tier": "Обычный",
        "icon": "🔫"
    },
    {
        "name": "USP Закрут",
        "full_name": "USP-S | Закрученный",
        "xp": 50,
        "tier": "Редкий",
        "icon": "🔫"
    },
    {
        "name": "Deagle Пламя",
        "full_name": "Desert Eagle | Оксидное пламя",
        "xp": 90,
        "tier": "Редкий",
        "icon": "🔫"
    },
    {
        "name": "AK-47 Редлайн",
        "full_name": "AK-47 | Красная линия",
        "xp": 180,
        "tier": "Эпический",
        "icon": "🔥"
    },
    {
        "name": "AWP Нео-нуар",
        "full_name": "AWP | Нео-нуар",
        "xp": 260,
        "tier": "Эпический",
        "icon": "🎯"
    },
    {
        "name": "M4A4 Вой",
        "full_name": "M4A4 | Вой",
        "xp": 600,
        "tier": "ЛЕГЕНДАРНЫЙ",
        "icon": "🐺"
    },
    {
        "name": "Керамбит Град",
        "full_name": "★ Керамбит | Градиент",
        "xp": 1200,
        "tier": "ЛЕГЕНДАРНЫЙ",
        "icon": "🗡️"
    },
    {
        "name": "Бизон Эмбарго",
        "full_name": "ПП-19 Бизон | Эмбарго (Float: 0.0000089)",
        "xp": 3000,
        "tier": "ЭКСКЛЮЗИВ",
        "icon": "💎"
    }
]

# Шансы: Бизон = 0.1% (1 из 1000), Керамбит = 0.2%, Вой = 1.2%, и т.д.
ITEM_WEIGHTS = [300, 250, 180, 130, 80, 45, 12, 2, 1]

def get_rank_title(level: int) -> str:
    if level < 5:
        return "Новичок"
    elif level < 15:
        return "Штурмовик"
    elif level < 30:
        return "Ветеран ЧАТА"
    elif level < 50:
        return "Кибер-Фантом"
    return "Легенда Зоны"

def get_fonts():
    try:
        font_title = ImageFont.truetype("arial.ttf", 34)
        font_bold = ImageFont.truetype("arialbd.ttf", 22)
        font_regular = ImageFont.truetype("arial.ttf", 18)
        font_small = ImageFont.truetype("arial.ttf", 13)
    except IOError:
        font_title = font_bold = font_regular = font_small = ImageFont.load_default()
    return font_title, font_bold, font_regular, font_small

def generate_rank_card(username: str, level: int, current_xp: int, needed_xp: int, inv_count: int, avatar_bytes: bytes = None) -> bytes:
    w, h = 800, 270
    img = Image.new("RGBA", (w, h), (15, 20, 32, 255))
    draw = ImageDraw.Draw(img)
    font_title, font_bold, font_regular, font_small = get_fonts()

    for i in range(0, w, 40):
        draw.line([(i, 0), (i + 80, h)], fill=(22, 30, 48, 255), width=1)

    draw.rounded_rectangle([12, 12, w - 12, h - 12], radius=16, outline=(38, 50, 75), width=2)
    draw.rounded_rectangle([12, 12, 20, h - 12], radius=4, fill=(56, 189, 248))

    av_size = 144
    av_x, av_y = 48, 62
    if avatar_bytes:
        try:
            av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av_img = av_img.resize((av_size, av_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (av_size, av_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
            img.paste(av_img, (av_x, av_y), mask)
        except Exception:
            avatar_bytes = None

    if not avatar_bytes:
        draw.ellipse([av_x, av_y, av_x + av_size, av_y + av_size], fill=(24, 32, 47), outline=(56, 189, 248), width=3)
        draw.text((av_x + 50, av_y + 40), "★", fill=(56, 189, 248), font=font_title)

    draw.ellipse([av_x - 3, av_y - 3, av_x + av_size + 3, av_y + av_size + 3], outline=(56, 189, 248), width=3)
    draw.ellipse([av_x - 7, av_y - 7, av_x + av_size + 7, av_y + av_size + 7], outline=(14, 116, 144), width=1)

    text_x = 235
    title_rank = get_rank_title(level)

    draw.text((text_x, 42), username[:18], fill=(248, 250, 252), font=font_title)
    draw.text((text_x, 86), f"[{title_rank}]", fill=(148, 163, 184), font=font_regular)

    draw.rounded_rectangle([text_x, 120, text_x + 130, 154], radius=8, fill=(30, 41, 59))
    draw.text((text_x + 14, 126), f"LVL {level}", fill=(56, 189, 248), font=font_bold)

    draw.rounded_rectangle([text_x + 145, 120, text_x + 310, 154], radius=8, fill=(30, 41, 59))
    draw.text((text_x + 158, 126), f"ЛУТ: {inv_count} шт.", fill=(203, 213, 225), font=font_bold)

    xp_str = f"{current_xp} / {needed_xp} XP"
    draw.text((w - 55 - len(xp_str) * 11, 128), xp_str, fill=(148, 163, 184), font=font_regular)

    bar_x1, bar_y1 = text_x, 175
    bar_x2, bar_y2 = w - 45, 206
    draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=16, fill=(24, 32, 47), outline=(45, 55, 72))

    percent = min(max(current_xp / needed_xp, 0.0), 1.0)
    fill_x = bar_x1 + int((bar_x2 - bar_x1) * percent)

    if fill_x > bar_x1 + 16:
        draw.rounded_rectangle([bar_x1, bar_y1, fill_x, bar_y2], radius=16, fill=(34, 197, 94))
        draw.rounded_rectangle([bar_x1 + 4, bar_y1 + 3, fill_x - 4, bar_y1 + 10], radius=6, fill=(134, 239, 172, 180))

    draw.text((text_x, 216), f"Прогресс уровня: {int(percent * 100)}%", fill=(100, 116, 139), font=font_small)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()

def generate_case_roulette_gif(winning_item: dict) -> bytes:
    """Генерация быстрой и плавной GIF-анимации рулетки в стиле CS."""
    w, h = 640, 200
    slot_w = 140
    card_w, card_h = 120, 140
    card_y = 30
    center_x = w // 2

    win_index = 15
    tape = []
    for i in range(21):
        if i == win_index:
            tape.append(winning_item)
        else:
            tape.append(random.choices(CASE_ITEMS, weights=ITEM_WEIGHTS, k=1)[0])

    target_offset = win_index * slot_w + (slot_w // 2) - center_x + random.randint(-15, 15)

    _, font_bold, font_regular, font_small = get_fonts()
    frames = []
    total_frames = 26  # Оптимизировано для быстрой генерации на сервере

    for f in range(total_frames):
        t = f / (total_frames - 1)
        ease_out = 1.0 - math.pow(1.0 - t, 3.2)
        cur_offset = target_offset * ease_out

        frame = Image.new("RGB", (w, h), (15, 20, 32))
        draw = ImageDraw.Draw(frame)

        for bg_x in range(0, w, 32):
            draw.line([(bg_x, 0), (bg_x, h)], fill=(20, 27, 43), width=1)

        for idx, it in enumerate(tape):
            item_center_x = int(idx * slot_w + (slot_w // 2) - cur_offset)
            box_x1 = item_center_x - (card_w // 2)
            box_x2 = box_x1 + card_w
            box_y1 = card_y
            box_y2 = card_y + card_h

            if box_x2 < -30 or box_x1 > w + 30:
                continue

            color = TIER_COLORS.get(it["tier"], (148, 163, 184))

            draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=10, fill=(24, 32, 47), outline=color, width=2)
            draw.rounded_rectangle([box_x1 + 4, box_y2 - 6, box_x2 - 4, box_y2 - 2], radius=2, fill=color)

            draw.text((item_center_x - 12, box_y1 + 18), it.get("icon", "🔫"), font=font_bold)

            name_label = it["name"]
            if len(name_label) > 12:
                name_label = name_label[:11] + "…"
            name_bbox = draw.textbbox((0, 0), name_label, font=font_small)
            text_w = name_bbox[2] - name_bbox[0]
            draw.text((item_center_x - (text_w // 2), box_y1 + 68), name_label, fill=(241, 245, 249), font=font_small)

            tier_bbox = draw.textbbox((0, 0), it["tier"][:7], font=font_small)
            tw = tier_bbox[2] - tier_bbox[0]
            draw.text((item_center_x - (tw // 2), box_y1 + 92), it["tier"][:7], fill=color, font=font_small)

            xp_badge = f"+{it['xp']}XP"
            draw.text((item_center_x - 18, box_y1 + 112), xp_badge, fill=(34, 197, 94), font=font_small)

        # Боковое затемнение рулетки
        draw.rectangle([0, 0, 70, h], fill=(15, 20, 32))
        draw.rectangle([w - 70, 0, w, h], fill=(15, 20, 32))

        # Окно и центральный визир
        draw.rectangle([0, 0, w - 1, h - 1], outline=(38, 50, 75), width=2)
        draw.line([(center_x, 0), (center_x, h)], fill=(245, 158, 11), width=2)
        draw.polygon([(center_x - 10, 2), (center_x + 10, 2), (center_x, 18)], fill=(245, 158, 11))
        draw.polygon([(center_x - 10, h - 3), (center_x + 10, h - 3), (center_x, h - 19)], fill=(245, 158, 11))

        frames.append(frame)

    durations = [70] * (total_frames - 5) + [100, 150, 220, 320, 1600]

    out = io.BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0
    )
    return out.getvalue()

# =====================================================================
# 3. КУЛДАУНЫ, ШЕДУЛЕР И ИГРОВАЯ МЕХАНИКА
# =====================================================================

cooldowns = {}
active_duels = {}
is_double_xp = False

def get_cooldown(user_id: int, action: str, wait_seconds: int) -> int:
    now = time.time()
    last_time = cooldowns.get((user_id, action), 0)
    passed = now - last_time
    if passed < wait_seconds:
        return int(wait_seconds - passed) + 1
    cooldowns[(user_id, action)] = now
    return 0

scheduler = AsyncIOScheduler(timezone="Asia/Yekaterinburg")

async def happy_hour_start():
    global is_double_xp
    is_double_xp = True
    print("🔥 [EVENT] Счастливый час x2 XP включён!")

async def happy_hour_end():
    global is_double_xp
    is_double_xp = False
    print("💤 [EVENT] Счастливый час x2 XP завершён.")

# x2 XP каждый вечер с 19:00 до 20:00
scheduler.add_job(happy_hour_start, "cron", hour=19, minute=0)
scheduler.add_job(happy_hour_end, "cron", hour=20, minute=0)

def get_user_tag(user: types.User) -> str:
    tag = f" (@{user.username})" if user.username else ""
    return f"{user.first_name}{tag}"

# =====================================================================
# 4. ХЭНДЛЕРЫ КОМАНД
# =====================================================================

@dp.message(Command("rank", "profile"))
async def check_rank(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    name = message.from_user.first_name

    user = await db.get_user(user_id, chat_id)
    if not user:
        await message.reply("У тебя пока 0 опыта. Напиши пару сообщений в чат!")
        return

    xp = user["xp"]
    level = user["level"]
    needed = level * 100
    inv_count = await db.get_inventory_count(user_id, chat_id)

    avatar_bytes = None
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            file_info = await bot.get_file(file_id)
            buf = io.BytesIO()
            await bot.download_file(file_info.file_path, buf)
            avatar_bytes = buf.getvalue()
    except Exception:
        pass

    img_data = generate_rank_card(
        username=name,
        level=level,
        current_xp=xp,
        needed_xp=needed,
        inv_count=inv_count,
        avatar_bytes=avatar_bytes
    )

    photo_file = BufferedInputFile(img_data, filename="rank.png")
    status = " <i>(🔥 Включён бонус x2 XP!)</i>" if is_double_xp else ""
    await message.reply_photo(photo=photo_file, caption=f"Профиль игрока <b>{name}</b>{status}", parse_mode="HTML")

@dp.message(Command("case", "box"))
async def open_lootbox(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = get_user_tag(message.from_user)

    remains = get_cooldown(user_id, "case", wait_seconds=45)
    if remains > 0:
        await message.reply(f"⏳ Кейс перезаряжается! Подожди ещё <b>{remains} сек.</b>", parse_mode="HTML")
        return

    item = random.choices(CASE_ITEMS, weights=ITEM_WEIGHTS, k=1)[0]
    reward_xp = item["xp"] * 2 if is_double_xp else item["xp"]
    full_title = item.get("full_name", item["name"])

    # Сохраняем в инвентарь полное имя со скином и флотом
    await db.add_item(user_id, chat_id, full_title, item["tier"], item["xp"])
    lvl_up, new_lvl, _ = await db.add_xp(user_id, chat_id, username, reward_xp)

    if lvl_up:
        await message.answer(f"🎉 <b>{message.from_user.first_name}</b> повысил уровень до <b>{new_lvl}</b>!", parse_mode="HTML")

    gif_bytes = generate_case_roulette_gif(item)
    anim_file = BufferedInputFile(gif_bytes, filename="case_roulette.gif")

    caption_text = (
        f"🎁 <b>{message.from_user.first_name} открыл кейс!</b>\n\n"
        f"🎯 Выпало: <b>{item.get('icon', '🔫')} {full_title}</b> [{item['tier']}]\n"
        f"✨ Получено: <b>+{reward_xp} XP</b>\n"
        f"🎒 Предмет добавлен в <code>/inv</code>"
    )

    if item["tier"] == "ЭКСКЛЮЗИВ":
        caption_text = (
            "💎👑 <b>МИРОВОЙ ТОП-1 ВЫПАЛ! (ШАНС 0.1%)</b> 👑💎\n"
            "😱 <b>ЭТО ЖЕ ТОТ САМЫЙ ПАТТЕРН И МИНИМАЛЬНЫЙ ФЛОТ!</b>\n\n"
            + caption_text
        )
    elif item["tier"] == "ЛЕГЕНДАРНЫЙ":
        caption_text = "🌟🔥 <b>ЛЕГЕНДАРНЫЙ ДРОП!</b> 🔥🌟\n\n" + caption_text

    await message.reply_animation(animation=anim_file, caption=caption_text, parse_mode="HTML")

@dp.message(Command("inv", "inventory"))
async def show_inventory(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    items = await db.get_items(user_id, chat_id)

    if not items:
        await message.reply("🎒 Твой инвентарь пуст! Открой кейс через /case.")
        return

    text = f"🎒 <b>Инвентарь {message.from_user.first_name}:</b>\n\n"
    for it in items:
        text += f"• <code>#{it['id']}</code> <b>{it['item_name']}</b> [{it['tier']}] ({it['base_xp']} XP)\n"

    text += "\n💡 <i>Продажа на рынок: <code>/sell &lt;номер&gt; &lt;цена_в_XP&gt;</code></i>"
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("slots"))
async def play_slots(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = get_user_tag(message.from_user)

    remains = get_cooldown(user_id, "slots", wait_seconds=15)
    if remains > 0:
        await message.reply(f"🎰 Автомат остывает! Жди <b>{remains} сек.</b>", parse_mode="HTML")
        return

    msg = await message.answer_dice(emoji="🎰")
    dice_val = msg.dice.value
    await asyncio.sleep(2.5)

    reward = 100 if dice_val == 64 else (35 if dice_val in (1, 22, 43) else 0)
    if is_double_xp and reward > 0:
        reward *= 2

    text = f"🔥 <b>777! ДЖЕКПОТ!</b> +{reward} XP!" if reward >= 100 else (f"✨ <b>Победа!</b> +{reward} XP!" if reward > 0 else "💨 Мимо! Попробуй ещё раз.")

    if reward > 0:
        await db.add_xp(user_id, chat_id, username, reward)

    await message.reply(text, parse_mode="HTML")

@dp.message(Command("duel"))
async def start_duel(message: types.Message):
    if not message.reply_to_message:
        await message.reply("⚠️ Напиши <code>/duel &lt;ставка&gt;</code> в ответ на сообщение соперника!", parse_mode="HTML")
        return

    challenger = message.from_user
    target = message.reply_to_message.from_user

    if challenger.id == target.id or target.is_bot:
        await message.reply("😅 Нельзя вызывать самого себя или ботов!")
        return

    remains = get_cooldown(challenger.id, "duel", wait_seconds=20)
    if remains > 0:
        await message.reply(f"⏳ Переведи дух! До следующей дуэли: <b>{remains} сек.</b>", parse_mode="HTML")
        return

    parts = message.text.split()
    bet = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 25

    chat_id = message.chat.id
    user_data = await db.get_user(challenger.id, chat_id)
    challenger_xp = user_data["xp"] if user_data else 0

    if challenger_xp < bet:
        await message.reply(f"❌ Не хватает XP! Твой баланс: {challenger_xp} XP.")
        return

    duel_key = f"{chat_id}_{challenger.id}_{target.id}"
    active_duels[duel_key] = {
        "challenger_id": challenger.id,
        "challenger_name": challenger.first_name,
        "target_id": target.id,
        "target_name": target.first_name,
        "bet": bet
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚔️ Принять вызов ({bet} XP)", callback_data=f"accept_duel_{duel_key}")]
    ])

    await message.answer(
        f"⚔️ <b>{challenger.first_name}</b> бросает вызов <b>{target.first_name}</b>!\n"
        f"💰 Ставка: <b>{bet} XP</b>!\n\n"
        f"<i>{target.first_name}, подтверди готовность:</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("accept_duel_"))
async def accept_duel_callback(call: CallbackQuery):
    duel_key = call.data.replace("accept_duel_", "")
    duel = active_duels.get(duel_key)

    if not duel:
        await call.answer("⏳ Время дуэли вышло!", show_alert=True)
        return

    if call.from_user.id != duel["target_id"]:
        await call.answer("🚫 Вызов брошен не тебе!", show_alert=True)
        return

    chat_id = call.message.chat.id
    bet = duel["bet"]
    ch_id = duel["challenger_id"]
    tg_id = duel["target_id"]

    target_data = await db.get_user(tg_id, chat_id)
    target_xp = target_data["xp"] if target_data else 0

    if target_xp < bet:
        await call.answer("❌ У тебя не хватает XP для этой ставки!", show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=None)
    del active_duels[duel_key]

    await call.message.answer("🎲 <b>Дуэль началась!</b> Бросаем кубики...", parse_mode="HTML")

    await call.message.answer(f"🎲 Бросает {duel['challenger_name']}:")
    d1 = await bot.send_dice(chat_id, emoji="🎲")
    await asyncio.sleep(2.5)

    await call.message.answer(f"🎲 Бросает {duel['target_name']}:")
    d2 = await bot.send_dice(chat_id, emoji="🎲")
    await asyncio.sleep(2.5)

    v1, v2 = d1.dice.value, d2.dice.value

    if v1 > v2:
        await db.transfer_xp(tg_id, ch_id, chat_id, bet)
        res = f"🏆 Победил <b>{duel['challenger_name']}</b> ({v1} vs {v2}) и забирает <b>+{bet} XP</b>!"
    elif v2 > v1:
        await db.transfer_xp(ch_id, tg_id, chat_id, bet)
        res = f"🏆 Победил <b>{duel['target_name']}</b> ({v2} vs {v1}) и забирает <b>+{bet} XP</b>!"
    else:
        res = f"🤝 <b>Ничья!</b> ({v1} vs {v2}). Опыт остался на месте."

    await call.message.answer(res, parse_mode="HTML")

@dp.message(Command("sell"))
async def sell_item(message: types.Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("⚠️ Формат: <code>/sell &lt;номер_вещи&gt; &lt;цена_XP&gt;</code>", parse_mode="HTML")
        return

    try:
        item_id, price = int(parts[1]), int(parts[2])
    except ValueError:
        await message.reply("⚠️ Номер и цена должны быть числами!")
        return

    if price <= 0:
        await message.reply("⚠️ Цена должна быть больше 0 XP!")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    name = message.from_user.first_name

    item = await db.get_item_by_id(item_id, user_id, chat_id)
    if not item:
        await message.reply("❌ Предмет не найден в твоём /inv!")
        return

    await db.delete_item(item_id)
    await db.add_market_lot(chat_id, user_id, name, item["item_name"], item["tier"], item["base_xp"], price)

    await message.answer(f"🏷️ <b>{name}</b> выставил на продажу <b>{item['item_name']}</b> за <b>{price} XP</b>!\nРынок: /market", parse_mode="HTML")

@dp.message(Command("market"))
async def show_market(message: types.Message):
    chat_id = message.chat.id
    lots = await db.get_market_lots(chat_id)

    if not lots:
        await message.reply("🏪 Рынок пуст! Продай оружие через /sell.")
        return

    text = "🏪 <b>Торговая площадка чата:</b>\n\n"
    for lot in lots:
        text += f"• Лот <code>#{lot['lot_id']}</code>: <b>{lot['item_name']}</b> [{lot['tier']}]\n  Продавец: <i>{lot['seller_name']}</i> | Цена: <b>{lot['price_xp']} XP</b>\n\n"

    text += "Купить: <code>/buy_lot &lt;номер_лота&gt;</code>"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("buy_lot"))
async def buy_market_lot(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("⚠️ Формат: <code>/buy_lot &lt;номер_лота&gt;</code>", parse_mode="HTML")
        return

    lot_id = int(parts[1])
    buyer_id = message.from_user.id
    chat_id = message.chat.id

    lot = await db.get_lot(lot_id, chat_id)
    if not lot:
        await message.reply("❌ Лот не найден!")
        return

    seller_id, seller_name, item_name, tier, base_xp, price = (
        lot["seller_id"], lot["seller_name"], lot["item_name"], lot["tier"], lot["base_xp"], lot["price_xp"]
    )

    if seller_id == buyer_id:
        await message.reply("😅 Нельзя купить собственный лот!")
        return

    buyer_data = await db.get_user(buyer_id, chat_id)
    buyer_xp = buyer_data["xp"] if buyer_data else 0

    if buyer_xp < price:
        await message.reply(f"❌ Нужно <b>{price} XP</b>, а у тебя только <b>{buyer_xp} XP</b>.", parse_mode="HTML")
        return

    await db.transfer_xp(buyer_id, seller_id, chat_id, price)
    await db.add_item(buyer_id, chat_id, item_name, tier, base_xp)
    await db.delete_lot(lot_id)

    await message.answer(f"🤝 <b>{message.from_user.first_name}</b> купил <b>{item_name}</b> за <b>{price} XP</b>!", parse_mode="HTML")

@dp.message(Command("top"))
async def show_top(message: types.Message):
    chat_id = message.chat.id
    rows = await db.get_top(chat_id, limit=10)

    if not rows:
        await message.reply("В чате пока никто не набрал опыт!")
        return

    top_text = "🏆 <b>Топ участников чата:</b>\n\n"
    for i, user in enumerate(rows, start=1):
        top_text += f"{i}. <b>{user['username']}</b> — Ур. {user['level']} ({user['xp']} XP)\n"

    await message.answer(top_text, parse_mode="HTML")

@dp.message(Command("shop"))
async def open_shop(message: types.Message):
    text = (
        "🛒 <b>Бусты за Telegram Stars ⭐:</b>\n\n"
        "• /buy_small — <b>+250 XP</b> (5 Stars ⭐)\n"
        "• /buy_big — <b>+1000 XP</b> (15 Stars ⭐)"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("buy_small"))
async def buy_small_pack(message: types.Message):
    prices = [LabeledPrice(label="Малый буст: 250 XP", amount=5)]
    await message.answer_invoice(title="Буст 250 XP", description="Прокачка уровня!", payload="stars_250_xp", currency="XTR", prices=prices, provider_token="")

@dp.message(Command("buy_big"))
async def buy_big_pack(message: types.Message):
    prices = [LabeledPrice(label="Мега-буст: 1000 XP", amount=15)]
    await message.answer_invoice(title="Буст 1000 XP", description="Мощный буст!", payload="stars_1000_xp", currency="XTR", prices=prices, provider_token="")

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = get_user_tag(message.from_user)
    reward = 250 if payload == "stars_250_xp" else (1000 if payload == "stars_1000_xp" else 0)

    if reward > 0:
        await db.add_xp(user_id, chat_id, username, reward)
        await message.answer(f"🎉 <b>Спасибо за поддержку!</b> Начислено +{reward} XP!", parse_mode="HTML")

@dp.message(Command("start", "help"))
async def send_help(message: types.Message):
    text = (
        "👋 <b>Команды бота:</b>\n\n"
        "🔫 <b>Оружейные кейсы и маркет:</b>\n"
        "• /case — открыть оружейный кейс (анимация рулетки, КД 45 сек)\n"
        "• /inv — твой инвентарь оружия\n"
        "• /sell &lt;№&gt; &lt;цена&gt; — выставить скин на рынок\n"
        "• /market — торговая площадка\n"
        "• /buy_lot &lt;№&gt; — купить оружие с рынка\n\n"
        "🎲 <b>Игры:</b>\n"
        "• /duel &lt;ставка&gt; — дуэль на костях (в ответ на смс, КД 20 сек)\n"
        "• /slots — игровые автоматы 🎰 (КД 15 сек)\n\n"
        "📊 <b>Профиль:</b>\n"
        "• /rank — карточка игрока (Pillow)\n"
        "• /top — топ лидеров чата\n"
        "• /shop — бусты ⭐"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text)
async def process_xp(message: types.Message):
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    if get_cooldown(user_id, "chat_xp", wait_seconds=30) > 0:
        return

    base_xp = random.randint(5, 15)
    earned = base_xp * 2 if is_double_xp else base_xp
    username = get_user_tag(message.from_user)

    lvl_up, new_lvl, _ = await db.add_xp(user_id, chat_id, username, earned)
    if lvl_up:
        await message.reply(f"🎉 {message.from_user.first_name} повысил уровень до <b>{new_lvl}</b>!", parse_mode="HTML")

# =====================================================================
# 5. СТАРТ
# =====================================================================

async def main():
    await db.connect()
    scheduler.start()
    print("🚀 Оружейный бот запущен! Оружие, кейсы, Бизон Эмбарго и оптимизация активны.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())