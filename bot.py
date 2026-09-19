# -*- coding: utf-8 -*-
"""
Mogg Russia — Telegram-бот-магазин (приём заявок на покупку админки).

ВАЖНО: бот НЕ принимает и НЕ проверяет платежи. Он только создаёт заявку
и отправляет её владельцу. Оплата и выдача админки обсуждаются вручную.

Запуск:   python bot.py
Настройки, цены, скидки, отзывы и заказы хранятся в data.json.
"""

import copy
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================================
#  НАСТРОЙКИ
# ============================================================================

# Токен бота от @BotFather. Вставьте его между кавычками
# (или задайте переменную окружения BOT_TOKEN — так удобнее на VPS/Replit).
BOT_TOKEN = "8704526500:AAGUttPe8RNiLhKCQoqfi83KIVIm954uWRw"

# Только этот Telegram ID считается владельцем/администратором бота.
OWNER_ID = 5139892680

DATA_FILE = Path(__file__).resolve().parent / "data.json"
LOG_FILE = Path(__file__).resolve().parent / "bot.log"

LEVELS = list(range(1, 11))          # уровни админки 1..10
MSK = timezone(timedelta(hours=3))   # время Москвы (без перехода на летнее время)
MAX_OPEN_ORDERS_PER_USER = 5         # защита от спама заявками
MAX_REVIEW_LEN = 300                 # максимальная длина отзыва

STATUS_NEW = "Новая"
STATUS_ACCEPTED = "Принята"
STATUS_REJECTED = "Отклонена"
STATUS_ICONS = {STATUS_NEW: "🆕", STATUS_ACCEPTED: "✅", STATUS_REJECTED: "❌"}

# ключ -> (название для заявки, название на кнопке)
PAYMENTS = {
    "sber": ("Сбер", "🔴 Сбер"),
    "funpay": ("FunPay", "🟣 FunPay"),
}

DISCOUNT_STATES = ("d_menu", "d_levels", "d_percent", "d_hours")

DEFAULT_DATA = {
    "settings": {"shop_name": "Mogg Russia", "admin_username": ""},
    "prices": {str(i): i * 100 for i in LEVELS},
    "discount": {"active": False, "levels": [], "percent": 0, "expires_at": 0},
    "reviews": [],
    "orders": [],
    "last_order_number": 1000,
}

log = logging.getLogger("mogg_bot")


# ============================================================================
#  ХРАНИЛИЩЕ data.json
# ============================================================================

def normalize(raw):
    """Приводит данные из файла к правильному виду, подставляя значения по умолчанию."""
    data = copy.deepcopy(DEFAULT_DATA)

    settings = raw.get("settings")
    if isinstance(settings, dict):
        data["settings"].update(settings)

    prices = raw.get("prices")
    if isinstance(prices, dict):
        for lvl in LEVELS:
            try:
                price = int(round(float(prices.get(str(lvl)))))
                if price <= 0:
                    raise ValueError
                data["prices"][str(lvl)] = price
            except (TypeError, ValueError, OverflowError):
                log.warning("Некорректная цена уровня %s в data.json — использую значение по умолчанию.", lvl)

    disc = raw.get("discount")
    if isinstance(disc, dict):
        try:
            levels = sorted({int(x) for x in disc.get("levels", []) if int(x) in LEVELS})
            percent = int(disc.get("percent", 0))
            expires = float(disc.get("expires_at", 0) or 0)
            active = bool(disc.get("active")) and bool(levels) and 1 <= percent <= 99
            if active:
                data["discount"] = {"active": True, "levels": levels, "percent": percent, "expires_at": expires}
        except (TypeError, ValueError, OverflowError):
            log.warning("Некорректный блок discount в data.json — скидка отключена.")

    reviews = raw.get("reviews")
    if isinstance(reviews, list):
        data["reviews"] = [r for r in reviews if isinstance(r, dict)]

    orders = raw.get("orders")
    if isinstance(orders, list):
        data["orders"] = [o for o in orders if isinstance(o, dict)]

    numbers = [1000]
    try:
        numbers.append(int(raw.get("last_order_number", 1000)))
    except (TypeError, ValueError):
        pass
    for order in data["orders"]:
        try:
            numbers.append(int(order.get("number", 0)))
        except (TypeError, ValueError):
            pass
    data["last_order_number"] = max(numbers)
    return data


class Store:
    """Читает и сохраняет data.json. Если файл изменили вручную — перечитывает его."""

    def __init__(self, path):
        self.path = path
        self.data = None
        self._mtime = None

    def _read_file(self):
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("Корень data.json должен быть объектом { ... }")
        return normalize(raw)

    def start(self):
        """Первая загрузка при запуске. При ошибке в файле — понятное сообщение и остановка."""
        if not self.path.exists():
            self.data = normalize({})
            self.save()
            return
        try:
            self.data = self._read_file()
            self._mtime = self.path.stat().st_mtime_ns
        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(
                f"\nОШИБКА: файл data.json повреждён или содержит ошибку: {e}\n"
                "Откройте его и исправьте (частая причина — лишняя или пропущенная запятая/кавычка),\n"
                "либо переименуйте файл — тогда бот создаст новый (старые заказы останутся в старом файле).\n"
            )

    def get(self):
        """Возвращает актуальные данные (перечитывает файл, если он изменился)."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return self.data
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self.data = self._read_file()
                log.info("data.json изменён вручную — данные перечитаны.")
            except Exception as e:  # noqa: BLE001
                log.error("Не удалось перечитать data.json (%s). Использую прежние данные.", e)
        return self.data

    def save(self):
        """Атомарная запись: сначала во временный файл, потом замена."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns


store = Store(DATA_FILE)


# ============================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def esc(value):
    return html.escape(str(value), quote=False)


def fmt_price(value):
    return f"{int(value):,}".replace(",", "\u00a0") + "\u00a0₽"


def fmt_time(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def fmt_remaining(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts)


def fmt_levels(levels):
    """[1,2,3,5] -> '1–3, 5'"""
    levels = sorted(levels)
    if levels == LEVELS:
        return "все (1–10)"
    chunks, start, prev = [], None, None
    for n in levels:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            chunks.append((start, prev))
            start = prev = n
    if start is not None:
        chunks.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in chunks)


def active_discount(data):
    """Возвращает действующую скидку или None (окончание проверяется автоматически по времени)."""
    d = data.get("discount") or {}
    if d.get("active") and d.get("levels") and d.get("percent", 0) > 0 and d.get("expires_at", 0) > time.time():
        return d
    return None


def level_price(data, level):
    """Возвращает (цена с учётом скидки, базовая цена, процент скидки)."""
    base = int(data["prices"][str(level)])
    d = active_discount(data)
    if d and level in d["levels"]:
        pct = int(d["percent"])
        price = max(1, (base * (100 - pct) + 50) // 100)
        return price, base, pct
    return base, base, 0


def discount_banner(data):
    d = active_discount(data)
    if not d:
        return ""
    left = fmt_remaining(d["expires_at"] - time.time())
    return f"🔥 <b>Скидка {d['percent']}%</b> на уровни: {fmt_levels(d['levels'])}\n⏳ Осталось: {left}"


def is_owner(update):
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


def admin_url(data):
    username = str(data["settings"].get("admin_username", "") or "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        return f"https://t.me/{username}"
    return f"tg://user?id={OWNER_ID}"


# ---- разбор ввода владельца (скидки) ---------------------------------------

def parse_levels(text):
    t = text.strip().lower()
    if not t:
        raise ValueError("Пустой ввод. Пример: 1,3,5 или 1-10 или «все».")
    if t in ("все", "всё", "all", "*"):
        return list(LEVELS)
    t = re.sub(r"\s*[-–—]\s*", "-", t)
    tokens = [x for x in re.split(r"[,\s;]+", t) if x]
    result = set()
    for tok in tokens:
        m = re.fullmatch(r"([0-9]{1,3})-([0-9]{1,3})", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            numbers = range(a, b + 1)
        elif re.fullmatch(r"[0-9]{1,3}", tok):
            numbers = [int(tok)]
        else:
            raise ValueError(f"Не понял «{tok}». Пример: 1,3,5 или 1-10 или «все».")
        for n in numbers:
            if n not in LEVELS:
                raise ValueError(f"Уровня {n} не существует. Доступны уровни от 1 до 10.")
            result.add(n)
    if not result:
        raise ValueError("Не выбрано ни одного уровня.")
    return sorted(result)


def parse_percent(text):
    m = re.fullmatch(r"\s*([0-9]{1,3})\s*%?\s*", text)
    if not m or not 1 <= int(m.group(1)) <= 99:
        raise ValueError("Скидка — целое число от 1 до 99 (например 30 или 30%).")
    return int(m.group(1))


def parse_hours(text):
    m = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:ч|час|часа|часов|h)?\s*", text.lower())
    if not m:
        raise ValueError("Длительность — число часов, например 24.")
    hours = float(m.group(1).replace(",", "."))
    if not 0 < hours <= 8760:
        raise ValueError("Длительность должна быть больше 0 и не больше 8760 часов (365 дней).")
    return hours


def parse_quick(text):
    """'1-10, 30, 24' -> ([1..10], 30, 24.0)"""
    t = re.sub(r"\s*[-–—]\s*", "-", text.strip().lower())
    tokens = [x for x in re.split(r"[,\s;]+", t) if x and x not in ("%", "ч", "час", "часов", "h")]
    if len(tokens) < 3:
        raise ValueError("Формат: уровни, процент, часы. Например: 1-10, 30, 24")
    hours = parse_hours(tokens[-1])
    percent = parse_percent(tokens[-2])
    levels = parse_levels(" ".join(tokens[:-2]))
    return levels, percent, hours


def clean_nick(text):
    nick = " ".join(text.strip().lstrip("@").split())
    if not re.fullmatch(r"[\w.\-\[\]() ]{2,32}", nick):
        raise ValueError(
            "Ник должен быть длиной от 2 до 32 символов и содержать только буквы, цифры и символы _ - . [ ] ( )."
        )
    return nick


# ============================================================================
#  КЛАВИАТУРЫ И ТЕКСТЫ
# ============================================================================

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def back_row(target="m:main"):
    return [btn("◀️ Назад", target)]


def main_menu_kb():
    return InlineKeyboardMarkup([
        [btn("🛒 Купить админку", "m:buy")],
        [btn("⭐ Отзывы", "m:reviews")],
        [btn("ℹ️ Информация", "m:info")],
        [btn("💬 Поддержка", "m:support")],
    ])


def contact_kb(data, back_target="m:main"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Связаться с администратором", url=admin_url(data))],
        back_row(back_target),
    ])


def order_kb(order):
    uid = order["user_id"]
    username = order.get("username")
    url = f"https://t.me/{username}" if username else f"tg://user?id={uid}"
    rows = [[InlineKeyboardButton("💬 Написать клиенту", url=url)]]
    if order.get("status") == STATUS_NEW:
        num = order["number"]
        rows.append([btn("✅ Принять", f"ord:a:{num}"), btn("❌ Отклонить", f"ord:r:{num}")])
    return InlineKeyboardMarkup(rows)


def format_order_message(o):
    if o.get("username"):
        tg = f"@{esc(o['username'])}"
    else:
        tg = f'<a href="tg://user?id={o["user_id"]}">{esc(o.get("full_name") or "профиль")}</a> (нет username)'
    price = fmt_price(o["price"])
    if o.get("discount_percent"):
        price += f" (скидка {o['discount_percent']}%, было {fmt_price(o['base_price'])})"
    header = "🛒 <b>НОВАЯ ЗАЯВКА</b>" if o.get("status") == STATUS_NEW else "🛒 <b>ЗАЯВКА</b>"
    icon = STATUS_ICONS.get(o.get("status"), "")
    return (
        f"{header}\n\n"
        f"Номер: <b>#{o['number']}</b>\n\n"
        f"👤 Telegram: {tg}\n"
        f"🆔 ID: <code>{o['user_id']}</code>\n"
        f"🎮 Игровой ник: <code>{esc(o['nick'])}</code>\n"
        f"⭐ Уровень: {o['level']}\n"
        f"💳 Оплата: {esc(o['payment'])}\n"
        f"💰 Цена: {price}\n\n"
        f"📌 Статус: {icon} {esc(o.get('status', ''))}"
    )


INFO_TEXT = (
    "Mogg Russia — магазин игровых привилегий.\n\n"
    "Здесь можно оставить заявку на покупку админки от 1 до 10 уровня.\n\n"
    "Доступные способы оплаты:\n"
    "🔴 Сбер\n"
    "🟣 FunPay\n\n"
    "После выбора способа оплаты заявка передаётся администратору. "
    "Все детали оплаты и выдачи админки обсуждаются напрямую с администратором."
)

SUPPORT_TEXT = "По вопросам покупки, оплаты и работы магазина обращайтесь к администратору."


# ============================================================================
#  ОТПРАВКА СООБЩЕНИЙ (с защитой от ошибок Telegram)
# ============================================================================

def _has_user_url(kb):
    if kb is None:
        return False
    return any((b.url or "").startswith("tg://user") for row in kb.inline_keyboard for b in row)


def _strip_user_urls(kb):
    rows = []
    for row in kb.inline_keyboard:
        kept = [b for b in row if not (b.url or "").startswith("tg://user")]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(rows) if rows else None


def _variants(kb):
    """Если Telegram не принял кнопку-ссылку на профиль (настройки приватности) — пробуем без неё."""
    variants = [kb]
    if _has_user_url(kb):
        variants.append(_strip_user_urls(kb))
    return variants


async def render(update, context, text, kb=None):
    """Показывает экран: редактирует сообщение (для кнопок) или отправляет новое (для текста)."""
    query = update.callback_query
    variants = _variants(kb)
    for markup in variants:
        try:
            if query is not None:
                await query.edit_message_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
            else:
                await update.effective_message.reply_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
            return
        except BadRequest as e:
            if query is not None and "message is not modified" in str(e).lower():
                return
            log.warning("Не удалось показать экран (%s), пробую запасной вариант.", e)
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_message(
            chat.id, text, reply_markup=variants[-1], parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def send_safe(bot, chat_id, text, kb=None):
    """Отправка сообщения без риска упасть. Возвращает True/False."""
    for markup in _variants(kb):
        try:
            await bot.send_message(
                chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            return True
        except BadRequest as e:
            log.warning("Ошибка отправки сообщения %s: %s", chat_id, e)
        except TelegramError as e:
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
            return False
    return False


async def safe_answer(query, text=None, alert=False):
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError as e:
        log.debug("query.answer: %s", e)


# ============================================================================
#  ГЛАВНОЕ МЕНЮ, ИНФОРМАЦИЯ, ПОДДЕРЖКА, ОТЗЫВЫ
# ============================================================================

async def show_main(update, context):
    context.user_data.clear()
    data = store.get()
    text = f"👋 Добро пожаловать в <b>{esc(data['settings'].get('shop_name', 'Mogg Russia'))}</b>!\n\nВыберите действие:"
    banner = discount_banner(data)
    if banner:
        text += f"\n\n{banner}"
    await render(update, context, text, main_menu_kb())


async def cmd_start(update, context):
    await show_main(update, context)


async def cmd_help(update, context):
    if is_owner(update):
        text = (
            "🛠 <b>Команды администратора</b>\n\n"
            "/skidka — панель скидок\n"
            "/orders — последние заявки\n"
            "/order 1001 — показать заявку по номеру\n"
            "/addreview Имя | 5 | Текст — добавить отзыв\n"
            "/delreview 2 — удалить отзыв №2\n"
            "/start — главное меню"
        )
        await render(update, context, text, InlineKeyboardMarkup([back_row("m:main")]))
    else:
        await show_main(update, context)


async def show_info(update, context):
    context.user_data.clear()
    await render(update, context, esc(INFO_TEXT), InlineKeyboardMarkup([back_row("m:main")]))


async def show_support(update, context):
    context.user_data.clear()
    await render(update, context, esc(SUPPORT_TEXT), contact_kb(store.get()))


def stars(rating):
    try:
        return "⭐" * max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        return ""


async def show_reviews(update, context):
    context.user_data.clear()
    reviews = store.get()["reviews"]
    if not reviews:
        text = "⭐ <b>Отзывы</b>\n\nПока отзывов нет."
    else:
        blocks, total = [], 0
        for idx in range(len(reviews) - 1, -1, -1):  # сначала новые
            r = reviews[idx]
            author = esc(r.get("author") or "Покупатель")
            rate = stars(r.get("rating"))
            block = f"<b>№{idx + 1} · {author}</b> {rate}\n{esc(r.get('text', ''))}"
            if r.get("date"):
                block += f"\n<i>{esc(r['date'])}</i>"
            if len(blocks) >= 10 or total + len(block) > 3500:
                break
            blocks.append(block)
            total += len(block) + 2
        text = "⭐ <b>Отзывы покупателей</b>\n\n" + "\n\n".join(blocks)
    await render(update, context, text, InlineKeyboardMarkup([back_row("m:main")]))


# ============================================================================
#  ПОКУПКА АДМИНКИ
# ============================================================================

async def show_levels(update, context):
    context.user_data.clear()
    data = store.get()
    lines = ["🛒 <b>Купить админку</b>", "", "Выберите уровень:"]
    banner = discount_banner(data)
    if banner:
        lines += ["", banner, ""]
        for lvl in LEVELS:
            price, base, pct = level_price(data, lvl)
            if pct:
                lines.append(f"{lvl} уровень — <s>{fmt_price(base)}</s> → <b>{fmt_price(price)}</b>")
    rows = []
    for lvl in LEVELS:
        price, base, pct = level_price(data, lvl)
        label = f"{lvl} уровень — {fmt_price(price)}"
        if pct:
            label += f" 🔥 −{pct}%"
        rows.append([btn(label, f"lvl:{lvl}")])
    rows.append(back_row("m:main"))
    await render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))


async def choose_level(update, context, level):
    data = store.get()
    if level not in LEVELS:
        await show_levels(update, context)
        return
    price, base, pct = level_price(data, level)
    ud = context.user_data
    ud.clear()
    ud["state"] = "nick"
    ud["level"] = level
    price_text = fmt_price(price) + (f" (скидка {pct}%)" if pct else "")
    text = (
        f"⭐ Выбран <b>{level} уровень</b> — {price_text}\n\n"
        "🎮 Введите ваш игровой ник (одним сообщением):"
    )
    await render(update, context, text, InlineKeyboardMarkup([back_row("m:buy")]))


def summary_text(data, level, nick, note=""):
    price, base, pct = level_price(data, level)
    price_line = fmt_price(price)
    if pct:
        price_line += f" (скидка {pct}%, было {fmt_price(base)})"
    return (
        f"{note}"
        "📋 <b>Проверьте данные заявки</b>\n\n"
        f"⭐ Уровень: <b>{level}</b>\n"
        f"🎮 Игровой ник: <code>{esc(nick)}</code>\n"
        f"💰 Итоговая цена: <b>{price_line}</b>\n\n"
        "Выберите способ оплаты:"
    )


def summary_kb(level):
    return InlineKeyboardMarkup([
        [btn(PAYMENTS["sber"][1], "pay:sber"), btn(PAYMENTS["funpay"][1], "pay:funpay")],
        [btn("✏️ Изменить ник", f"lvl:{level}")],
        back_row("m:buy"),
    ])


async def handle_nick(update, context, text):
    ud = context.user_data
    level = ud.get("level")
    if level not in LEVELS:
        await show_levels(update, context)
        return
    try:
        nick = clean_nick(text)
    except ValueError as e:
        await render(update, context, f"⚠️ {esc(e)}\n\nВведите ник ещё раз:", InlineKeyboardMarkup([back_row("m:buy")]))
        return
    data = store.get()
    ud["nick"] = nick
    ud["state"] = "confirm"
    ud["price"] = level_price(data, level)[0]
    await render(update, context, summary_text(data, level, nick), summary_kb(level))


def create_order(user, level, nick, payment_key):
    data = store.get()
    price, base, pct = level_price(data, level)
    data["last_order_number"] += 1
    order = {
        "number": data["last_order_number"],
        "user_id": user.id,
        "username": user.username or "",
        "full_name": user.full_name or "",
        "nick": nick,
        "level": level,
        "payment": PAYMENTS[payment_key][0],
        "price": price,
        "base_price": base,
        "discount_percent": pct,
        "status": STATUS_NEW,
        "created_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "updated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "owner_notified": False,
    }
    data["orders"].append(order)
    try:
        store.save()
    except Exception:
        data["orders"].pop()
        data["last_order_number"] -= 1
        raise
    return order


async def choose_payment(update, context, key):
    ud = context.user_data
    level, nick = ud.get("level"), ud.get("nick")
    if ud.get("state") != "confirm" or key not in PAYMENTS or level not in LEVELS or not nick:
        await render(
            update, context,
            "⌛ Оформление устарело. Пожалуйста, начните заново.",
            InlineKeyboardMarkup([[btn("🛒 Купить админку", "m:buy")], back_row("m:main")]),
        )
        return
    user = update.effective_user
    data = store.get()

    # Цена могла измениться (началась/закончилась скидка) — показываем актуальную и просим подтвердить.
    current_price = level_price(data, level)[0]
    if current_price != ud.get("price"):
        ud["price"] = current_price
        note = "⚠️ <b>Цена изменилась</b> (изменилась скидка). Проверьте данные:\n\n"
        await render(update, context, summary_text(data, level, nick, note), summary_kb(level))
        return

    open_orders = sum(1 for o in data["orders"] if o.get("user_id") == user.id and o.get("status") == STATUS_NEW)
    if open_orders >= MAX_OPEN_ORDERS_PER_USER:
        await render(
            update, context,
            "⚠️ У вас уже много необработанных заявок. Дождитесь ответа администратора.",
            contact_kb(data),
        )
        return

    order = create_order(user, level, nick, key)
    ud.clear()

    # 1) уведомляем владельца
    ok = await send_safe(context.bot, OWNER_ID, format_order_message(order), order_kb(order))
    if ok:
        stored = next((o for o in store.get()["orders"] if o.get("number") == order["number"]), None)
        if stored is not None:
            stored["owner_notified"] = True
            try:
                store.save()
            except OSError as e:
                log.error("Не удалось сохранить флаг owner_notified: %s", e)
    else:
        log.error(
            "Заявка #%s сохранена, но владельцу отправить не удалось. "
            "Владелец должен один раз нажать /start в боте. Заявку можно открыть командой /order %s",
            order["number"], order["number"],
        )

    # 2) подтверждаем пользователю
    text = (
        "✅ <b>Заявка создана. Ожидайте связи с администратором.</b>\n\n"
        f"Номер заявки: <b>#{order['number']}</b>\n"
        f"⭐ Уровень: {order['level']}\n"
        f"🎮 Ник: <code>{esc(order['nick'])}</code>\n"
        f"💳 Оплата: {esc(order['payment'])}\n"
        f"💰 Цена: {fmt_price(order['price'])}"
    )
    await render(update, context, text, contact_kb(store.get()))


# ============================================================================
#  ДЕЙСТВИЯ ВЛАДЕЛЬЦА С ЗАЯВКАМИ
# ============================================================================

async def handle_order_action(update, context, rest):
    query = update.callback_query
    if not is_owner(update):
        await safe_answer(query, "⛔ Доступ запрещён", alert=True)
        return
    action, _, num_text = rest.partition(":")
    try:
        number = int(num_text)
    except ValueError:
        await safe_answer(query, "Некорректная заявка", alert=True)
        return

    data = store.get()
    order = next((o for o in data["orders"] if o.get("number") == number), None)
    if order is None or action not in ("a", "r"):
        await safe_answer(query, "Заявка не найдена", alert=True)
        return
    if order.get("status") != STATUS_NEW:
        await safe_answer(query, f"Заявка уже обработана: {order.get('status')}", alert=True)
        await render(update, context, format_order_message(order), order_kb(order))
        return

    order["status"] = STATUS_ACCEPTED if action == "a" else STATUS_REJECTED
    order["updated_at"] = datetime.now(MSK).isoformat(timespec="seconds")
    store.save()
    await safe_answer(query, f"Заявка #{number}: {order['status']}")
    await render(update, context, format_order_message(order), order_kb(order))

    # уведомляем клиента (если он не заблокировал бота — ошибки игнорируются)
    if action == "a":
        text = (
            f"✅ Ваша заявка <b>#{number}</b> принята.\n"
            "Администратор свяжется с вами для обсуждения оплаты и выдачи админки."
        )
    else:
        text = (
            f"❌ Ваша заявка <b>#{number}</b> отклонена.\n"
            "Если у вас есть вопросы — свяжитесь с администратором."
        )
    await send_safe(context.bot, order["user_id"], text, contact_kb(store.get()))


async def cmd_orders(update, context):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ Доступ запрещён.")
        return
    orders = store.get()["orders"]
    if not orders:
        await update.effective_message.reply_text("Заявок пока нет.")
        return
    lines = ["📦 <b>Последние заявки</b>", ""]
    for o in reversed(orders[-15:]):
        who = f"@{esc(o['username'])}" if o.get("username") else f"ID {o.get('user_id')}"
        lines.append(
            f"#{o.get('number')} · {STATUS_ICONS.get(o.get('status'), '')} {esc(o.get('status', ''))} · {who} · "
            f"{o.get('level')} ур. · {esc(o.get('payment', ''))} · {fmt_price(o.get('price', 0))}"
        )
    lines.append("\nОткрыть заявку: /order 1001")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_order(update, context):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ Доступ запрещён.")
        return
    try:
        number = int(context.args[0].lstrip("#"))
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Использование: /order 1001")
        return
    order = next((o for o in store.get()["orders"] if o.get("number") == number), None)
    if order is None:
        await update.effective_message.reply_text(f"Заявка #{number} не найдена.")
        return
    ok = await send_safe(context.bot, update.effective_chat.id, format_order_message(order), order_kb(order))
    if not ok:
        await update.effective_message.reply_text("Не удалось показать заявку. Смотрите bot.log.")


# ============================================================================
#  ОТЗЫВЫ (управляет только владелец)
# ============================================================================

async def cmd_addreview(update, context):
    msg = update.effective_message
    if not is_owner(update):
        await msg.reply_text("⛔ Доступ запрещён.")
        return
    usage = "Формат: /addreview Имя | 5 | Текст отзыва\n(оценка 1–5 необязательна: /addreview Имя | Текст отзыва)"
    parts_raw = (msg.text or "").split(None, 1)
    if len(parts_raw) < 2:
        await msg.reply_text(usage)
        return
    parts = [p.strip() for p in parts_raw[1].split("|", 2)]
    rating = None
    if len(parts) == 3:
        author, rating_text, text = parts
        if not re.fullmatch(r"[1-5]", rating_text):
            await msg.reply_text("Оценка должна быть числом от 1 до 5.\n\n" + usage)
            return
        rating = int(rating_text)
    elif len(parts) == 2:
        author, text = parts
    else:
        await msg.reply_text(usage)
        return
    if not author or not text:
        await msg.reply_text("Имя и текст отзыва не должны быть пустыми.\n\n" + usage)
        return
    if len(author) > 40 or len(text) > MAX_REVIEW_LEN:
        await msg.reply_text(f"Слишком длинно: имя — до 40 символов, отзыв — до {MAX_REVIEW_LEN}.")
        return
    data = store.get()
    data["reviews"].append({
        "author": author,
        "rating": rating,
        "text": text,
        "date": datetime.now(MSK).strftime("%d.%m.%Y"),
    })
    store.save()
    await msg.reply_text(f"✅ Отзыв добавлен (№{len(data['reviews'])}).")


async def cmd_delreview(update, context):
    msg = update.effective_message
    if not is_owner(update):
        await msg.reply_text("⛔ Доступ запрещён.")
        return
    try:
        number = int(context.args[0].lstrip("№#"))
    except (IndexError, ValueError):
        await msg.reply_text("Использование: /delreview 2  (номер отзыва из списка «Отзывы»)")
        return
    data = store.get()
    if not 1 <= number <= len(data["reviews"]):
        await msg.reply_text(f"Отзыва №{number} нет. Всего отзывов: {len(data['reviews'])}.")
        return
    data["reviews"].pop(number - 1)
    store.save()
    await msg.reply_text(f"🗑 Отзыв №{number} удалён. Номера последующих отзывов сдвинулись.")


# ============================================================================
#  СКИДКИ (только владелец)
# ============================================================================

def discount_panel_text(data, note=""):
    d = active_discount(data)
    if d:
        status = (
            f"🔥 Сейчас действует: <b>{d['percent']}%</b> на уровни: {fmt_levels(d['levels'])}\n"
            f"⏳ До {fmt_time(d['expires_at'])} (осталось {fmt_remaining(d['expires_at'] - time.time())})"
        )
    else:
        status = "Сейчас скидка не действует."
    return (
        f"{note}"
        "🏷 <b>Панель скидок</b>\n\n"
        f"{status}\n\n"
        "<b>Как установить скидку:</b>\n"
        "• кнопками ниже (уровни → процент → часы), или\n"
        "• одним сообщением: уровни, процент, часы. Например:\n"
        "<code>1-10, 30, 24</code> — скидка 30% на уровни 1–10 на 24 часа\n"
        "<code>1,3,5, 20, 12</code> — скидка 20% на уровни 1, 3 и 5 на 12 часов\n"
        "<code>все, 50, 6</code> — скидка 50% на все уровни на 6 часов\n\n"
        "Новая скидка заменяет предыдущую."
    )


def discount_panel_kb():
    return InlineKeyboardMarkup([
        [btn("🎯 Выбрать уровни", "d:pick"), btn("🌐 Все уровни", "d:all")],
        [btn("🛑 Отключить скидку", "d:off")],
        back_row("m:main"),
    ])


async def show_discount_panel(update, context, note=""):
    ud = context.user_data
    ud.clear()
    ud["state"] = "d_menu"
    await render(update, context, discount_panel_text(store.get(), note), discount_panel_kb())


async def cmd_skidka(update, context):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ Доступ запрещён. Эта команда только для администратора.")
        return
    await show_discount_panel(update, context)


async def ask_percent(update, context, levels):
    ud = context.user_data
    ud.clear()
    ud["state"] = "d_percent"
    ud["d_levels"] = levels
    text = (
        f"Уровни: <b>{fmt_levels(levels)}</b>\n\n"
        "Выберите размер скидки кнопкой или отправьте число от 1 до 99 (например: 25):"
    )
    kb = InlineKeyboardMarkup([
        [btn("10%", "d:pct:10"), btn("20%", "d:pct:20"), btn("30%", "d:pct:30"), btn("50%", "d:pct:50")],
        back_row("d:panel"),
    ])
    await render(update, context, text, kb)


async def ask_hours(update, context, percent):
    ud = context.user_data
    ud["state"] = "d_hours"
    ud["d_percent"] = percent
    text = (
        f"Уровни: <b>{fmt_levels(ud['d_levels'])}</b>\nСкидка: <b>{percent}%</b>\n\n"
        "Выберите длительность кнопкой или отправьте число часов (например: 24):"
    )
    kb = InlineKeyboardMarkup([
        [btn("6 ч", "d:hrs:6"), btn("12 ч", "d:hrs:12"), btn("24 ч", "d:hrs:24")],
        [btn("48 ч", "d:hrs:48"), btn("72 ч", "d:hrs:72"), btn("7 дней", "d:hrs:168")],
        back_row("d:panel"),
    ])
    await render(update, context, text, kb)


async def finish_discount(update, context, levels, percent, hours):
    data = store.get()
    expires = time.time() + hours * 3600
    data["discount"] = {"active": True, "levels": levels, "percent": percent, "expires_at": expires}
    store.save()
    ud = context.user_data
    ud.clear()
    ud["state"] = "d_menu"
    hours_text = f"{hours:g}"
    text = (
        "✅ <b>Скидка установлена</b>\n\n"
        f"Уровни: {fmt_levels(levels)}\n"
        f"Скидка: {percent}%\n"
        f"Длительность: {hours_text} ч\n"
        f"Действует до: {fmt_time(expires)}\n\n"
        "Цены в меню уже обновлены."
    )
    kb = InlineKeyboardMarkup([[btn("🏷 Панель скидок", "d:panel")], back_row("m:main")])
    await render(update, context, text, kb)


async def handle_discount_cb(update, context, rest):
    query = update.callback_query
    if not is_owner(update):
        await safe_answer(query, "⛔ Доступ запрещён", alert=True)
        return
    await safe_answer(query)
    ud = context.user_data
    action, _, arg = rest.partition(":")

    if action == "panel":
        await show_discount_panel(update, context)
    elif action == "pick":
        ud.clear()
        ud["state"] = "d_levels"
        text = (
            "Введите уровни, на которые действует скидка:\n\n"
            "<code>1,3,5</code> — конкретные уровни\n"
            "<code>1-10</code> — диапазон\n"
            "<code>все</code> — все уровни"
        )
        await render(update, context, text, InlineKeyboardMarkup([back_row("d:panel")]))
    elif action == "all":
        await ask_percent(update, context, list(LEVELS))
    elif action == "pct":
        try:
            percent = parse_percent(arg)
        except ValueError:
            await show_discount_panel(update, context)
            return
        if not ud.get("d_levels"):
            await show_discount_panel(update, context, "⌛ Данные устарели, начните заново.\n\n")
            return
        await ask_hours(update, context, percent)
    elif action == "hrs":
        try:
            hours = parse_hours(arg)
        except ValueError:
            await show_discount_panel(update, context)
            return
        if not ud.get("d_levels") or not ud.get("d_percent"):
            await show_discount_panel(update, context, "⌛ Данные устарели, начните заново.\n\n")
            return
        await finish_discount(update, context, ud["d_levels"], ud["d_percent"], hours)
    elif action == "off":
        data = store.get()
        was_active = active_discount(data) is not None
        data["discount"] = copy.deepcopy(DEFAULT_DATA["discount"])
        store.save()
        note = "🛑 Скидка отключена.\n\n" if was_active else "Скидка и так не действовала.\n\n"
        await show_discount_panel(update, context, note)
    else:
        await show_discount_panel(update, context)


async def handle_discount_text(update, context, text):
    ud = context.user_data
    state = ud.get("state")
    back_kb = InlineKeyboardMarkup([back_row("d:panel")])
    try:
        if state == "d_menu":
            levels, percent, hours = parse_quick(text)
            await finish_discount(update, context, levels, percent, hours)
        elif state == "d_levels":
            levels = parse_levels(text)
            await ask_percent(update, context, levels)
        elif state == "d_percent":
            percent = parse_percent(text)
            await ask_hours(update, context, percent)
        elif state == "d_hours":
            hours = parse_hours(text)
            if not ud.get("d_levels") or not ud.get("d_percent"):
                await show_discount_panel(update, context, "⌛ Данные устарели, начните заново.\n\n")
                return
            await finish_discount(update, context, ud["d_levels"], ud["d_percent"], hours)
    except ValueError as e:
        await render(update, context, f"⚠️ {esc(e)}\n\nПопробуйте ещё раз.", back_kb)


async def discount_watchdog(context):
    """Раз в 30 секунд проверяет, не закончилась ли скидка, и отключает её."""
    data = store.get()
    d = data.get("discount") or {}
    if d.get("active") and d.get("expires_at", 0) <= time.time():
        data["discount"] = copy.deepcopy(DEFAULT_DATA["discount"])
        store.save()
        log.info("Скидка закончилась и отключена автоматически.")
        await send_safe(context.bot, OWNER_ID, "⏰ Скидка закончилась и отключена автоматически. Цены вернулись к обычным.")


# ============================================================================
#  РОУТЕРЫ
# ============================================================================

async def on_callback(update, context):
    query = update.callback_query
    if query is None:
        return
    kind, _, rest = (query.data or "").partition(":")

    if kind == "ord":
        await handle_order_action(update, context, rest)
        return
    if kind == "d":
        await handle_discount_cb(update, context, rest)
        return

    await safe_answer(query)
    if kind == "m":
        handlers = {
            "main": show_main,
            "buy": show_levels,
            "reviews": show_reviews,
            "info": show_info,
            "support": show_support,
        }
        await handlers.get(rest, show_main)(update, context)
    elif kind == "lvl":
        try:
            level = int(rest)
        except ValueError:
            level = 0
        await choose_level(update, context, level)
    elif kind == "pay":
        await choose_payment(update, context, rest)
    else:
        await show_main(update, context)


async def on_text(update, context):
    text = (update.effective_message.text or "").strip()
    ud = context.user_data
    state = ud.get("state")

    if state == "nick":
        await handle_nick(update, context, text)
    elif state == "confirm":
        level, nick = ud.get("level"), ud.get("nick")
        if level in LEVELS and nick:
            await render(update, context, summary_text(store.get(), level, nick), summary_kb(level))
        else:
            await show_main(update, context)
    elif state in DISCOUNT_STATES:
        if is_owner(update):
            await handle_discount_text(update, context, text)
        else:
            await show_main(update, context)
    else:
        await render(update, context, "Не совсем понял 🤔 Пользуйтесь кнопками меню:", main_menu_kb())


async def on_non_text(update, context):
    state = context.user_data.get("state")
    if state == "nick":
        await render(update, context, "⚠️ Пожалуйста, отправьте ник обычным текстом.",
                     InlineKeyboardMarkup([back_row("m:buy")]))
    elif state in DISCOUNT_STATES and is_owner(update):
        await render(update, context, "⚠️ Пожалуйста, отправьте текстовое сообщение.",
                     InlineKeyboardMarkup([back_row("d:panel")]))


async def on_unknown_command(update, context):
    await render(update, context, "Неизвестная команда. Откройте меню:", main_menu_kb())


async def on_error(update, context):
    error = context.error
    if isinstance(error, NetworkError):
        log.warning("Сетевая ошибка: %s", error)
        return
    log.error("Необработанная ошибка", exc_info=error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите /start.",
            )
        except TelegramError:
            pass


# ============================================================================
#  ЗАПУСК
# ============================================================================

def setup_logging():
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    setup_logging()

    token = (os.getenv("BOT_TOKEN") or BOT_TOKEN).strip()
    if not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token):
        raise SystemExit(
            "\nОШИБКА: не указан токен бота.\n"
            "Откройте bot.py, найдите строку BOT_TOKEN = \"...\" и вставьте токен от @BotFather\n"
            "(или задайте переменную окружения BOT_TOKEN). Подробности — в README.txt.\n"
        )

    store.start()

    # Принудительно обновляем цены при каждом запуске.
    # Остальные данные data.json (заказы, отзывы, скидки и т.д.) сохраняются.
    store.data["prices"] = {
        "1": 50,
        "2": 75,
        "3": 100,
        "4": 150,
        "5": 200,
        "6": 225,
        "7": 250,
        "8": 275,
        "9": 300,
        "10": 400,
    }
    store.save()

    app = Application.builder().token(token).build()
    private = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler(["start", "menu", "cancel"], cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CommandHandler("skidka", cmd_skidka, filters=private))
    app.add_handler(CommandHandler("orders", cmd_orders, filters=private))
    app.add_handler(CommandHandler("order", cmd_order, filters=private))
    app.add_handler(CommandHandler("addreview", cmd_addreview, filters=private))
    app.add_handler(CommandHandler("delreview", cmd_delreview, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND & private, on_non_text))
    app.add_handler(MessageHandler(filters.COMMAND & private, on_unknown_command))
    app.add_error_handler(on_error)

    if app.job_queue is not None:
        app.job_queue.run_repeating(discount_watchdog, interval=30, first=5)
    else:
        log.warning(
            "JobQueue недоступен (установите зависимости: pip install -r requirements.txt). "
            "Скидки всё равно будут заканчиваться вовремя, но без уведомления владельцу."
        )

    log.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
