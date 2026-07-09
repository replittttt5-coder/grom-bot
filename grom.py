import asyncio
import json
import os
import random
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telethon import TelegramClient, events, Button
from telethon.extensions import markdown as tl_markdown
from telethon.tl.types import (
    MessageEntityTextUrl, MessageEntityCustomEmoji, MessageEntitySpoiler,
    Invoice, LabeledPrice, InputMediaInvoice, DataJSON,
    UpdateBotPrecheckoutQuery, MessageActionPaymentSentMe,
)
from telethon.tl.functions.messages import SendMediaRequest, SetBotPrecheckoutResultsRequest
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError

# ========== КОНФИГ ==========
# Значения ниже — это фолбэки по умолчанию. Если переменные окружения
# TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_BOT_TOKEN заданы — используются они,
# иначе используются значения по умолчанию (можно просто запустить: python bot.py)
API_ID = os.environ.get("TELEGRAM_API_ID", "30829847")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "ee19553ced2ae8139ce441c423ec7a19")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8543625474:AAHTgmQr6zuxgeyFbyGzOhpzO3f7T1I4KB0")

if not API_ID or not API_HASH:
    raise RuntimeError(
        "TELEGRAM_API_ID / TELEGRAM_API_HASH not set. "
        "Get them for free at https://my.telegram.org -> API Development Tools"
    )
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

API_ID = int(API_ID)

ADMIN_IDS = [5454585281]

RANK_NAMES = {1: "Стажёр", 2: "Админ", 3: "Старший админ", 4: "Правая рука"}
RANK_TRAINEE = 1
RANK_ADMIN = 2
RANK_SENIOR = 3
RANK_RIGHT_HAND = 4
DAILY_GIVE_LIMITS = {
    RANK_TRAINEE: 100000,   # стажёр — до 100 000 GROM в сутки
    RANK_ADMIN: 250000,     # админ — до 250 000 GROM в сутки
    RANK_SENIOR: 500000,    # старший админ — до 500 000 GROM в сутки
    # правая рука и главный админ — без ограничений
}

COMPANY_PRICE_DEFAULT = 100000  # стоимость покупки компании по умолчанию
COMPANY_MAX_CHANCE = 70         # максимальный % шанса, который может выставить сама компания
COMPANY_DEBT_LIMIT = -100000    # при таком балансе (или хуже) компания замораживается
COMPANY_FREEZE_HOURS = 6        # сколько часов даётся на погашение долга
COMPANY_WITHDRAW_MIN = 300000   # с какой суммы можно подать заявку на вывод

STAR_PACKS = [
    (10, 50000),
    (15, 100000),
    (25, 250000),
]  # список (stars, grom)

REQUIRED_CHANNELS = ["gromobotchat", "gromofficia"]  # обязательная подписка для /start


async def is_subscribed(user_id: int) -> bool:
    for ch in REQUIRED_CHANNELS:
        try:
            await client(GetParticipantRequest(ch, user_id))
        except UserNotParticipantError:
            return False
        except Exception:
            continue  # если бот не может проверить (не админ/канал недоступен) — не блокируем
    return True


def subscribe_buttons():
    buttons = [[Button.url(f"📢 Подписаться @{ch}", f"https://t.me/{ch}")] for ch in REQUIRED_CHANNELS]
    buttons.append([Button.inline("✅ Я подписался", data="check_subscription")])
    return buttons

BASE_DIR = os.path.dirname(__file__)
DATA_FILE = os.path.join(BASE_DIR, "data.json")
GIF_FILE = os.path.join(BASE_DIR, "gif_id.txt")   # хранит путь к сохранённому gif-файлу
GIF_MEDIA_PATH = os.path.join(BASE_DIR, "roulette_gif.mp4")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
client = TelegramClient("bot_session", API_ID, API_HASH)


def apply_defaults(d: dict) -> dict:
    d.setdefault("users", {})
    d.setdefault("bonus_cooldown", {})
    d.setdefault("tournaments", {})
    d.setdefault("next_tournament_id", 1)
    d.setdefault("profiles", {})
    d.setdefault("clans", {})
    d.setdefault("next_clan_id", 1)
    d.setdefault("roulette_history", {})
    d.setdefault("active_roulette_bets", {})
    d.setdefault("active_first_bet_time", {})
    # Экономика компаний
    d.setdefault("companies", {})
    d.setdefault("next_company_id", 1)
    d.setdefault("game_owner", {})      # game_key -> company_id
    d.setdefault("game_auctions", {})   # auction_id -> {game, price, status}
    d.setdefault("next_auction_id", 1)
    d.setdefault("company_price", COMPANY_PRICE_DEFAULT)   # цену можно менять из бота
    d.setdefault("company_withdrawals", {})  # wid -> {company_id, user_id, amount, status}
    d.setdefault("next_withdrawal_id", 1)
    d.setdefault("checks", {})  # code_lower -> {amount, used, used_by, display_code, created_by}
    d.setdefault("custom_emojis", {})  # slot_key -> custom_emoji document_id (str)
    # Система персонала (рангов) и банов
    d.setdefault("admins", {})     # str(user_id) -> {"rank": int, "name": str, "added_by": int}
    d.setdefault("banned", {})     # str(user_id) -> {"by": int, "reason": str, "at": iso}
    d.setdefault("daily_gifts", {})  # str(admin_id) -> {"date": "YYYY-MM-DD", "amount": int}
    # Шанс проигрыша по каждой игре (0-100, 50 = честная игра)
    d.setdefault("game_chance", {})
    # Звёздные покупки GROM
    d.setdefault("star_purchases", {})  # payload -> {"user_id", "stars", "grom", "at"}
    return d


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
    else:
        d = {}
    return apply_defaults(d)


def get_profile(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in data["profiles"]:
        data["profiles"][uid] = {
            "house": None,
            "clan_id": None,
            "duel_wins": 0,
            "duel_losses": 0,
            "duel_draws": 0,
            "lang": "ru",
        }
    return data["profiles"][uid]


def record_duel(winner_id: int, loser_id: int, draw: bool = False):
    if draw:
        get_profile(winner_id)["duel_draws"] += 1
        get_profile(loser_id)["duel_draws"] += 1
    else:
        get_profile(winner_id)["duel_wins"] += 1
        get_profile(loser_id)["duel_losses"] += 1
    save_data(data)


def get_user_rank(user_id: int) -> int:
    uid = str(user_id)
    sorted_users = sorted(data["users"].items(), key=lambda x: x[1], reverse=True)
    for i, (u, _) in enumerate(sorted_users, 1):
        if u == uid:
            return i
    return len(data["users"]) + 1


HOUSES = [
    ("🦁", "Гриффиндор"),
    ("🐍", "Слизерин"),
    ("🦅", "Когтевран"),
    ("🦡", "Пуффендуй"),
]

# Игры, для которых можно настраивать шанс и которые могут принадлежать компаниям
GAME_KEYS = ["рулетка", "мины", "блэкджек", "краш", "башня", "монетка", "hilo", "квак"]
CHANCE_ADJUSTABLE_GAMES = [g for g in GAME_KEYS if g != "краш"]  # у краша свой шанс не настраивается
GAME_LABELS = {
    "рулетка": "🎰 Рулетка",
    "мины": "💣 Мины",
    "блэкджек": "🃏 Блэкджек",
    "краш": "🚀 Краш",
    "башня": "🏗 Башня",
    "монетка": "🪙 Монетка",
    "hilo": "🎴 HiLo",
    "квак": "🐸 Квак",
}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


def norm(text: str) -> str:
    """Приводит текст к единому Unicode-виду (NFC), чтобы 'ё'/'й',
    набранные разными клавиатурами (как отдельный символ или буква+ударение),
    всегда совпадали при сравнении команд."""
    return unicodedata.normalize("NFC", text)


def fmt(n: int) -> str:
    """Форматирует число с пробелами: 1 000 / 10 000 / 100 000"""
    return f"{int(n):,}".replace(",", "\u00a0")


data = load_data()

waiting_for_gif = False
waiting_for_chance_game: dict = {}  # admin_id -> game_key, ожидаем ввод числа шанса
waiting_for_company_name: dict = {}  # user_id -> True, ожидаем ввод названия компании


def _restore_bets(raw: dict) -> dict:
    result = {}
    for k, entries in raw.items():
        result[int(k)] = [tuple(e) for e in entries]
    return result


roulette_bets: dict = _restore_bets(data.get("active_roulette_bets", {}))
first_bet_time: dict = {
    int(k): datetime.fromisoformat(v)
    for k, v in data.get("active_first_bet_time", {}).items()
}
roulette_spinning: dict = {}
last_round_bets: dict = {}

roulette_history: dict = {int(k): v for k, v in data.get("roulette_history", {}).items()}


def save_roulette_state():
    data["active_roulette_bets"] = {
        str(cid): [list(b) for b in bets]
        for cid, bets in roulette_bets.items()
    }
    data["active_first_bet_time"] = {
        str(cid): t.isoformat()
        for cid, t in first_bet_time.items()
    }
    save_data(data)


duel_challenges: dict = {}
_chat_locks: dict = {}
admin_flow_state: dict = {}  # admin_id -> {"action": "give"/"take"/"auction", "step": ..., ...}


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ========== СИСТЕМА РАНГОВ ПЕРСОНАЛА ==========
# ========== КАСТОМНЫЕ ПРЕМИУМ-ЭМОДЗИ ==========
class CustomMarkdown:
    """parse_mode для markdown с поддержкой премиум-эмодзи через ссылки [emoji](emoji/DOCUMENT_ID).
    Работает во всех версиях Telethon (в отличие от <tg-emoji> в HTML, который поддерживают только новые)."""

    @staticmethod
    def parse(text):
        text, entities = tl_markdown.parse(text)
        for i, e in enumerate(entities):
            if isinstance(e, MessageEntityTextUrl) and e.url.startswith("emoji/"):
                entities[i] = MessageEntityCustomEmoji(e.offset, e.length, int(e.url.split("/")[1]))
        return text, entities

    @staticmethod
    def unparse(text, entities):
        entities = entities or []
        for i, e in enumerate(entities):
            if isinstance(e, MessageEntityCustomEmoji):
                entities[i] = MessageEntityTextUrl(e.offset, e.length, f"emoji/{e.document_id}")
        return tl_markdown.unparse(text, entities)


custom_md = CustomMarkdown()

EMOJI_SLOTS = {
    "welcome": ("👋", "Приветствие (/start)"),
    "profile": ("👤", "Профиль"),
    "balance": ("💰", "Баланс"),
    "id": ("🆔", "ID"),
    "companies": ("🏢", "Компании"),
}


def emoji_tag(slot: str, fallback: str) -> str:
    """Возвращает markdown-ссылку [emoji](emoji/ID) для CustomMarkdown, если задан кастомный эмодзи,
    иначе обычный эмодзи. Используется вместе с parse_mode=custom_md."""
    eid = data["custom_emojis"].get(slot)
    if eid:
        return f"[{fallback}](emoji/{eid})"
    return fallback


def extract_custom_emoji_id(event) -> Optional[str]:
    """Ищет custom emoji entity в сообщении и возвращает document_id первой найденной."""
    entities = event.message.entities or []
    for ent in entities:
        if type(ent).__name__ == "MessageEntityCustomEmoji":
            return str(ent.document_id)
    return None


waiting_for_emoji_slot: dict = {}  # admin_id -> slot_key


def admin_rank(user_id: int) -> int:
    """100 — главный админ (ADMIN_IDS), 1-4 — назначенный ранг персонала, 0 — не персонал."""
    if user_id in ADMIN_IDS:
        return 100
    entry = data["admins"].get(str(user_id))
    return entry["rank"] if entry else 0


def is_staff(user_id: int) -> bool:
    return admin_rank(user_id) > 0


def rank_label(rank: int) -> str:
    if rank >= 100:
        return "Главный админ"
    return RANK_NAMES.get(rank, f"Ранг {rank}")


def can_ban(user_id: int) -> bool:
    return admin_rank(user_id) >= RANK_TRAINEE


def can_give(user_id: int) -> bool:
    return admin_rank(user_id) >= RANK_TRAINEE


def can_take(user_id: int) -> bool:
    return admin_rank(user_id) >= RANK_ADMIN


def can_full_panel(user_id: int) -> bool:
    """Доступ ко всем разделам админ-панели (кроме назначения других админов)."""
    return admin_rank(user_id) >= RANK_SENIOR


def can_manage_admins(user_id: int) -> bool:
    """Только главный админ и «правая рука» могут назначать/снимать персонал."""
    r = admin_rank(user_id)
    return r >= 100 or r >= RANK_RIGHT_HAND


def can_ban_target(actor_id: int, target_id: int) -> bool:
    """Нельзя банить главного админа, если у тебя не ранг 'правая рука' (или ты сам главный админ)."""
    if target_id not in ADMIN_IDS:
        return True
    actor_rank = admin_rank(actor_id)
    return actor_rank >= 100 or actor_rank >= RANK_RIGHT_HAND


async def notify_main_admins(text: str):
    for aid in ADMIN_IDS:
        try:
            await client.send_message(aid, text, parse_mode="html")
        except Exception:
            pass


def register_daily_gift(admin_id: int, amount: int):
    today = datetime.now().strftime("%Y-%m-%d")
    key = str(admin_id)
    entry = data["daily_gifts"].get(key)
    if not entry or entry.get("date") != today:
        entry = {"date": today, "amount": 0}
    entry["amount"] += amount
    data["daily_gifts"][key] = entry
    save_data(data)


def get_daily_gift_total(admin_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    entry = data["daily_gifts"].get(str(admin_id))
    if not entry or entry.get("date") != today:
        return 0
    return entry["amount"]


# ========== СИСТЕМА БАНОВ ==========
def is_banned(user_id: int) -> bool:
    return str(user_id) in data["banned"]


def ban_user(user_id: int, by_id: int, reason: str = ""):
    data["banned"][str(user_id)] = {
        "by": by_id,
        "reason": reason,
        "at": datetime.now().isoformat(),
    }
    save_data(data)


def unban_user(user_id: int) -> bool:
    if str(user_id) in data["banned"]:
        data["banned"].pop(str(user_id))
        save_data(data)
        return True
    return False


@client.on(events.NewMessage(func=lambda e: e.sender_id and is_banned(e.sender_id)))
async def block_banned_users(event):
    raise events.StopPropagation


@client.on(events.CallbackQuery(func=lambda e: e.sender_id and is_banned(e.sender_id)))
async def block_banned_users_cb(event):
    await event.answer("⛔ Ты забанен.", alert=True)
    raise events.StopPropagation


# ========== ШАНС ИГР (общий механизм для любой игры) ==========
def get_game_chance(game_key: str) -> int:
    return data["game_chance"].get(game_key, 50)


def set_game_chance(game_key: str, value: int):
    data["game_chance"][game_key] = value
    save_data(data)


def apply_chance(game_key: str, natural_win: bool) -> bool:
    """Применяет настроенный админом шанс проигрыша к естественному результату игры.
    chance < 50 -> игроки выигрывают чаще, chance > 50 -> чаще проигрывают, 50 = честно."""
    chance = get_game_chance(game_key)
    if chance == 50:
        return natural_win
    if chance < 50:
        force_win_chance = (50 - chance) * 2
        if not natural_win and random.randint(1, 100) <= force_win_chance:
            return True
    else:
        force_lose_chance = (chance - 50) * 2
        if natural_win and random.randint(1, 100) <= force_lose_chance:
            return False
    return natural_win


# ========== КОМПАНИИ (экономика владения играми) ==========
def get_company_price() -> int:
    return data.get("company_price", COMPANY_PRICE_DEFAULT)


def set_company_price(value: int):
    data["company_price"] = value
    save_data(data)


def get_company_by_owner(user_id: int):
    for cid, c in data["companies"].items():
        if c["owner_id"] == user_id:
            return cid, c
    return None, None


def get_company_owning_game(game_key: str):
    cid = data["game_owner"].get(game_key)
    if cid and cid in data["companies"]:
        return cid, data["companies"][cid]
    return None, None


def is_game_frozen(game_key: str):
    """Возвращает (заморожена_ли_игра, компания), если игрой владеет замороженная компания."""
    cid, comp = get_company_owning_game(game_key)
    if comp and comp.get("frozen"):
        return True, comp
    return False, None


def check_company_debt(cid: str, comp: dict):
    """Проверяет баланс компании и замораживает/размораживает её при пересечении порога долга."""
    balance = comp.get("balance", 0)
    if balance <= COMPANY_DEBT_LIMIT and not comp.get("frozen"):
        comp["frozen"] = True
        comp["frozen_until"] = (datetime.now() + timedelta(hours=COMPANY_FREEZE_HOURS)).isoformat()
        save_data(data)
        asyncio.create_task(notify_company_frozen(cid, comp))
    elif balance > COMPANY_DEBT_LIMIT and comp.get("frozen"):
        comp["frozen"] = False
        comp["frozen_until"] = None
        save_data(data)
        asyncio.create_task(notify_company_unfrozen(cid, comp))


async def notify_company_frozen(cid: str, comp: dict):
    try:
        await client.send_message(
            comp["owner_id"],
            f"⛔ <b>Компания «{comp['name']}» заморожена!</b>\n\n"
            f"Баланс компании: <b>{fmt(comp['balance'])} GROM</b> (долг {fmt(-comp['balance'])} GROM).\n"
            f"Игры компании не будут работать <b>{COMPANY_FREEZE_HOURS} часов</b>.\n\n"
            f"Пополни баланс компании командой <code>пополнить компанию [сумма]</code>, "
            f"иначе по истечении срока игры будут изъяты.",
            parse_mode="html",
        )
    except Exception:
        pass


async def notify_company_unfrozen(cid: str, comp: dict):
    try:
        await client.send_message(
            comp["owner_id"],
            f"✅ Компания «{comp['name']}» разморожена, долг погашен. Игры снова доступны.",
            parse_mode="html",
        )
    except Exception:
        pass


async def notify_company_repossessed(cid: str, comp: dict, games: list):
    labels = ", ".join(GAME_LABELS.get(g, g) for g in games)
    try:
        await client.send_message(
            comp["owner_id"],
            f"❌ Срок оплаты долга компании «{comp['name']}» истёк.\n"
            f"Игры изъяты: {labels}.",
            parse_mode="html",
        )
    except Exception:
        pass


async def company_debt_watcher():
    """Фоновая задача: раз в пару минут проверяет просроченные заморозки и изымает игры."""
    while True:
        try:
            now = datetime.now()
            for cid, comp in list(data["companies"].items()):
                if not comp.get("frozen"):
                    continue
                deadline_raw = comp.get("frozen_until")
                if not deadline_raw:
                    continue
                deadline = datetime.fromisoformat(deadline_raw)
                if now >= deadline and comp.get("balance", 0) <= COMPANY_DEBT_LIMIT:
                    owned_games = [g for g, owner_cid in list(data["game_owner"].items()) if owner_cid == cid]
                    for g in owned_games:
                        data["game_owner"].pop(g, None)
                    comp["frozen"] = False
                    comp["frozen_until"] = None
                    save_data(data)
                    if owned_games:
                        asyncio.create_task(notify_company_repossessed(cid, comp, owned_games))
        except Exception:
            pass
        await asyncio.sleep(120)


def apply_company_economy(game_key: str, won: bool, bet_amount: int, payout_amount: int = 0):
    """Если игра принадлежит компании: при проигрыше игрока компания получает 85% от его ставки,
    при выигрыше игрока — сумма выплаты списывается с баланса компании."""
    cid, comp = get_company_owning_game(game_key)
    if not comp:
        return
    if won:
        comp["balance"] -= payout_amount
    else:
        comp["balance"] += int(bet_amount * 0.85)
    save_data(data)
    check_company_debt(cid, comp)


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_user_balance(user_id: int) -> int:
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = 0
        save_data(data)
    return data["users"][uid]


def set_user_balance(user_id: int, amount: int):
    uid = str(user_id)
    data["users"][uid] = amount
    save_data(data)


def add_balance(user_id: int, amount: int):
    new_bal = max(0, get_user_balance(user_id) + amount)
    set_user_balance(user_id, new_bal)


def can_take_bonus(user_id: int) -> Tuple[bool, Optional[int]]:
    uid = str(user_id)
    if user_id in ADMIN_IDS:
        return True, None
    last = data["bonus_cooldown"].get(uid)
    if not last:
        return True, None
    last_time = datetime.fromisoformat(last)
    if datetime.now() >= last_time + timedelta(hours=24):
        return True, None
    remaining = int((last_time + timedelta(hours=24) - datetime.now()).total_seconds())
    return False, remaining


def set_bonus_taken(user_id: int):
    if user_id not in ADMIN_IDS:
        data["bonus_cooldown"][str(user_id)] = datetime.now().isoformat()
        save_data(data)


def mention(user_id: int, name: str) -> str:
    safe = (name or str(user_id)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def parse_bet_type(token: str) -> Optional[Tuple[str, any, float]]:
    t = token.lower().strip()
    if t in ["красное", "red", "к", "кр"]:
        return ("color", "red", 2.0)
    if t in ["черное", "чёрное", "black", "ч", "чр"]:
        return ("color", "black", 2.0)
    if t in ["odd", "нечет", "нечетное", "н", "нч", "одд"]:
        return ("parity", "odd", 2.0)
    if t in ["even", "чет", "четное", "чт", "евен"]:
        return ("parity", "even", 2.0)
    if t == "0":
        return ("number", 0, 35.0)
    if t.isdigit() and 0 <= int(t) <= 36:
        return ("number", int(t), 35.0)
    m = re.match(r"^(\d+)-(\d+)$", t)
    if m:
        low, high = int(m.group(1)), int(m.group(2))
        if 0 <= low <= high <= 36:
            count = high - low + 1
            return ("range", (low, high), 36.0 / count)
    return None


def parse_bet(text: str) -> Optional[Tuple[int, str, any, float]]:
    parts = text.lower().strip().split()
    if len(parts) < 2:
        return None
    try:
        amount = int(parts[0])
    except Exception:
        return None
    result = parse_bet_type(" ".join(parts[1:]))
    if result is None:
        return None
    bet_type, bet_value, multiplier = result
    return (amount, bet_type, bet_value, multiplier)


def parse_multi_bet(text: str) -> Optional[Tuple[int, list]]:
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    try:
        amount = int(parts[0])
    except Exception:
        return None
    if amount <= 0:
        return None

    bets = []
    for token in parts[1:]:
        result = parse_bet_type(token)
        if result is None:
            return None
        bets.append(result)

    if not bets:
        return None
    return (amount, bets)


def calculate_win(number: int, bet_type: str, bet_value, multiplier: float) -> bool:
    reds = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    if bet_type == "number":
        return number == bet_value
    elif bet_type == "color":
        if number == 0:
            return False
        if bet_value == "red":
            return number in reds
        else:
            return number not in reds and number != 0
    elif bet_type == "parity":
        if number == 0:
            return False
        if bet_value == "odd":
            return number % 2 == 1
        else:
            return number % 2 == 0
    elif bet_type == "range":
        low, high = bet_value
        return low <= number <= high
    return False


def get_color_symbol(number: int) -> str:
    if number == 0:
        return "🟢"
    reds = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    return "🔴" if number in reds else "⚫️"


def add_tournament_score(user_id: int, profit: int):
    for tour in data.get("tournaments", {}).values():
        if tour["status"] == "active" and user_id in tour["participants"]:
            scores = tour.setdefault("scores", {})
            scores[str(user_id)] = scores.get(str(user_id), 0) + profit
    save_data(data)


def bet_label_str(bt, bv) -> str:
    if bt == "color":
        return "RED" if bv == "red" else "BLACK"
    if bt == "parity":
        return "ODD" if bv == "odd" else "EVEN"
    if bt == "number":
        return str(bv)
    if bt == "range":
        return f"{bv[0]}-{bv[1]}"
    return str(bv)


async def get_display_name(user_id: int) -> str:
    """Аналог bot.get_chat(id).first_name из aiogram."""
    try:
        entity = await client.get_entity(user_id)
        return getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(user_id)
    except Exception:
        return str(user_id)


def pick_roulette_number(bets, chance: int) -> int:
    """Выбирает выпавшее число с учётом ставок и шанса:
    - шанс 50 — честный случайный выбор, без подкрутки;
    - шанс < 50 — чаще выпадает число, покрытое НАИБОЛЬШЕЙ суммой ставок (в пользу игроков);
    - шанс > 50 — чаще выпадает число, покрытое НАИМЕНЬШЕЙ суммой ставок (в пользу казино).
    Сила подкрутки растёт линейно по мере отдаления шанса от 50.
    """
    if not bets:
        return random.randint(0, 36)

    stake_per_number = [0] * 37
    for _user_id, amount, bet_type, bet_value, multiplier in bets:
        for n in range(37):
            if calculate_win(n, bet_type, bet_value, multiplier):
                stake_per_number[n] += amount

    bias = abs(chance - 50) / 50  # 0..1, сила подкрутки
    if random.random() < bias:
        if chance < 50:
            target = max(stake_per_number)
        else:
            target = min(stake_per_number)
        candidates = [n for n, s in enumerate(stake_per_number) if s == target]
        return random.choice(candidates)

    return random.randint(0, 36)


async def process_roulette(chat_id: int):
    bets = roulette_bets.get(chat_id, [])
    if not bets:
        await client.send_message(chat_id, "Нет ставок для розыгрыша.")
        return

    has_gif = os.path.exists(GIF_MEDIA_PATH)

    if has_gif:
        gif_msg = await client.send_file(chat_id, GIF_MEDIA_PATH)
        await asyncio.sleep(3)
        await client.delete_messages(chat_id, gif_msg)
        await asyncio.sleep(0.5)
    else:
        await client.send_message(chat_id, "🎰 Вращаем рулетку...")
        await asyncio.sleep(2)

    result_num = pick_roulette_number(bets, get_game_chance("рулетка"))
    color_symbol = get_color_symbol(result_num)

    if chat_id not in roulette_history:
        roulette_history[chat_id] = []
    roulette_history[chat_id].append((result_num, color_symbol))
    if len(roulette_history[chat_id]) > 9:
        roulette_history[chat_id].pop(0)
    data["roulette_history"][str(chat_id)] = roulette_history[chat_id]
    save_data(data)

    per_user: dict = {}
    for user_id, amount, bet_type, bet_value, multiplier in bets:
        per_user.setdefault(user_id, []).append(
            (amount, bet_type, bet_value, multiplier)
        )
    for uid, entries in per_user.items():
        last_round_bets[(chat_id, uid)] = entries

    bet_results = []
    for user_id, amount, bet_type, bet_value, multiplier in bets:
        win = calculate_win(result_num, bet_type, bet_value, multiplier)
        if win:
            win_amount = int(amount * multiplier)
            add_balance(user_id, win_amount)
            apply_company_economy("рулетка", True, amount, win_amount)
            profit = win_amount - amount
            bet_results.append((user_id, amount, bet_type, bet_value, True, profit))
            add_tournament_score(user_id, profit)
        else:
            apply_company_economy("рулетка", False, amount)
            bet_results.append((user_id, amount, bet_type, bet_value, False, -amount))

    names: dict = {}
    for user_id, *_ in bet_results:
        if user_id not in names:
            names[user_id] = await get_display_name(user_id)

    await send_roulette_result(
        chat_id, result_num, color_symbol, bet_results, names, per_user
    )

    roulette_bets.pop(chat_id, None)
    first_bet_time.pop(chat_id, None)
    roulette_spinning.pop(chat_id, None)
    save_roulette_state()


# ========== КЛАВИАТУРЫ ==========
def main_keyboard():
    return [
        [Button.text("👤 Профиль", resize=True), Button.text("📋 Команды", resize=True)],
        [Button.text("🛒 Донат", resize=True), Button.text("💬 Чаты", resize=True)],
        [Button.text("🎮 Игры", resize=True), Button.text("🎁 Бонус", resize=True)],
        [Button.text("🏢 Компания", resize=True)],
    ]


def is_cmd(text: Optional[str], name: str) -> bool:
    """Аналог aiogram Command(name): матчит /name и /name@botusername в начале строки."""
    if not text:
        return False
    first = text.split()[0] if text.split() else ""
    first = first.split("@")[0]
    return first.lower() == f"/{name}"


# ========== ОБРАБОТЧИКИ ==========
@client.on(events.NewMessage(func=lambda e: is_cmd(e.raw_text, "start")))
async def cmd_start(event):
    user_id = event.sender_id
    parts = event.raw_text.strip().split(maxsplit=1)
    deep_param = parts[1].strip() if len(parts) > 1 else ""

    if not await is_subscribed(user_id):
        await event.respond(
            "🔒 <b>Для использования бота нужно подписаться на наши каналы:</b>",
            buttons=subscribe_buttons(),
            parse_mode="html",
        )
        return

    me = await client.get_me()
    await event.respond(
        f"{emoji_tag('welcome', '👋')} Добро пожаловать!\n\n"
        "GROM — развлекательный бот для вашего чата:\n"
        "• ⚔️ Создание своего клана\n"
        "• 🏢 Своя компания и рынок игр\n"
        "• 🎮 Мини-игры\n"
        "• 🤺 Дуэли\n\n"
        "Запуская бота, вы соглашаетесь с условиями использования.",
        buttons=[[Button.url("➕ Добавить бота в чат", f"https://t.me/{me.username}?startgroup=start")]],
        parse_mode=custom_md,
    )
    await event.respond("Выберите раздел:", buttons=main_keyboard())

    if deep_param == "bonus":
        can, remaining = can_take_bonus(user_id)
        if can:
            add_balance(user_id, 1000)
            set_bonus_taken(user_id)
            await event.respond(f"🎁 Вам начислено: 1 000 GROM\n💰 Новый баланс: {fmt(get_user_balance(user_id))}")
        else:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await event.respond(f"⏳ Осталось подождать {hours:02d}:{minutes:02d} до следующего бонуса")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "check_subscription"))
async def check_subscription_callback(event):
    if await is_subscribed(event.sender_id):
        await event.answer("✅ Подписка подтверждена!", alert=True)
        try:
            await event.delete()
        except Exception:
            pass
        me = await client.get_me()
        await client.send_message(
            event.sender_id,
            f"{emoji_tag('welcome', '👋')} Добро пожаловать!\n\n"
            "GROM — развлекательный бот для вашего чата:\n"
            "• ⚔️ Создание своего клана\n"
            "• 🏢 Своя компания и рынок игр\n"
            "• 🎮 Мини-игры\n"
            "• 🤺 Дуэли\n\n"
            "Запуская бота, вы соглашаетесь с условиями использования.",
            buttons=[[Button.url("➕ Добавить бота в чат", f"https://t.me/{me.username}?startgroup=start")]],
            parse_mode=custom_md,
        )
        await client.send_message(event.sender_id, "Выберите раздел:", buttons=main_keyboard())
    else:
        await event.answer("❌ Не вижу подписки на все каналы. Подпишись и нажми ещё раз.", alert=True)


@client.on(events.NewMessage(func=lambda e: is_cmd(e.raw_text, "adm")))
async def cmd_adm(event):
    user_id = event.sender_id
    if not is_staff(user_id):
        return
    if not event.is_private:
        await event.reply("Команда доступна только в личных сообщениях с ботом.")
        return
    admin_flow_state.pop(user_id, None)

    rank = admin_rank(user_id)
    keyboard = [
        [Button.inline("💰 Выдать GROM", data="give_gram")],
        [Button.inline("🔨 Забанить", data="ban_user_start"), Button.inline("🔓 Разбанить", data="unban_user_start")],
        [Button.inline("🎨 Изменить эмодзи", data="change_emoji_start")],
    ]
    if rank >= RANK_ADMIN:
        keyboard.append([Button.inline("💸 Забрать GROM", data="take_gram")])
    if rank >= RANK_SENIOR:
        keyboard += [
            [Button.inline("🎬 Загрузить гифку для рулетки", data="upload_gif")],
            [Button.inline("🧹 Обнулить баланс юзера", data="reset_user")],
            [Button.inline("💣 Обнулить ВСЕ балансы", data="reset_all")],
            [Button.inline("🎲 Шанс игр", data="chance_game")],
            [Button.inline("📤 Аукцион игры", data="auction_game")],
            [Button.inline("🏢 Компании (стата)", data="companies_stat")],
            [Button.inline("💵 Цена компании", data="company_price")],
            [Button.inline("📨 Заявки на вывод", data="withdrawals_list")],
            [Button.inline("0️⃣ Обнулить баланс компании", data="reset_company")],
            [Button.inline("🗑 Удалить компанию", data="delete_company")],
            [Button.inline("🎮 Забрать игру у компании", data="revoke_game")],
            [Button.inline("🧾 Создать чек", data="create_check"), Button.inline("📋 Список чеков", data="list_checks")],
            [Button.inline("👥 Все участники", data="view_users:0")],
        ]
    if can_manage_admins(user_id):
        keyboard += [
            [Button.inline("➕ Добавить админа", data="add_admin_start"), Button.inline("➖ Убрать админа", data="remove_admin_start")],
            [Button.inline("📋 Список админов", data="list_admins")],
            [Button.inline("🗄 ВОЗВРАТ ДАННЫХ", data="restore_data_start")],
        ]

    await event.respond(
        f"🔧 <b>Админ-панель</b>\nТвой ранг: <b>{rank_label(rank)}</b>",
        buttons=keyboard,
        parse_mode="html",
    )


ACTION_MIN_RANK = {
    "give_gram": RANK_TRAINEE,
    "take_gram": RANK_ADMIN,
    "upload_gif": RANK_SENIOR,
    "chance_game": RANK_SENIOR,
    "auction_game": RANK_SENIOR,
    "companies_stat": RANK_SENIOR,
    "company_price": RANK_SENIOR,
    "withdrawals_list": RANK_SENIOR,
    "reset_user": RANK_SENIOR,
    "reset_all": RANK_SENIOR,
    "reset_company": RANK_SENIOR,
    "delete_company": RANK_SENIOR,
    "revoke_game": RANK_SENIOR,
    "create_check": RANK_SENIOR,
    "list_checks": RANK_SENIOR,
}


@client.on(events.CallbackQuery(
    func=lambda e: e.data and e.data.decode() in
    ["upload_gif", "give_gram", "take_gram",
     "chance_game", "auction_game", "companies_stat", "company_price", "withdrawals_list",
     "reset_user", "reset_all", "reset_company", "delete_company", "revoke_game",
     "create_check", "list_checks"]
))
async def admin_callbacks(event):
    action = event.data.decode()
    if admin_rank(event.sender_id) < ACTION_MIN_RANK.get(action, 100):
        await event.answer("Доступ запрещён — не хватает ранга.", alert=True)
        return

    global waiting_for_gif

    if action == "upload_gif":
        waiting_for_gif = True
        await event.respond("Отправьте гифку (одну, она сохранится навсегда)")
        await event.answer()
    elif action == "give_gram":
        admin_flow_state[event.sender_id] = {"action": "give", "step": "id"}
        await event.respond(
            "💰 <b>Выдача GROM</b>\n\nВведите ID пользователя (или @username):",
            parse_mode="html",
        )
        await event.answer()
    elif action == "take_gram":
        admin_flow_state[event.sender_id] = {"action": "take", "step": "id"}
        await event.respond(
            "💸 <b>Списание GROM</b>\n\nВведите ID пользователя (или @username):",
            parse_mode="html",
        )
        await event.answer()
    elif action == "chance_game":
        buttons = [[Button.inline(GAME_LABELS[g], data=f"chance_pick:{g}")] for g in CHANCE_ADJUSTABLE_GAMES]
        await event.respond("Выберите игру, чтобы изменить шанс проигрыша игроков:", buttons=buttons)
        await event.answer()
    elif action == "auction_game":
        buttons = [[Button.inline(GAME_LABELS[g], data=f"auc_pick:{g}")] for g in GAME_KEYS]
        await event.respond("Выберите игру, которую выставить на аукцион компаний:", buttons=buttons)
        await event.answer()
    elif action == "companies_stat":
        if not data["companies"]:
            await event.respond("Компаний пока нет.")
        else:
            lines = ["🏢 <b>Компании</b>\n"]
            for cid, c in data["companies"].items():
                owned = [GAME_LABELS.get(g, g) for g, ccid in data["game_owner"].items() if ccid == cid]
                owner_name = await get_display_name(c["owner_id"])
                frozen_note = " ⛔ ЗАМОРОЖЕНА" if c.get("frozen") else ""
                lines.append(
                    f"🏢 <b>{c['name']}</b> (#{cid}){frozen_note}\n"
                    f"Владелец: {mention(c['owner_id'], owner_name)}\n"
                    f"Баланс: <b>{fmt(c['balance'])} GROM</b>\n"
                    f"Игры: {', '.join(owned) if owned else 'нет'}"
                )
            await event.respond("\n\n".join(lines), parse_mode="html")
        await event.answer()
    elif action == "reset_user":
        admin_flow_state[event.sender_id] = {"action": "reset_user", "step": "id"}
        await event.respond(
            "🧹 <b>Обнуление баланса игрока</b>\n\n"
            "Введите ID пользователя (или @username). "
            "Обнулится его личный баланс и (если есть) баланс его компании.",
            parse_mode="html",
        )
        await event.answer()
    elif action == "reset_all":
        await event.respond(
            "💣 <b>Обнулить ВСЕ балансы?</b>\n\n"
            "Это обнулит балансы <b>всех игроков</b> и <b>всех компаний</b> без возможности отмены.",
            buttons=[[
                Button.inline("✅ Да, обнулить всё", data="reset_all_confirm"),
                Button.inline("❌ Отмена", data="reset_all_cancel"),
            ]],
            parse_mode="html",
        )
        await event.answer()
    elif action == "reset_company":
        if not data["companies"]:
            await event.respond("Компаний пока нет.")
        else:
            buttons = [
                [Button.inline(f"{c['name']} ({fmt(c['balance'])} GROM)", data=f"resetcomp_pick:{cid}")]
                for cid, c in data["companies"].items()
            ]
            await event.respond("Выберите компанию, чей баланс обнулить:", buttons=buttons)
        await event.answer()
    elif action == "delete_company":
        if not data["companies"]:
            await event.respond("Компаний пока нет.")
        else:
            buttons = [
                [Button.inline(f"🗑 {c['name']}", data=f"delcomp_pick:{cid}")]
                for cid, c in data["companies"].items()
            ]
            await event.respond("Выберите компанию для удаления:", buttons=buttons)
        await event.answer()
    elif action == "revoke_game":
        owned = {g: cid for g, cid in data["game_owner"].items()}
        if not owned:
            await event.respond("Ни одна игра сейчас не принадлежит компании.")
        else:
            buttons = []
            for g, cid in owned.items():
                comp = data["companies"].get(cid, {})
                buttons.append([Button.inline(
                    f"{GAME_LABELS.get(g, g)} — {comp.get('name', '?')}", data=f"revoke_pick:{g}"
                )])
            await event.respond("Выберите игру, которую забрать у компании:", buttons=buttons)
        await event.answer()
    elif action == "create_check":
        admin_flow_state[event.sender_id] = {"action": "create_check", "step": "amount"}
        await event.respond(
            "🧾 <b>Создание чека</b>\n\nВведите сумму GROM для чека:",
            parse_mode="html",
        )
        await event.answer()
    elif action == "list_checks":
        active = {code: c for code, c in data["checks"].items() if c["activations_left"] > 0}
        if not active:
            await event.respond("Активных чеков нет.")
        else:
            buttons = [
                [Button.inline(
                    f"🗑 {c['display_code']} — {fmt(c['amount'])} GROM ({c['activations_left']}/{c['activations_total']})",
                    data=f"delcheck:{code}",
                )]
                for code, c in active.items()
            ]
            await event.respond("📋 <b>Активные чеки</b> (нажми, чтобы удалить):", buttons=buttons, parse_mode="html")
        await event.answer()
    elif action == "company_price":
        admin_flow_state[event.sender_id] = {"action": "company_price"}
        await event.respond(
            f"💵 Текущая цена компании: <b>{fmt(get_company_price())} GROM</b>\n\nВведите новую цену:",
            parse_mode="html",
        )
        await event.answer()
    elif action == "withdrawals_list":
        pending = {wid: w for wid, w in data["company_withdrawals"].items() if w["status"] == "pending"}
        if not pending:
            await event.respond("Нет заявок на вывод.")
        else:
            buttons = []
            for wid, w in pending.items():
                comp = data["companies"].get(w["company_id"], {})
                buttons.append([
                    Button.inline(f"✅ #{wid} {fmt(w['amount'])} GROM", data=f"wd_ok:{wid}"),
                    Button.inline("❌", data=f"wd_no:{wid}"),
                ])
            lines = ["📨 <b>Заявки на вывод</b>\n"]
            for wid, w in pending.items():
                comp = data["companies"].get(w["company_id"], {})
                owner_name = await get_display_name(w["user_id"])
                lines.append(
                    f"#{wid}: {comp.get('name', '?')} — {mention(w['user_id'], owner_name)} — {fmt(w['amount'])} GROM"
                )
            await event.respond("\n".join(lines), buttons=buttons, parse_mode="html")
        await event.answer()


# ========== БАН / РАЗБАН ==========
@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "ban_user_start"))
async def ban_user_start(event):
    if not can_ban(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    admin_flow_state[event.sender_id] = {"action": "ban", "step": "id"}
    await event.respond("🔨 <b>Бан</b>\n\nВведите ID пользователя (или @username):", parse_mode="html")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "unban_user_start"))
async def unban_user_start(event):
    if not can_ban(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    admin_flow_state[event.sender_id] = {"action": "unban", "step": "id"}
    await event.respond("🔓 <b>Разбан</b>\n\nВведите ID пользователя (или @username):", parse_mode="html")
    await event.answer()


@client.on(events.NewMessage(func=lambda e: e.raw_text and can_ban(e.sender_id) and re.match(r"^забанить\s+\S+", e.raw_text.lower().strip())))
async def ban_user_cmd(event):
    parts = event.raw_text.strip().split(maxsplit=2)
    target_id, target_name = await resolve_target(parts[1])
    if target_id is None:
        await event.reply("❌ Пользователь не найден.")
        return
    if not can_ban_target(event.sender_id, target_id):
        await event.reply("❌ Тебе нельзя банить главного админа.")
        return
    reason = parts[2] if len(parts) > 2 else ""
    ban_user(target_id, event.sender_id, reason)

    actor_rank = admin_rank(event.sender_id)
    actor_name = await get_display_name(event.sender_id)
    await event.reply(f"🔨 Пользователь {mention(target_id, target_name)} забанен.", parse_mode="html")
    await notify_main_admins(
        f"ранг [{actor_rank if actor_rank < 100 else 'Главный'}] {mention(event.sender_id, actor_name)} "
        f"забанил {mention(target_id, target_name)}" + (f"\nПричина: {reason}" if reason else "")
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and can_ban(e.sender_id) and re.match(r"^разбанить\s+\S+", e.raw_text.lower().strip())))
async def unban_user_cmd(event):
    parts = event.raw_text.strip().split(maxsplit=1)
    target_id, target_name = await resolve_target(parts[1])
    if target_id is None:
        await event.reply("❌ Пользователь не найден.")
        return
    if unban_user(target_id):
        await event.reply(f"🔓 Пользователь {mention(target_id, target_name)} разбанен.", parse_mode="html")
    else:
        await event.reply("❌ Этот пользователь не был забанен.")


# ========== ИЗМЕНЕНИЕ КАСТОМНЫХ ЭМОДЗИ ==========
@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "change_emoji_start"))
async def change_emoji_start(event):
    if not is_staff(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    buttons = [
        [Button.inline(f"{fallback} {label}", data=f"pick_emoji_slot:{slot}")]
        for slot, (fallback, label) in EMOJI_SLOTS.items()
    ]
    await event.respond("🎨 Какой эмодзи хочешь заменить?", buttons=buttons)
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("pick_emoji_slot:")))
async def pick_emoji_slot(event):
    if not is_staff(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    slot = event.data.decode().split(":", 1)[1]
    waiting_for_emoji_slot[event.sender_id] = slot
    fallback, label = EMOJI_SLOTS[slot]
    await event.respond(
        f"✏️ Пришли сообщение с премиум-эмодзи, который хочешь поставить вместо «{fallback} {label}» "
        f"(просто отправь эмодзи или перешли сообщение с ним).",
    )
    await event.answer()


@client.on(events.NewMessage(func=lambda e: e.sender_id in waiting_for_emoji_slot))
async def set_emoji_slot(event):
    slot = waiting_for_emoji_slot.get(event.sender_id)
    if not slot:
        return
    eid = extract_custom_emoji_id(event)
    if not eid:
        await event.reply("❗ Не нашёл премиум-эмодзи в этом сообщении. Пришли эмодзи ещё раз (или /adm для отмены).")
        return
    waiting_for_emoji_slot.pop(event.sender_id, None)
    data["custom_emojis"][slot] = eid
    save_data(data)
    fallback, label = EMOJI_SLOTS[slot]
    await event.reply(
        f"✅ Эмодзи для «{label}» обновлён: {emoji_tag(slot, fallback)}",
        parse_mode=custom_md,
    )


# ========== УПРАВЛЕНИЕ ПЕРСОНАЛОМ ==========
@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "add_admin_start"))
async def add_admin_start(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    admin_flow_state[event.sender_id] = {"action": "add_admin", "step": "id"}
    await event.respond("➕ <b>Новый админ</b>\n\nВведите ID пользователя (или @username):", parse_mode="html")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("pick_rank:")))
async def pick_rank_callback(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    state = admin_flow_state.get(event.sender_id)
    if not state or state.get("action") != "add_admin" or state.get("step") != "rank":
        await event.answer("Сессия истекла, начните заново через /adm.", alert=True)
        return
    rank = int(event.data.decode().split(":", 1)[1])
    target_id = state["target_id"]
    target_name = state["target_name"]
    data["admins"][str(target_id)] = {"rank": rank, "name": target_name, "added_by": event.sender_id}
    save_data(data)
    admin_flow_state.pop(event.sender_id, None)
    await event.edit(f"✅ {mention(target_id, target_name)} назначен на ранг «{rank_label(rank)}».", parse_mode="html")
    await event.answer()
    try:
        await client.send_message(target_id, f"🎖 Тебе выдан ранг персонала: <b>{rank_label(rank)}</b>.\nОткрой /adm.", parse_mode="html")
    except Exception:
        pass


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "remove_admin_start"))
async def remove_admin_start(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    if not data["admins"]:
        await event.respond("Персонала пока нет.")
        await event.answer()
        return
    buttons = [
        [Button.inline(f"➖ {e['name']} ({rank_label(e['rank'])})", data=f"rm_admin:{uid}")]
        for uid, e in data["admins"].items()
    ]
    await event.respond("Кого снять с должности?", buttons=buttons)
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("rm_admin:")))
async def remove_admin_pick(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    uid = event.data.decode().split(":", 1)[1]
    entry = data["admins"].pop(uid, None)
    save_data(data)
    if not entry:
        await event.answer("Уже снят.", alert=True)
        return
    await event.edit(f"✅ {entry['name']} снят с должности «{rank_label(entry['rank'])}».")
    await event.answer()
    try:
        await client.send_message(int(uid), "❌ Ты снят с должности персонала администратором.")
    except Exception:
        pass


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "list_admins"))
async def list_admins(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    if not data["admins"]:
        await event.respond("Персонала пока нет.")
    else:
        lines = ["👮 <b>Персонал</b>\n"]
        for uid, e in data["admins"].items():
            lines.append(f"• {mention(int(uid), e['name'])} — {rank_label(e['rank'])}")
        await event.respond("\n".join(lines), parse_mode="html")
    await event.answer()


# ========== ВОЗВРАТ ДАННЫХ ==========
waiting_for_data_restore: dict = {}  # admin_id -> True


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "restore_data_start"))
async def restore_data_start(event):
    if not can_manage_admins(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    waiting_for_data_restore[event.sender_id] = True
    await event.respond(
        "🗄 <b>Возврат данных</b>\n\n"
        "Пришли файл <code>data.json</code> следующим сообщением как документ.\n"
        "⚠️ Это полностью заменит текущие данные бота (балансы, компании, кланы и т.д.). "
        "Текущие данные будут сохранены в резервную копию на всякий случай.",
        parse_mode="html",
    )
    await event.answer()


@client.on(events.NewMessage(func=lambda e: e.document and waiting_for_data_restore.get(e.sender_id)))
async def restore_data_file(event):
    admin_id = event.sender_id
    waiting_for_data_restore.pop(admin_id, None)

    try:
        raw_path = os.path.join(BASE_DIR, f"_incoming_restore_{admin_id}.json")
        await event.download_media(file=raw_path)
        with open(raw_path, "r", encoding="utf-8") as f:
            new_data = json.load(f)
        os.remove(raw_path)
    except Exception as ex:
        await event.reply(f"❌ Не удалось прочитать файл как JSON: {ex}")
        return

    if not isinstance(new_data, dict) or "users" not in new_data:
        await event.reply("❌ Файл не похож на data.json этого бота (нет ключа «users»).")
        return

    backup_path = os.path.join(BASE_DIR, f"data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f_old, open(backup_path, "w", encoding="utf-8") as f_bak:
            f_bak.write(f_old.read())
    except Exception:
        pass

    new_data = apply_defaults(new_data)
    data.clear()
    data.update(new_data)
    save_data(data)

    await event.reply(
        f"✅ Данные восстановлены из присланного файла.\n"
        f"💾 Резервная копия прежних данных: <code>{os.path.basename(backup_path)}</code>",
        parse_mode="html",
    )


# ========== СПИСОК УЧАСТНИКОВ ==========
USERS_PAGE_SIZE = 20


async def render_users_page(offset: int) -> Tuple[str, list]:
    all_users = sorted(data["users"].items(), key=lambda kv: kv[1], reverse=True)
    total = len(all_users)
    page = all_users[offset:offset + USERS_PAGE_SIZE]

    lines = [f"👥 <b>Участники</b> ({total} всего)\n"]
    for uid, balance in page:
        name = await get_display_name(int(uid))
        lines.append(f"• {mention(int(uid), name)} — {fmt(balance)} GROM")

    nav_row = []
    if offset > 0:
        nav_row.append(Button.inline("⬅️ Назад", data=f"view_users:{max(0, offset - USERS_PAGE_SIZE)}"))
    if offset + USERS_PAGE_SIZE < total:
        nav_row.append(Button.inline("➡️ Далее", data=f"view_users:{offset + USERS_PAGE_SIZE}"))
    buttons = [nav_row] if nav_row else []
    buttons.append([Button.inline("🔄 Обновить", data=f"view_users:{offset}")])
    return "\n".join(lines), buttons


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("view_users:")))
async def view_users(event):
    if not can_full_panel(event.sender_id):
        await event.answer("Доступ запрещён", alert=True)
        return
    offset = int(event.data.decode().split(":", 1)[1])
    text, buttons = await render_users_page(offset)
    try:
        await event.edit(text, buttons=buttons, parse_mode="html")
    except Exception:
        await event.respond(text, buttons=buttons, parse_mode="html")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("chance_pick:")))
async def chance_pick(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    game_key = event.data.decode().split(":", 1)[1]
    if game_key not in CHANCE_ADJUSTABLE_GAMES:
        await event.answer("У этой игры нет настраиваемого шанса.", alert=True)
        return
    waiting_for_chance_game[event.sender_id] = game_key
    cur = get_game_chance(game_key)
    await event.respond(
        f"Игра: <b>{GAME_LABELS.get(game_key, game_key)}</b>\n"
        f"Текущий шанс проигрыша: <b>{cur}%</b>\n\n"
        "Введите число от 0 до 100:\n"
        "• 0 — игроки всегда выигрывают\n"
        "• 50 — стандартная честная игра\n"
        "• 100 — игроки всегда проигрывают",
        parse_mode="html",
    )
    await event.answer()


@client.on(events.NewMessage(func=lambda e: (
    e.raw_text and admin_rank(e.sender_id) >= RANK_SENIOR and e.sender_id in waiting_for_chance_game
)))
async def set_game_chance_handler(event):
    game_key = waiting_for_chance_game.get(event.sender_id)
    if not game_key:
        return
    try:
        val = int(event.raw_text.strip())
    except ValueError:
        await event.reply("Введите целое число от 0 до 100.")
        return
    if not 0 <= val <= 100:
        await event.reply("Число должно быть от 0 до 100.")
        return
    set_game_chance(game_key, val)
    waiting_for_chance_game.pop(event.sender_id, None)
    await event.reply(
        f"✅ Шанс проигрыша для «{GAME_LABELS.get(game_key, game_key)}» установлен: <b>{val}%</b>",
        parse_mode="html",
    )


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("auc_pick:")))
async def auction_pick_game(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    game_key = event.data.decode().split(":", 1)[1]
    admin_flow_state[event.sender_id] = {"action": "auction", "step": "price", "game": game_key}
    await event.respond(
        f"Введите цену аукциона для игры «{GAME_LABELS.get(game_key, game_key)}» (в GROM):",
        parse_mode="html",
    )
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() in ("reset_all_confirm", "reset_all_cancel")))
async def reset_all_decision(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    if event.data.decode() == "reset_all_cancel":
        await event.edit("Отменено.")
        await event.answer()
        return

    for uid in list(data["users"].keys()):
        data["users"][uid] = 0
    for cid, comp in data["companies"].items():
        comp["balance"] = 0
        comp["frozen"] = False
        comp["frozen_until"] = None
    save_data(data)
    clear_all_active_games()

    await event.edit("💣 Все балансы (игроков и компаний) обнулены.")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("resetcomp_pick:")))
async def reset_company_pick(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    cid = event.data.decode().split(":", 1)[1]
    comp = data["companies"].get(cid)
    if not comp:
        await event.answer("Компания не найдена.", alert=True)
        return
    comp["balance"] = 0
    comp["frozen"] = False
    comp["frozen_until"] = None
    save_data(data)
    await event.edit(f"✅ Баланс компании «{comp['name']}» обнулён.")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("delcomp_pick:")))
async def delete_company_pick(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    cid = event.data.decode().split(":", 1)[1]
    comp = data["companies"].get(cid)
    if not comp:
        await event.answer("Компания не найдена.", alert=True)
        return
    await event.edit(
        f"⚠️ Точно удалить компанию «{comp['name']}»? Все её игры станут свободными, история не восстановится.",
        buttons=[[
            Button.inline("✅ Да, удалить", data=f"delcomp_confirm:{cid}"),
            Button.inline("❌ Отмена", data="delcomp_cancel"),
        ]],
    )
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "delcomp_cancel"))
async def delete_company_cancel(event):
    await event.edit("Отменено.")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("delcomp_confirm:")))
async def delete_company_confirm(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    cid = event.data.decode().split(":", 1)[1]
    comp = data["companies"].pop(cid, None)
    if not comp:
        await event.answer("Компания уже удалена.", alert=True)
        return
    for g in [g for g, ccid in data["game_owner"].items() if ccid == cid]:
        data["game_owner"].pop(g, None)
    for w in data["company_withdrawals"].values():
        if w["company_id"] == cid and w["status"] == "pending":
            w["status"] = "cancelled"
    save_data(data)
    await event.edit(f"🗑 Компания «{comp['name']}» удалена, её игры освобождены.")
    await event.answer()
    try:
        await client.send_message(comp["owner_id"], f"❌ Твоя компания «{comp['name']}» была удалена администратором.")
    except Exception:
        pass


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("revoke_pick:")))
async def revoke_game_pick(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    game_key = event.data.decode().split(":", 1)[1]
    cid = data["game_owner"].pop(game_key, None)
    save_data(data)
    if not cid:
        await event.answer("Эта игра уже никому не принадлежит.", alert=True)
        return
    comp = data["companies"].get(cid)
    label = GAME_LABELS.get(game_key, game_key)
    await event.edit(f"✅ Игра «{label}» изъята у компании «{comp['name'] if comp else '?'}».")
    await event.answer()
    if comp:
        try:
            await client.send_message(comp["owner_id"], f"❌ Игра «{label}» изъята администратором у твоей компании «{comp['name']}».")
        except Exception:
            pass


@client.on(events.CallbackQuery(
    func=lambda e: e.data and (e.data.decode().startswith("rpt:") or e.data.decode().startswith("dbl:"))
))
async def repeat_double_callback(event):
    parts = event.data.decode().split(":")
    action = parts[0]
    chat_id = int(parts[1])
    user_id = event.sender_id

    saved = last_round_bets.get((chat_id, user_id))
    if not saved:
        await event.answer("Вы не играли в прошлом раунде", alert=True)
        return

    if roulette_spinning.get(chat_id):
        await event.answer("Раунд уже идёт — подожди.", alert=True)
        return

    multiplier_factor = 2 if action == "dbl" else 1
    new_bets = [
        (amount * multiplier_factor, bt, bv, mult) for amount, bt, bv, mult in saved
    ]
    total_cost = sum(a for a, *_ in new_bets)
    balance = get_user_balance(user_id)

    if total_cost > balance:
        await event.answer("Недостаточно GROM на балансе", alert=True)
        return

    add_balance(user_id, -total_cost)

    if chat_id not in roulette_bets:
        roulette_bets[chat_id] = []
    if chat_id not in first_bet_time:
        first_bet_time[chat_id] = datetime.now()

    for amount, bet_type, bet_value, mult in new_bets:
        roulette_bets[chat_id].append((user_id, amount, bet_type, bet_value, mult))
    save_roulette_state()

    sender = await event.get_sender()
    name = mention(user_id, sender.first_name if sender else str(user_id))
    lines = "\n".join(
        f"Ставка принята: {name} {fmt(a)} GROM на {bet_label_str(bt, bv)}"
        for a, bt, bv, _ in new_bets
    )
    await event.reply(lines, parse_mode="html")
    await event.answer()


async def resolve_target(target: str):
    """@username или числовой id -> (user_id, first_name) либо (None, None)."""
    try:
        if target.startswith("@"):
            entity = await client.get_entity(target)
        else:
            entity = await client.get_entity(int(target))
        return entity.id, (getattr(entity, "first_name", None) or str(entity.id))
    except Exception:
        return None, None


def clear_user_active_games(user_id: int):
    """Убирает все активные игры/ставки пользователя из памяти (используется при обнулении баланса)."""
    for chat_id in list(roulette_bets.keys()):
        remaining = [b for b in roulette_bets[chat_id] if b[0] != user_id]
        if remaining:
            roulette_bets[chat_id] = remaining
        else:
            roulette_bets.pop(chat_id, None)
            first_bet_time.pop(chat_id, None)
    save_roulette_state()
    mines_games.pop(user_id, None)
    blackjack_games.pop(user_id, None)
    crash_games.pop(user_id, None)
    tower_games.pop(user_id, None)
    kvak_games.pop(user_id, None)
    hilo_games.pop(user_id, None)


def clear_all_active_games():
    """Полная очистка всех активных игр (используется при полном обнулении всех балансов)."""
    roulette_bets.clear()
    first_bet_time.clear()
    save_roulette_state()
    mines_games.clear()
    blackjack_games.clear()
    crash_games.clear()
    tower_games.clear()
    kvak_games.clear()
    hilo_games.clear()


@client.on(events.NewMessage(func=lambda e: (
    e.is_private
    and e.sender_id in admin_flow_state
    and e.raw_text
    and not waiting_for_gif
    and e.sender_id not in waiting_for_chance_game
)))
async def admin_flow_handler(event):
    state = admin_flow_state.get(event.sender_id)
    if not state:
        return
    text = event.raw_text.strip()

    if state["action"] == "ban" and state["step"] == "id":
        target_id, target_name = await resolve_target(text)
        if target_id is None:
            await event.reply("❌ Пользователь не найден. Введите ID или @username ещё раз (или /adm для отмены).")
            return
        if not can_ban_target(event.sender_id, target_id):
            admin_flow_state.pop(event.sender_id, None)
            await event.reply("❌ Тебе нельзя банить главного админа.")
            return
        state["target_id"] = target_id
        state["target_name"] = target_name
        state["step"] = "reason"
        await event.reply("Причина бана? (или напиши «-», чтобы без причины)")
        return

    if state["action"] == "ban" and state["step"] == "reason":
        target_id = state["target_id"]
        target_name = state["target_name"]
        reason = "" if text.strip() == "-" else text.strip()
        ban_user(target_id, event.sender_id, reason)
        admin_flow_state.pop(event.sender_id, None)

        actor_rank = admin_rank(event.sender_id)
        actor_name = await get_display_name(event.sender_id)
        await event.reply(f"🔨 Пользователь {mention(target_id, target_name)} забанен.", parse_mode="html")
        await notify_main_admins(
            f"ранг [{actor_rank if actor_rank < 100 else 'Главный'}] {mention(event.sender_id, actor_name)} "
            f"забанил {mention(target_id, target_name)}" + (f"\nПричина: {reason}" if reason else "")
        )
        return

    if state["action"] == "unban" and state["step"] == "id":
        target_id, target_name = await resolve_target(text)
        if target_id is None:
            await event.reply("❌ Пользователь не найден. Введите ID или @username ещё раз (или /adm для отмены).")
            return
        admin_flow_state.pop(event.sender_id, None)
        if unban_user(target_id):
            await event.reply(f"🔓 Пользователь {mention(target_id, target_name)} разбанен.", parse_mode="html")
        else:
            await event.reply("❌ Этот пользователь не был забанен.")
        return

    if state["action"] == "add_admin" and state["step"] == "id":
        target_id, target_name = await resolve_target(text)
        if target_id is None:
            await event.reply("❌ Пользователь не найден. Введите ID или @username ещё раз (или /adm для отмены).")
            return
        state["target_id"] = target_id
        state["target_name"] = target_name
        state["step"] = "rank"
        await event.reply(
            f"Выбери ранг для {mention(target_id, target_name)}:",
            buttons=[
                [Button.inline("1️⃣ Стажёр", data="pick_rank:1")],
                [Button.inline("2️⃣ Админ", data="pick_rank:2")],
                [Button.inline("3️⃣ Старший админ", data="pick_rank:3")],
                [Button.inline("4️⃣ Правая рука", data="pick_rank:4")],
            ],
            parse_mode="html",
        )
        return

    if state["action"] in ("give", "take"):
        if state["step"] == "id":
            target_id, target_name = await resolve_target(text)
            if target_id is None:
                await event.reply("❌ Пользователь не найден. Введите ID или @username ещё раз (или /adm для отмены).")
                return
            state["target_id"] = target_id
            state["target_name"] = target_name
            state["step"] = "amount"
            action_word = "выдать" if state["action"] == "give" else "списать"
            await event.reply(
                f"Сколько GROM {action_word} пользователю {mention(target_id, target_name)}?",
                parse_mode="html",
            )
            return

        if state["step"] == "amount":
            try:
                amount = int(text)
            except ValueError:
                await event.reply("❗ Введите сумму целым числом.")
                return
            if amount <= 0:
                await event.reply("❗ Сумма должна быть больше 0.")
                return

            target_id = state["target_id"]
            target_name = state["target_name"]

            if state["action"] == "give":
                actor_rank = admin_rank(event.sender_id)
                limit = DAILY_GIVE_LIMITS.get(actor_rank)
                if limit is not None:
                    already = get_daily_gift_total(event.sender_id)
                    if already + amount > limit:
                        left = max(0, limit - already)
                        await event.reply(
                            f"❗ Лимит для «{rank_label(actor_rank)}» — {fmt(limit)} GROM в сутки.\n"
                            f"Уже выдано сегодня: {fmt(already)} GROM. Осталось: {fmt(left)} GROM.",
                        )
                        return
                    register_daily_gift(event.sender_id, amount)

                add_balance(target_id, amount)
                await event.reply(
                    f"✅ Выдано <b>{fmt(amount)} GROM</b> пользователю {mention(target_id, target_name)}\n"
                    f"💰 Новый баланс: {fmt(get_user_balance(target_id))} GROM",
                    parse_mode="html",
                )
            else:
                current = get_user_balance(target_id)
                new_bal = max(0, current - amount)
                set_user_balance(target_id, new_bal)
                await event.reply(
                    f"✅ Списано <b>{fmt(amount)} GROM</b> у {mention(target_id, target_name)}\n"
                    f"💰 Новый баланс: {fmt(new_bal)} GROM",
                    parse_mode="html",
                )

            admin_flow_state.pop(event.sender_id, None)
        return

    if state["action"] == "reset_user" and state["step"] == "id":
        target_id, target_name = await resolve_target(text)
        if target_id is None:
            await event.reply("❌ Пользователь не найден. Введите ID или @username ещё раз (или /adm для отмены).")
            return

        set_user_balance(target_id, 0)
        clear_user_active_games(target_id)

        cid, comp = get_company_by_owner(target_id)
        comp_note = ""
        if comp:
            comp["balance"] = 0
            comp["frozen"] = False
            comp["frozen_until"] = None
            save_data(data)
            comp_note = f"\n🏢 Баланс его компании «{comp['name']}» тоже обнулён."

        admin_flow_state.pop(event.sender_id, None)
        await event.reply(
            f"✅ Баланс пользователя {mention(target_id, target_name)} обнулён.{comp_note}",
            parse_mode="html",
        )
        return

    if state["action"] == "create_check":
        if state["step"] == "amount":
            try:
                amount = int(text)
            except ValueError:
                await event.reply("❗ Введите сумму целым числом.")
                return
            if amount <= 0:
                await event.reply("❗ Сумма должна быть больше 0.")
                return
            state["amount"] = amount
            state["step"] = "uses"
            await event.reply(
                "🔢 Сколько раз можно активировать этот чек? (например: 1 — одноразовый, 50 — на 50 активаций):",
            )
            return

        if state["step"] == "uses":
            try:
                uses = int(text)
            except ValueError:
                await event.reply("❗ Введите целое число.")
                return
            if uses <= 0:
                await event.reply("❗ Число активаций должно быть больше 0.")
                return
            state["uses"] = uses
            state["step"] = "code"
            await event.reply(
                "✏️ Теперь введите код активации чека (слово без пробелов, например: <code>NEWYEAR2026</code>):",
                parse_mode="html",
            )
            return

        if state["step"] == "code":
            code_display = text.strip()
            if " " in code_display or not code_display:
                await event.reply("❗ Код должен быть одним словом, без пробелов. Введите ещё раз:")
                return
            code_key = norm(code_display.lower())
            if code_key in data["checks"]:
                await event.reply("❗ Такой код уже существует. Введите другой:")
                return

            amount = state["amount"]
            uses = state["uses"]
            data["checks"][code_key] = {
                "amount": amount,
                "activations_total": uses,
                "activations_left": uses,
                "used_by": [],
                "display_code": code_display,
                "created_by": event.sender_id,
            }
            save_data(data)
            admin_flow_state.pop(event.sender_id, None)
            await event.reply(
                f"✅ Чек создан!\n\n"
                f"💰 Сумма: <b>{fmt(amount)} GROM</b>\n"
                f"🔢 Активаций: <b>{uses}</b>\n"
                f"🔑 Код: <code>{code_display}</code>\n\n"
                f"Чтобы активировать, пользователь пишет боту:\n"
                f"<code>активировать {code_display}</code>",
                parse_mode="html",
            )
            return
        return

    if state["action"] == "auction" and state["step"] == "price":
        try:
            price = int(text)
        except ValueError:
            await event.reply("❗ Введите цену целым числом.")
            return
        if price <= 0:
            await event.reply("❗ Цена должна быть больше 0.")
            return
        aid = str(data["next_auction_id"])
        data["next_auction_id"] += 1
        data["game_auctions"][aid] = {"game": state["game"], "price": price, "status": "open"}
        save_data(data)
        admin_flow_state.pop(event.sender_id, None)
        await event.reply(
            f"✅ Игра «{GAME_LABELS.get(state['game'], state['game'])}» выставлена на аукцион "
            f"за <b>{fmt(price)} GROM</b>.\nКомпании увидят её в разделе «🏢 Компания».",
            parse_mode="html",
        )
        return

    if state["action"] == "company_price":
        try:
            price = int(text)
        except ValueError:
            await event.reply("❗ Введите цену целым числом.")
            return
        if price <= 0:
            await event.reply("❗ Цена должна быть больше 0.")
            return
        set_company_price(price)
        admin_flow_state.pop(event.sender_id, None)
        await event.reply(f"✅ Новая цена компании: <b>{fmt(price)} GROM</b>", parse_mode="html")
        return


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.startswith("/givegram")))
async def give_gram(event):
    if not can_give(event.sender_id):
        return
    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.reply("Формат: /givegram @username 1000")
        return
    try:
        amount = int(parts[-1])
        target = parts[-2]
        user_id, _name = await resolve_target(target)
        if user_id is None:
            await event.reply("Пользователь не найден")
            return
        actor_rank = admin_rank(event.sender_id)
        limit = DAILY_GIVE_LIMITS.get(actor_rank)
        if limit is not None:
            already = get_daily_gift_total(event.sender_id)
            if already + amount > limit:
                await event.reply(
                    f"❗ Лимит для «{rank_label(actor_rank)}» — {fmt(limit)} GROM в сутки. "
                    f"Уже выдано сегодня: {fmt(already)} GROM."
                )
                return
            register_daily_gift(event.sender_id, amount)
        add_balance(user_id, amount)
        await event.reply(f"✅ Выдано {fmt(amount)} GROM пользователю {target}")
    except Exception:
        await event.reply("Ошибка в формате")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.startswith("/takegram")))
async def take_gram(event):
    if not can_take(event.sender_id):
        return
    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.reply("Формат: /takegram @username 500")
        return
    try:
        amount = int(parts[-1])
        target = parts[-2]
        user_id, _name = await resolve_target(target)
        if user_id is None:
            await event.reply("Пользователь не найден")
            return
        current = get_user_balance(user_id)
        new_bal = max(0, current - amount)
        set_user_balance(user_id, new_bal)
        await event.reply(f"✅ Забрано {fmt(amount)} GROM у {target}. Новый баланс: {fmt(new_bal)}")
    except Exception:
        await event.reply("Ошибка")


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^п(\s|$)", e.raw_text.lower())))
async def transfer_gram(event):
    sender = await event.get_sender()
    sender_id = event.sender_id
    sender_name = sender.first_name if sender else str(sender_id)
    parts = event.raw_text.split()

    comment = ""
    reply_msg = await event.get_reply_message()
    if reply_msg:
        if len(parts) < 2:
            await event.reply("❗ Формат: ответь на сообщение и напиши <code>п 1000</code>", parse_mode="html")
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await event.reply("❗ Укажи сумму числом: <code>п 1000</code>", parse_mode="html")
            return
        target_sender = await reply_msg.get_sender()
        target_id = reply_msg.sender_id
        target_name = target_sender.first_name if target_sender else str(target_id)
        comment = " ".join(parts[2:])
    else:
        if len(parts) < 3:
            await event.reply(
                "❗ Форматы передачи:\n"
                "• Ответь на сообщение: <code>п 1000</code>\n"
                "• По айди: <code>п 123456789 1000</code>",
                parse_mode="html",
            )
            return
        try:
            target_id = int(parts[1])
            amount = int(parts[2])
        except ValueError:
            await event.reply(
                "❗ Укажи айди и сумму числами: <code>п 123456789 1000</code>", parse_mode="html"
            )
            return
        target_id_res, target_name = await resolve_target(str(target_id))
        if target_id_res is None:
            await event.reply("❌ Пользователь с таким айди не найден.")
            return
        comment = " ".join(parts[3:])

    if target_id == sender_id:
        await event.reply("🤡 Нельзя передать GROM самому себе.")
        return
    if amount <= 0:
        await event.reply("❗ Сумма должна быть больше 0.")
        return

    sender_balance = get_user_balance(sender_id)
    if amount > sender_balance:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(sender_id, -amount)
    add_balance(target_id, amount)

    text = f"{mention(sender_id, sender_name)} перевел {fmt(amount)} GROM {mention(target_id, target_name)}"
    if comment:
        text += f"\n<blockquote>💬 {comment}</blockquote>"

    await event.reply(text, parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() in ["б", "b", "в"]))
async def check_balance_short(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    name = sender.first_name if sender else str(user_id)
    balance = get_user_balance(user_id)
    await event.respond(f"{mention(user_id, name)}\n💰 Баланс: <b>{fmt(balance)}</b> GROM", parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^дуэль\s+", e.raw_text.lower()) and not e.is_private))
async def duel_challenge(event):
    challenger_id = event.sender_id
    sender = await event.get_sender()
    challenger_name = sender.first_name if sender else str(challenger_id)
    chat_id = event.chat_id

    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.reply("❗ Формат: <code>дуэль @username 500</code>", parse_mode="html")
        return

    raw_target = parts[1]
    try:
        amount = int(parts[2])
    except ValueError:
        await event.reply("❗ Укажи сумму числом: <code>дуэль @username 500</code>", parse_mode="html")
        return

    if amount <= 0:
        await event.reply("❗ Сумма должна быть больше 0.")
        return

    challenger_balance = get_user_balance(challenger_id)
    if amount > challenger_balance:
        await event.reply("Недостаточно GROM на балансе")
        return

    target_id = None
    target_name = None

    reply_msg = await event.get_reply_message()
    if reply_msg and raw_target.lower() in ["@", "."]:
        target_sender = await reply_msg.get_sender()
        target_id = reply_msg.sender_id
        target_name = target_sender.first_name if target_sender else str(target_id)
    elif raw_target.startswith("@"):
        target_id, target_name = await resolve_target(raw_target)
        if target_id is None:
            await event.reply("❌ Пользователь не найден.")
            return
    else:
        await event.reply("❗ Формат: <code>дуэль @username 500</code>", parse_mode="html")
        return

    if target_id == challenger_id:
        await event.reply("🤡 Нельзя вызвать самого себя.")
        return

    target_balance = get_user_balance(target_id)
    if amount > target_balance:
        await event.reply(
            f"❌ У {mention(target_id, target_name)} недостаточно GROM (баланс: {fmt(target_balance)}).",
            parse_mode="html",
        )
        return

    if chat_id not in duel_challenges:
        duel_challenges[chat_id] = {}
    duel_challenges[chat_id][(challenger_id, target_id)] = {
        "amount": amount,
        "expires": datetime.now() + timedelta(seconds=60),
    }

    await event.reply(
        f"⚔️ {mention(challenger_id, challenger_name)} вызывает {mention(target_id, target_name)} на дуэль!\n"
        f"💰 Ставка: <b>{fmt(amount)} GROM</b>\n\n"
        f"{mention(target_id, target_name)}, напиши <b>принять</b> в течение 60 секунд.",
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() == "принять" and not e.is_private))
async def duel_accept(event):
    target_id = event.sender_id
    sender = await event.get_sender()
    target_name = sender.first_name if sender else str(target_id)
    chat_id = event.chat_id

    if chat_id not in duel_challenges:
        return

    challenge_key = None
    challenge_data = None
    for (c_id, t_id), cdata in list(duel_challenges[chat_id].items()):
        if t_id == target_id:
            if datetime.now() > cdata["expires"]:
                del duel_challenges[chat_id][(c_id, t_id)]
                await event.reply("⏰ Время дуэли истекло.")
                return
            challenge_key = (c_id, t_id)
            challenge_data = cdata
            break

    if not challenge_key:
        return

    challenger_id = challenge_key[0]
    amount = challenge_data["amount"]

    if get_user_balance(challenger_id) < amount:
        del duel_challenges[chat_id][challenge_key]
        c_name = await get_display_name(challenger_id)
        await event.reply(
            f"❌ У {mention(challenger_id, c_name)} больше нет достаточно GROM для дуэли.",
            parse_mode="html",
        )
        return
    if get_user_balance(target_id) < amount:
        del duel_challenges[chat_id][challenge_key]
        await event.reply("Недостаточно GROM на балансе")
        return

    del duel_challenges[chat_id][challenge_key]

    add_balance(challenger_id, -amount)
    add_balance(target_id, -amount)

    c_name = await get_display_name(challenger_id)

    await event.reply("⚔️ Дуэль начинается! Бросаем кубики...")
    await asyncio.sleep(2)

    c_roll = random.randint(1, 6)
    t_roll = random.randint(1, 6)
    pot = amount * 2

    result = (
        f"🎲 {mention(challenger_id, c_name)}: <b>{c_roll}</b>\n"
        f"🎲 {mention(target_id, target_name)}: <b>{t_roll}</b>\n\n"
    )

    if c_roll > t_roll:
        add_balance(challenger_id, pot)
        record_duel(challenger_id, target_id)
        result += f"🏆 Победитель: {mention(challenger_id, c_name)} +{fmt(pot)} GROM!"
    elif t_roll > c_roll:
        add_balance(target_id, pot)
        record_duel(target_id, challenger_id)
        result += f"🏆 Победитель: {mention(target_id, target_name)} +{fmt(pot)} GROM!"
    else:
        add_balance(challenger_id, amount)
        add_balance(target_id, amount)
        record_duel(challenger_id, target_id, draw=True)
        result += "🤝 Ничья! Ставки возвращены."

    await event.reply(result, parse_mode="html")


@client.on(events.NewMessage(func=lambda e: is_cmd(e.raw_text, "top") or (e.raw_text and e.raw_text.lower() == "топ")))
async def cmd_top(event):
    users = data.get("users", {})
    if not users:
        await event.reply("📊 Пока никто не заработал GROM.")
        return

    sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]

    lines = ["🏆 <b>Топ-10 богатейших игроков</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, balance) in enumerate(sorted_users):
        name = await get_display_name(int(uid))
        icon = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{icon} {mention(int(uid), name)} — <b>{fmt(balance)}</b> GROM")

    await event.reply("\n".join(lines), parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() == "лог" and not e.is_private))
async def cmd_roulette_log(event):
    chat_id = event.chat_id
    history = roulette_history.get(chat_id, [])
    if not history:
        await event.reply("📜 История ещё пуста — сыграйте хотя бы один раунд.")
        return
    lines = [f"{num}{sym}" for num, sym in reversed(history[-9:])]
    await event.reply("\n".join(lines))


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() == "ставки" and not e.is_private))
async def cmd_my_bets(event):
    chat_id = event.chat_id
    user_id = event.sender_id
    bets = [b for b in roulette_bets.get(chat_id, []) if b[0] == user_id]
    sender = await event.get_sender()
    name_txt = sender.first_name if sender else str(user_id)
    if not bets:
        name = mention(user_id, name_txt)
        await event.respond(f"Не найдено ни одной активной ставки {name}", parse_mode="html")
        return

    name = mention(user_id, name_txt)
    lines = [f"Ставка: {name} {fmt(b[1])} GROM на {bet_label_str(b[2], b[3])}" for b in bets]
    await event.respond("\n".join(lines), parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() == "отмена" and not e.is_private))
async def cmd_cancel_bets(event):
    chat_id = event.chat_id
    user_id = event.sender_id

    if roulette_spinning.get(chat_id):
        await event.reply("Раунд уже идёт — отмена невозможна.")
        return

    all_bets = roulette_bets.get(chat_id, [])
    player_bets = [b for b in all_bets if b[0] == user_id]
    sender = await event.get_sender()
    name_txt = sender.first_name if sender else str(user_id)
    if not player_bets:
        name = mention(user_id, name_txt)
        await event.respond(f"Не найдено ни одной активной ставки {name}", parse_mode="html")
        return

    refund = sum(b[1] for b in player_bets)
    roulette_bets[chat_id] = [b for b in all_bets if b[0] != user_id]
    if not roulette_bets[chat_id]:
        roulette_bets.pop(chat_id, None)
        first_bet_time.pop(chat_id, None)
    add_balance(user_id, refund)
    save_roulette_state()
    name = mention(user_id, name_txt)
    await event.respond(f"Ставки отменены {name}", parse_mode="html")


def looks_like_bet(text: str) -> bool:
    parts = text.strip().split()
    if len(parts) < 2:
        return False
    try:
        int(parts[0])
        return True
    except ValueError:
        return False


@client.on(events.NewMessage(func=lambda e: not e.is_private and e.raw_text and looks_like_bet(e.raw_text)))
async def handle_roulette_bet(event):
    chat_id = event.chat_id

    multi = parse_multi_bet(event.raw_text)
    if not multi:
        return

    amount, bets = multi
    user_id = event.sender_id
    total_cost = amount * len(bets)

    async with get_chat_lock(chat_id):
        if roulette_spinning.get(chat_id):
            return

        frozen, comp = is_game_frozen("рулетка")
        if frozen:
            await event.reply(f"⛔ Рулетка временно недоступна: компания-владелец «{comp['name']}» в долгу.")
            return

        balance = get_user_balance(user_id)
        if total_cost > balance:
            await event.reply("Недостаточно GROM на балансе")
            return

        add_balance(user_id, -total_cost)

        if chat_id not in roulette_bets:
            roulette_bets[chat_id] = []
        for bet_type, bet_value, multiplier in bets:
            roulette_bets[chat_id].append((user_id, amount, bet_type, bet_value, multiplier))

        if chat_id not in first_bet_time:
            first_bet_time[chat_id] = datetime.now()
        save_roulette_state()

    sender = await event.get_sender()
    name = mention(user_id, sender.first_name if sender else str(user_id))
    bet_lines = "\n".join(
        f"Ставка принята: {name} {fmt(amount)} GROM на {bet_label_str(bt, bv)}"
        for bt, bv, _ in bets
    )
    await event.reply(bet_lines, parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() in ["го", "go"] and not e.is_private))
async def trigger_roulette(event):
    chat_id = event.chat_id

    should_spin = False
    async with get_chat_lock(chat_id):
        if roulette_spinning.get(chat_id):
            return

        bets = roulette_bets.get(chat_id, [])
        if not bets:
            await event.respond("Невозможно начать игру без ставок.")
            return

        elapsed = (datetime.now() - first_bet_time[chat_id]).total_seconds()
        wait_needed = 13 - elapsed
        if wait_needed > 0:
            await event.respond(f"Ошибка. Закончить раунд можно через {int(wait_needed) + 1} секунд")
            return

        roulette_spinning[chat_id] = True
        should_spin = True

    if should_spin:
        await process_roulette(chat_id)


@client.on(events.NewMessage(func=lambda e: e.message.gif is not None and waiting_for_gif))
async def save_gif(event):
    global waiting_for_gif
    if admin_rank(event.sender_id) < RANK_SENIOR:
        return
    await event.download_media(file=GIF_MEDIA_PATH)
    with open(GIF_FILE, "w") as f:
        f.write(GIF_MEDIA_PATH)
    waiting_for_gif = False
    await event.reply("✅ Гифка сохранена навсегда! Будет использоваться в рулетке.")


@client.on(events.NewMessage(func=lambda e: is_cmd(e.raw_text, "menu")))
async def cmd_menu(event):
    await event.respond("📱 Меню бота:", buttons=main_keyboard())


# ========== ИГРА: МИНЫ ==========
MINES_GRID_SIZE = 25  # 5x5
mines_games: dict = {}  # user_id -> game state


def mines_fair_multiplier(mines_count: int, revealed: int, house_edge: float = 0.04) -> float:
    n = MINES_GRID_SIZE
    m = mines_count
    if revealed <= 0:
        return 1.0
    prob = 1.0
    for i in range(revealed):
        prob *= (n - m - i) / (n - i)
    if prob <= 0:
        return 1.0
    return (1 / prob) * (1 - house_edge)


def mines_apply_chance_and_swap(game: dict, idx: int) -> bool:
    """Применяет общий шанс игры «мины» и, если результат подменяется,
    сохраняет корректность расположения мин на поле."""
    actual_safe = idx not in game["mine_positions"]
    forced_safe = apply_chance("мины", actual_safe)
    if forced_safe != actual_safe:
        if forced_safe:
            # Была мина, но принудительно безопасно — переносим мину на другую клетку
            game["mine_positions"].discard(idx)
            free = [
                i for i in range(MINES_GRID_SIZE)
                if i != idx and i not in game["mine_positions"] and i not in game["revealed"]
            ]
            if free:
                game["mine_positions"].add(random.choice(free))
        else:
            # Была безопасна, но принудительно мина — переносим одну мину сюда
            game["mine_positions"].add(idx)
            others = [m for m in game["mine_positions"] if m != idx]
            if others:
                game["mine_positions"].discard(random.choice(others))
    return forced_safe


def mines_keyboard(game: dict, finished: bool = False, won: bool = False) -> list:
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if finished:
                if idx in game["mine_positions"]:
                    label = "💣"
                elif idx in game["revealed"]:
                    label = "💎"
                else:
                    label = "▫️"
                row.append(Button.inline(label, data="mn_done"))
            elif idx in game["revealed"]:
                row.append(Button.inline("💎", data="mn_done"))
            else:
                row.append(Button.inline("🔲", data=f"mn:{game['owner']}:{idx}"))
        rows.append(row)
    if not finished and game["revealed"]:
        mult = mines_fair_multiplier(game["mines_count"], len(game["revealed"]))
        cashout = int(game["bet"] * mult)
        rows.append([Button.inline(f"💰 Забрать {fmt(cashout)} GROM (x{mult:.2f})", data=f"mn_cash:{game['owner']}")])
    return rows


def mines_text(game: dict, finished: bool = False, won: bool = False, hit_idx: int = None) -> str:
    revealed_n = len(game["revealed"])
    mult = mines_fair_multiplier(game["mines_count"], revealed_n) if revealed_n else 1.0
    header = f"💣 <b>МИНЫ</b>\n\nСтавка: <b>{fmt(game['bet'])} GROM</b> | Мин на поле: <b>{game['mines_count']}</b>\n"
    if not finished:
        return (
            header + f"Открыто клеток: <b>{revealed_n}</b> | Множитель: <b>x{mult:.2f}</b>\n\n"
            "Нажимай на клетки, чтобы открывать их. Не задень мину!"
        )
    if won:
        cashout = int(game["bet"] * mult)
        return (
            header + f"✅ Клеток открыто: <b>{revealed_n}</b> | Множитель: <b>x{mult:.2f}</b>\n\n"
            f"🎉 Забрано: <b>{fmt(cashout)} GROM</b>!"
        )
    return (
        header + f"💥 Взрыв на клетке #{hit_idx}!\n\n"
        f"❌ Проигрыш: <b>{fmt(game['bet'])} GROM</b>"
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^мины\s+\d+(\s+\d+)?$", e.raw_text.lower().strip())))
async def start_mines(event):
    user_id = event.sender_id
    if user_id in mines_games:
        await event.reply("❗ У тебя уже есть активная игра в мины. Заверши её сначала.")
        return

    frozen, comp = is_game_frozen("мины")
    if frozen:
        await event.reply(f"⛔ Мины временно недоступны: компания-владелец «{comp['name']}» в долгу.")
        return

    parts = event.raw_text.strip().split()
    bet = int(parts[1])
    mines_count = int(parts[2]) if len(parts) > 2 else 5

    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if not 1 <= mines_count <= 24:
        await event.reply("❗ Количество мин должно быть от 1 до 24.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -bet)
    mine_positions = set(random.sample(range(MINES_GRID_SIZE), mines_count))
    game = {
        "owner": user_id,
        "chat_id": event.chat_id,
        "bet": bet,
        "mines_count": mines_count,
        "mine_positions": mine_positions,
        "revealed": set(),
    }
    mines_games[user_id] = game

    msg = await event.reply(mines_text(game), buttons=mines_keyboard(game), parse_mode="html")
    game["message_id"] = msg.id


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("mn:")))
async def mines_click(event):
    _, owner_str, idx_str = event.data.decode().split(":")
    owner_id = int(owner_str)
    idx = int(idx_str)

    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return

    game = mines_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return

    if idx in game["revealed"]:
        await event.answer()
        return

    safe = mines_apply_chance_and_swap(game, idx)

    if not safe:
        mines_games.pop(owner_id, None)
        apply_company_economy("мины", False, game["bet"])
        await event.edit(mines_text(game, finished=True, won=False, hit_idx=idx), buttons=mines_keyboard(game, finished=True), parse_mode="html")
        await event.answer("💥 Бум! Мина!", alert=True)
        return

    game["revealed"].add(idx)

    if len(game["revealed"]) >= MINES_GRID_SIZE - game["mines_count"]:
        mult = mines_fair_multiplier(game["mines_count"], len(game["revealed"]))
        win_amount = int(game["bet"] * mult)
        add_balance(owner_id, win_amount)
        apply_company_economy("мины", True, game["bet"], win_amount)
        mines_games.pop(owner_id, None)
        await event.edit(mines_text(game, finished=True, won=True), buttons=mines_keyboard(game, finished=True), parse_mode="html")
        await event.answer("🎉 Все безопасные клетки открыты!", alert=True)
        return

    await event.edit(mines_text(game), buttons=mines_keyboard(game), parse_mode="html")
    await event.answer("💎 Безопасно!")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("mn_cash:")))
async def mines_cashout(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return

    game = mines_games.get(owner_id)
    if not game or not game["revealed"]:
        await event.answer("Нечего забирать.", alert=True)
        return

    mult = mines_fair_multiplier(game["mines_count"], len(game["revealed"]))
    win_amount = int(game["bet"] * mult)
    add_balance(owner_id, win_amount)
    apply_company_economy("мины", True, game["bet"], win_amount)
    mines_games.pop(owner_id, None)
    await event.edit(mines_text(game, finished=True, won=True), buttons=mines_keyboard(game, finished=True), parse_mode="html")
    await event.answer(f"💰 Забрано {fmt(win_amount)} GROM!", alert=True)


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "mn_done"))
async def mines_done_noop(event):
    await event.answer()


# ========== ИГРА: БЛЭКДЖЕК ==========
CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]
CARD_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
blackjack_games: dict = {}  # user_id -> game state


def bj_new_deck() -> list:
    deck = [(r, s) for s in CARD_SUITS for r in CARD_RANKS]
    random.shuffle(deck)
    return deck


def bj_card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def bj_hand_value(hand: list) -> int:
    total = sum(bj_card_value(r) for r, s in hand)
    aces = sum(1 for r, s in hand if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def bj_format_hand(hand: list) -> str:
    return " ".join(f"{r}{s}" for r, s in hand)


def bj_keyboard(owner_id: int) -> list:
    return [[
        Button.inline("🃏 Взять карту", data=f"bj_hit:{owner_id}"),
        Button.inline("✋ Хватит", data=f"bj_stand:{owner_id}"),
    ]]


def bj_text(game: dict, reveal_dealer: bool = False, result_line: str = "") -> str:
    player_val = bj_hand_value(game["player"])
    lines = [f"🃏 <b>БЛЭКДЖЕК</b>\n", f"Ставка: <b>{fmt(game['bet'])} GROM</b>\n"]
    lines.append(f"👤 Ваши карты: {bj_format_hand(game['player'])} (<b>{player_val}</b>)")
    if reveal_dealer:
        dealer_val = bj_hand_value(game["dealer"])
        lines.append(f"🤖 Карты дилера: {bj_format_hand(game['dealer'])} (<b>{dealer_val}</b>)")
    else:
        hidden_card = f"{game['dealer'][0][0]}{game['dealer'][0][1]}"
        lines.append(f"🤖 Карты дилера: {hidden_card} 🂠")
    if result_line:
        lines.append("\n" + result_line)
    return "\n".join(lines)


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^бл(э|е)к(джек)?\s+\d+$", e.raw_text.lower().strip())))
async def start_blackjack(event):
    user_id = event.sender_id
    if user_id in blackjack_games:
        await event.reply("❗ У тебя уже есть активная игра в блэкджек.")
        return

    frozen, comp = is_game_frozen("блэкджек")
    if frozen:
        await event.reply(f"⛔ Блэкджек временно недоступен: компания-владелец «{comp['name']}» в долгу.")
        return

    bet = int(event.raw_text.strip().split()[1])
    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -bet)
    deck = bj_new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    game = {"owner": user_id, "bet": bet, "deck": deck, "player": player, "dealer": dealer}
    blackjack_games[user_id] = game

    if bj_hand_value(player) == 21:
        win = apply_chance("блэкджек", True)
        if win:
            win_amount = int(bet * 2.5)
            add_balance(user_id, win_amount)
            apply_company_economy("блэкджек", True, bet, win_amount)
            blackjack_games.pop(user_id, None)
            await event.reply(
                bj_text(game, reveal_dealer=True, result_line=f"🎉 <b>Блэкджек!</b> Выигрыш: {fmt(win_amount)} GROM"),
                parse_mode="html",
            )
        else:
            apply_company_economy("блэкджек", False, bet)
            blackjack_games.pop(user_id, None)
            await event.reply(
                bj_text(game, reveal_dealer=True, result_line=f"❌ <b>Проигрыш.</b> -{fmt(bet)} GROM"),
                parse_mode="html",
            )
        return

    await event.reply(bj_text(game), buttons=bj_keyboard(user_id), parse_mode="html")


async def bj_finish(event, owner_id: int, game: dict):
    dealer = game["dealer"]
    deck = game["deck"]
    while bj_hand_value(dealer) < 17:
        dealer.append(deck.pop())

    player_val = bj_hand_value(game["player"])
    dealer_val = bj_hand_value(dealer)
    bet = game["bet"]

    if dealer_val > 21 or player_val > dealer_val:
        natural_result = "win"
    elif player_val == dealer_val:
        natural_result = "push"
    else:
        natural_result = "lose"

    if natural_result != "push":
        win = apply_chance("блэкджек", natural_result == "win")
        if win:
            win_amount = bet * 2
            add_balance(owner_id, win_amount)
            apply_company_economy("блэкджек", True, bet, win_amount)
            result = f"🎉 <b>Победа!</b> Выигрыш: {fmt(win_amount)} GROM"
        else:
            apply_company_economy("блэкджек", False, bet)
            result = f"❌ <b>Проигрыш.</b> -{fmt(bet)} GROM"
    else:
        add_balance(owner_id, bet)
        result = "🤝 <b>Ничья.</b> Ставка возвращена."

    blackjack_games.pop(owner_id, None)
    await event.edit(bj_text(game, reveal_dealer=True, result_line=result), parse_mode="html")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("bj_hit:")))
async def blackjack_hit(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = blackjack_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return

    game["player"].append(game["deck"].pop())
    player_val = bj_hand_value(game["player"])

    if player_val > 21:
        blackjack_games.pop(owner_id, None)
        apply_company_economy("блэкджек", False, game["bet"])
        await event.edit(
            bj_text(game, reveal_dealer=True, result_line=f"💥 <b>Перебор!</b> Проигрыш: {fmt(game['bet'])} GROM"),
            parse_mode="html",
        )
        await event.answer()
        return

    if player_val == 21:
        await bj_finish(event, owner_id, game)
        await event.answer()
        return

    await event.edit(bj_text(game), buttons=bj_keyboard(owner_id), parse_mode="html")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("bj_stand:")))
async def blackjack_stand(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = blackjack_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return
    await bj_finish(event, owner_id, game)
    await event.answer()


# ========== ИГРА: КРАШ ==========
crash_games: dict = {}  # user_id -> game state


def crash_generate_point() -> float:
    chance = get_game_chance("краш")
    if chance <= 0:
        return float("inf")  # шанс 0% — крах не наступит, пока игрок сам не заберёт
    house_edge = 0.04 + (chance - 50) * 0.0016
    house_edge = max(0.005, min(house_edge, 0.5))
    r = random.random()
    if r < house_edge:
        return 1.00
    point = (1 - house_edge) / (1 - r)
    return round(min(point, 1000.0), 2)


def crash_keyboard(owner_id: int) -> list:
    return [[Button.inline("💰 Забрать", data=f"crash_cash:{owner_id}")]]


def crash_text(bet: int, mult: float, finished: bool = False, crashed: bool = False, cashed_out: bool = False) -> str:
    header = "🚀 <b>КРАШ</b>\n\n"
    if not finished:
        return header + f"Ставка: <b>{fmt(bet)} GROM</b>\nМножитель: <b>x{mult:.2f}</b> 📈\n\nЖми «Забрать», пока не поздно!"
    if cashed_out:
        win_amount = int(bet * mult)
        return header + f"Ставка: <b>{fmt(bet)} GROM</b>\n💰 Забрано на <b>x{mult:.2f}</b>: <b>{fmt(win_amount)} GROM</b>!"
    return header + f"Ставка: <b>{fmt(bet)} GROM</b>\n💥 <b>Крах на x{mult:.2f}!</b>\n❌ Проигрыш: {fmt(bet)} GROM"


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^краш\s+\d+(\s+\d+(\.\d+)?)?$", e.raw_text.lower().strip())))
async def start_crash(event):
    user_id = event.sender_id
    if user_id in crash_games:
        await event.reply("❗ У тебя уже есть активная игра в краш.")
        return

    frozen, comp = is_game_frozen("краш")
    if frozen:
        await event.reply(f"⛔ Краш временно недоступен: компания-владелец «{comp['name']}» в долгу.")
        return

    parts = event.raw_text.strip().split()
    bet = int(parts[1])
    auto_cashout = float(parts[2]) if len(parts) > 2 else None
    if auto_cashout is not None and auto_cashout < 1.01:
        await event.reply("❗ Множитель автовывода должен быть не меньше 1.01.")
        return
    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -bet)
    crash_point = crash_generate_point()
    game = {
        "owner": user_id,
        "bet": bet,
        "mult": 1.00,
        "crash_point": crash_point,
        "auto_cashout": auto_cashout,
        "cashed_out": False,
        "done": False,
    }
    crash_games[user_id] = game

    auto_line = f"\n🎯 Автовывод на: <b>x{auto_cashout:.2f}</b>" if auto_cashout else ""
    msg = await event.reply(crash_text(bet, 1.00) + auto_line, buttons=crash_keyboard(user_id), parse_mode="html")
    game["message"] = msg
    asyncio.create_task(crash_loop(user_id))


async def crash_loop(owner_id: int):
    game = crash_games.get(owner_id)
    if not game:
        return
    msg = game["message"]

    while True:
        await asyncio.sleep(0.8)
        game = crash_games.get(owner_id)
        if not game or game["done"]:
            return

        step = max(0.03, game["mult"] * 0.12)
        game["mult"] = round(game["mult"] + step, 2)

        if game["mult"] >= game["crash_point"]:
            game["mult"] = game["crash_point"]
            game["done"] = True
            crash_games.pop(owner_id, None)
            apply_company_economy("краш", False, game["bet"])
            try:
                await msg.edit(
                    crash_text(game["bet"], game["mult"], finished=True, crashed=True),
                    buttons=None,
                    parse_mode="html",
                )
            except Exception:
                pass
            return

        if game["auto_cashout"] and game["mult"] >= game["auto_cashout"]:
            game["mult"] = game["auto_cashout"]
            game["done"] = True
            game["cashed_out"] = True
            crash_games.pop(owner_id, None)
            win_amount = int(game["bet"] * game["mult"])
            add_balance(owner_id, win_amount)
            apply_company_economy("краш", True, game["bet"], win_amount)
            try:
                await msg.edit(
                    crash_text(game["bet"], game["mult"], finished=True, cashed_out=True),
                    buttons=None,
                    parse_mode="html",
                )
            except Exception:
                pass
            return

        try:
            await msg.edit(crash_text(game["bet"], game["mult"]), buttons=crash_keyboard(owner_id), parse_mode="html")
        except Exception:
            pass


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("crash_cash:")))
async def crash_cashout(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return

    game = crash_games.get(owner_id)
    if not game or game["done"]:
        await event.answer("Игра уже завершена.", alert=True)
        return

    game["done"] = True
    game["cashed_out"] = True
    crash_games.pop(owner_id, None)

    win_amount = int(game["bet"] * game["mult"])
    add_balance(owner_id, win_amount)
    apply_company_economy("краш", True, game["bet"], win_amount)

    await event.edit(
        crash_text(game["bet"], game["mult"], finished=True, cashed_out=True),
        buttons=None,
        parse_mode="html",
    )
    await event.answer(f"💰 Забрано {fmt(win_amount)} GROM на x{game['mult']:.2f}!", alert=True)


# ========== ИГРА: БАШНЯ ==========
TOWER_LEVELS = 9
TOWER_TILES = 5  # 1 ловушка, 4 безопасные двери на этаже
tower_games: dict = {}  # user_id -> game state


def tower_fair_multiplier(level: int, house_edge: float = 0.04) -> float:
    if level <= 0:
        return 1.0
    prob_per_level = (TOWER_TILES - 1) / TOWER_TILES
    total_prob = prob_per_level ** level
    if total_prob <= 0:
        return 1.0
    return (1 / total_prob) * (1 - house_edge)


def tower_keyboard(game: dict) -> list:
    rows = []
    level = game["level"]
    row = [Button.inline("🚪", data=f"tw:{game['owner']}:{i}") for i in range(TOWER_TILES)]
    rows.append(row)
    if level > 0:
        mult = tower_fair_multiplier(level)
        cashout = int(game["bet"] * mult)
        rows.append([Button.inline(f"💰 Забрать {fmt(cashout)} GROM (x{mult:.2f})", data=f"tw_cash:{game['owner']}")])
    return rows


def tower_text(game: dict, finished: bool = False, won: bool = False) -> str:
    level = game["level"]
    mult = tower_fair_multiplier(level) if level else 1.0
    header = f"🏗 <b>БАШНЯ</b>\n\nСтавка: <b>{fmt(game['bet'])} GROM</b>\n"
    if not finished:
        return header + f"Этаж: <b>{level}/{TOWER_LEVELS}</b> | Множитель: <b>x{mult:.2f}</b>\n\nВыбери одну из {TOWER_TILES} дверей."
    if won:
        cashout = int(game["bet"] * mult)
        return header + f"✅ Этаж: <b>{level}/{TOWER_LEVELS}</b> | Множитель: <b>x{mult:.2f}</b>\n\n🎉 Забрано: <b>{fmt(cashout)} GROM</b>!"
    return header + f"💥 Ловушка на этаже {level + 1}!\n\n❌ Проигрыш: <b>{fmt(game['bet'])} GROM</b>"


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^башня\s+\d+$", e.raw_text.lower().strip())))
async def start_tower(event):
    user_id = event.sender_id
    if user_id in tower_games:
        await event.reply("❗ У тебя уже есть активная игра в башню.")
        return
    frozen, comp = is_game_frozen("башня")
    if frozen:
        await event.reply(f"⛔ Башня временно недоступна: компания-владелец «{comp['name']}» в долгу.")
        return
    bet = int(event.raw_text.strip().split()[1])
    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return
    add_balance(user_id, -bet)
    game = {"owner": user_id, "bet": bet, "level": 0}
    tower_games[user_id] = game
    await event.reply(tower_text(game), buttons=tower_keyboard(game), parse_mode="html")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("tw:")))
async def tower_click(event):
    _, owner_str, idx_str = event.data.decode().split(":")
    owner_id = int(owner_str)
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = tower_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return

    bomb_idx = random.randint(0, TOWER_TILES - 1)
    actual_safe = int(idx_str) != bomb_idx
    safe = apply_chance("башня", actual_safe)

    if not safe:
        tower_games.pop(owner_id, None)
        apply_company_economy("башня", False, game["bet"])
        await event.edit(tower_text(game, finished=True, won=False), buttons=None, parse_mode="html")
        await event.answer("💥 Ловушка!", alert=True)
        return

    game["level"] += 1
    if game["level"] >= TOWER_LEVELS:
        mult = tower_fair_multiplier(game["level"])
        win_amount = int(game["bet"] * mult)
        add_balance(owner_id, win_amount)
        apply_company_economy("башня", True, game["bet"], win_amount)
        tower_games.pop(owner_id, None)
        await event.edit(tower_text(game, finished=True, won=True), buttons=None, parse_mode="html")
        await event.answer("🎉 Башня пройдена полностью!", alert=True)
        return

    await event.edit(tower_text(game), buttons=tower_keyboard(game), parse_mode="html")
    await event.answer("✅ Этаж пройден!")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("tw_cash:")))
async def tower_cashout(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = tower_games.get(owner_id)
    if not game or game["level"] <= 0:
        await event.answer("Нечего забирать.", alert=True)
        return
    mult = tower_fair_multiplier(game["level"])
    win_amount = int(game["bet"] * mult)
    add_balance(owner_id, win_amount)
    apply_company_economy("башня", True, game["bet"], win_amount)
    tower_games.pop(owner_id, None)
    await event.edit(tower_text(game, finished=True, won=True), buttons=None, parse_mode="html")
    await event.answer(f"💰 Забрано {fmt(win_amount)} GROM!", alert=True)


# ========== ИГРА: КВАК ==========
KVAK_WINDOWS = 5
KVAK_MAX_LEVEL = 4  # на уровне N игрока ждут N мин из 5 окошек (максимум 4 — иначе не останется безопасных)
kvak_games: dict = {}  # user_id -> game state


def kvak_mines_at_level(level: int) -> int:
    return level  # уровень 1 -> 1 мина, уровень 2 -> 2 мины и т.д.


def kvak_fair_multiplier(level_reached: int, house_edge: float = 0.04) -> float:
    if level_reached <= 0:
        return 1.0
    prob = 1.0
    for lvl in range(1, level_reached + 1):
        mines = kvak_mines_at_level(lvl)
        safe = KVAK_WINDOWS - mines
        prob *= safe / KVAK_WINDOWS
    if prob <= 0:
        return 1.0
    return (1 / prob) * (1 - house_edge)


def kvak_keyboard(game: dict) -> list:
    row = [Button.inline("🐸", data=f"kw:{game['owner']}:{i}") for i in range(KVAK_WINDOWS)]
    rows = [row]
    if game["level"] > 0:
        mult = kvak_fair_multiplier(game["level"])
        cashout = int(game["bet"] * mult)
        rows.append([Button.inline(f"💰 Забрать {fmt(cashout)} GROM (x{mult:.2f})", data=f"kw_cash:{game['owner']}")])
    return rows


def kvak_text(game: dict, finished: bool = False, won: bool = False) -> str:
    level = game["level"]
    mult = kvak_fair_multiplier(level) if level else 1.0
    header = f"🐸 <b>КВАК</b>\n\nСтавка: <b>{fmt(game['bet'])} GROM</b>\n"
    if not finished:
        next_mines = kvak_mines_at_level(level + 1)
        return (
            header + f"Раунд: <b>{level}/{KVAK_MAX_LEVEL}</b> | Множитель: <b>x{mult:.2f}</b>\n"
            f"На следующем раунде мин: <b>{next_mines} из {KVAK_WINDOWS}</b>\n\n"
            "Выбери окошко — лягушка прыгнет, если оно безопасно!"
        )
    if won:
        cashout = int(game["bet"] * mult)
        return header + f"✅ Раунд: <b>{level}/{KVAK_MAX_LEVEL}</b> | Множитель: <b>x{mult:.2f}</b>\n\n🎉 Забрано: <b>{fmt(cashout)} GROM</b>!"
    return header + f"💥 Лягушка не допрыгнула на раунде {level + 1}!\n\n❌ Проигрыш: <b>{fmt(game['bet'])} GROM</b>"


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^квак\s+\d+$", e.raw_text.lower().strip())))
async def start_kvak(event):
    user_id = event.sender_id
    if user_id in kvak_games:
        await event.reply("❗ У тебя уже есть активная игра в квак.")
        return
    frozen, comp = is_game_frozen("квак")
    if frozen:
        await event.reply(f"⛔ Квак временно недоступен: компания-владелец «{comp['name']}» в долгу.")
        return
    bet = int(event.raw_text.strip().split()[1])
    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return
    add_balance(user_id, -bet)
    game = {"owner": user_id, "bet": bet, "level": 0}
    kvak_games[user_id] = game
    await event.reply(kvak_text(game), buttons=kvak_keyboard(game), parse_mode="html")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("kw:")))
async def kvak_click(event):
    _, owner_str, idx_str = event.data.decode().split(":")
    owner_id = int(owner_str)
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = kvak_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return

    next_level = game["level"] + 1
    mines = kvak_mines_at_level(next_level)
    mine_positions = set(random.sample(range(KVAK_WINDOWS), mines))
    actual_safe = int(idx_str) not in mine_positions
    safe = apply_chance("квак", actual_safe)

    if not safe:
        kvak_games.pop(owner_id, None)
        apply_company_economy("квак", False, game["bet"])
        await event.edit(kvak_text(game, finished=True, won=False), buttons=None, parse_mode="html")
        await event.answer("💥 Не допрыгнула!", alert=True)
        return

    game["level"] = next_level
    if game["level"] >= KVAK_MAX_LEVEL:
        mult = kvak_fair_multiplier(game["level"])
        win_amount = int(game["bet"] * mult)
        add_balance(owner_id, win_amount)
        apply_company_economy("квак", True, game["bet"], win_amount)
        kvak_games.pop(owner_id, None)
        await event.edit(kvak_text(game, finished=True, won=True), buttons=None, parse_mode="html")
        await event.answer("🎉 Все раунды пройдены!", alert=True)
        return

    await event.edit(kvak_text(game), buttons=kvak_keyboard(game), parse_mode="html")
    await event.answer("✅ Допрыгнула!")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("kw_cash:")))
async def kvak_cashout(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = kvak_games.get(owner_id)
    if not game or game["level"] <= 0:
        await event.answer("Нечего забирать.", alert=True)
        return
    mult = kvak_fair_multiplier(game["level"])
    win_amount = int(game["bet"] * mult)
    add_balance(owner_id, win_amount)
    apply_company_economy("квак", True, game["bet"], win_amount)
    kvak_games.pop(owner_id, None)
    await event.edit(kvak_text(game, finished=True, won=True), buttons=None, parse_mode="html")
    await event.answer(f"💰 Забрано {fmt(win_amount)} GROM!", alert=True)


# ========== ИГРА: HILO ==========
HILO_MIN, HILO_MAX = 1, 13
HILO_RANK_NAMES = {1: "A", 11: "J", 12: "Q", 13: "K"}
hilo_games: dict = {}  # user_id -> game state


def hilo_rank_str(v: int) -> str:
    return HILO_RANK_NAMES.get(v, str(v))


def hilo_draw_different(exclude: int) -> int:
    while True:
        v = random.randint(HILO_MIN, HILO_MAX)
        if v != exclude:
            return v


def hilo_prob(current: int, direction: str) -> float:
    total_other = HILO_MAX - HILO_MIN
    if direction == "higher":
        return (HILO_MAX - current) / total_other
    return (current - HILO_MIN) / total_other


def hilo_fair_multiplier(prob: float, house_edge: float = 0.04) -> float:
    if prob <= 0 or prob >= 1:
        return 1.0
    return (1 / prob) * (1 - house_edge)


def hilo_keyboard(game: dict) -> list:
    current = game["current"]
    row = []
    if current < HILO_MAX:
        row.append(Button.inline("⬆️ Больше", data=f"hl:{game['owner']}:higher"))
    if current > HILO_MIN:
        row.append(Button.inline("⬇️ Меньше", data=f"hl:{game['owner']}:lower"))
    rows = [row]
    if game["level"] > 0:
        mult = hilo_fair_multiplier(game["prob"])
        cashout = int(game["bet"] * mult)
        rows.append([Button.inline(f"💰 Забрать {fmt(cashout)} GROM (x{mult:.2f})", data=f"hl_cash:{game['owner']}")])
    return rows


def hilo_text(game: dict, finished: bool = False, won: bool = False, next_card: int = None) -> str:
    mult = hilo_fair_multiplier(game["prob"]) if game["level"] else 1.0
    header = f"🎴 <b>HiLo</b>\n\nСтавка: <b>{fmt(game['bet'])} GROM</b>\nТекущая карта: <b>{hilo_rank_str(game['current'])}</b>\n"
    if not finished:
        return header + f"Раунд: <b>{game['level']}</b> | Множитель: <b>x{mult:.2f}</b>\n\nСледующая карта будет больше или меньше?"
    if won:
        cashout = int(game["bet"] * mult)
        return header + f"✅ Раунд: <b>{game['level']}</b> | Множитель: <b>x{mult:.2f}</b>\n\n🎉 Забрано: <b>{fmt(cashout)} GROM</b>!"
    return (
        header + f"Выпала карта: <b>{hilo_rank_str(next_card)}</b>\n\n"
        f"💥 Не угадал!\n❌ Проигрыш: <b>{fmt(game['bet'])} GROM</b>"
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^hilo\s+\d+$", e.raw_text.lower().strip())))
async def start_hilo(event):
    user_id = event.sender_id
    if user_id in hilo_games:
        await event.reply("❗ У тебя уже есть активная игра в HiLo.")
        return
    frozen, comp = is_game_frozen("hilo")
    if frozen:
        await event.reply(f"⛔ HiLo временно недоступна: компания-владелец «{comp['name']}» в долгу.")
        return
    bet = int(event.raw_text.strip().split()[1])
    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return
    add_balance(user_id, -bet)
    game = {
        "owner": user_id,
        "bet": bet,
        "current": random.randint(HILO_MIN, HILO_MAX),
        "prob": 1.0,
        "level": 0,
    }
    hilo_games[user_id] = game
    await event.reply(hilo_text(game), buttons=hilo_keyboard(game), parse_mode="html")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("hl:")))
async def hilo_click(event):
    _, owner_str, direction = event.data.decode().split(":")
    owner_id = int(owner_str)
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = hilo_games.get(owner_id)
    if not game:
        await event.answer("Игра уже завершена.", alert=True)
        return

    current = game["current"]
    next_card = hilo_draw_different(current)
    actual_correct = (next_card > current) if direction == "higher" else (next_card < current)
    correct = apply_chance("hilo", actual_correct)

    if not correct:
        hilo_games.pop(owner_id, None)
        apply_company_economy("hilo", False, game["bet"])
        await event.edit(hilo_text(game, finished=True, won=False, next_card=next_card), buttons=None, parse_mode="html")
        await event.answer("💥 Не угадал!", alert=True)
        return

    # результат «верно» — карта обязана соответствовать выбранному направлению
    if direction == "higher" and next_card <= current:
        next_card = random.randint(current + 1, HILO_MAX) if current < HILO_MAX else current
    elif direction == "lower" and next_card >= current:
        next_card = random.randint(HILO_MIN, current - 1) if current > HILO_MIN else current

    game["prob"] *= hilo_prob(current, direction)
    game["current"] = next_card
    game["level"] += 1

    await event.edit(hilo_text(game), buttons=hilo_keyboard(game), parse_mode="html")
    await event.answer("✅ Угадал!")


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("hl_cash:")))
async def hilo_cashout(event):
    owner_id = int(event.data.decode().split(":")[1])
    if event.sender_id != owner_id:
        await event.answer("Это не твоя игра!", alert=True)
        return
    game = hilo_games.get(owner_id)
    if not game or game["level"] <= 0:
        await event.answer("Нечего забирать.", alert=True)
        return
    mult = hilo_fair_multiplier(game["prob"])
    win_amount = int(game["bet"] * mult)
    add_balance(owner_id, win_amount)
    apply_company_economy("hilo", True, game["bet"], win_amount)
    hilo_games.pop(owner_id, None)
    await event.edit(hilo_text(game, finished=True, won=True), buttons=None, parse_mode="html")
    await event.answer(f"💰 Забрано {fmt(win_amount)} GROM!", alert=True)


# ========== ИГРА: МОНЕТКА ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^монетк?а\s+\d+\s+(орел|орёл|решка|о|р)$", norm(e.raw_text.lower().strip()))))
async def play_coin(event):
    user_id = event.sender_id
    parts = norm(event.raw_text.strip().lower()).split()
    bet = int(parts[1])
    choice = "орёл" if parts[2] in ("орел", "орёл", "о") else "решка"

    frozen, comp = is_game_frozen("монетка")
    if frozen:
        await event.reply(f"⛔ Монетка временно недоступна: компания-владелец «{comp['name']}» в долгу.")
        return

    if bet <= 0:
        await event.reply("❗ Ставка должна быть больше 0.")
        return
    if get_user_balance(user_id) < bet:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -bet)
    actual = random.choice(["орёл", "решка"])
    win = apply_chance("монетка", actual == choice)

    if win:
        payout = int(bet * 1.92)
        add_balance(user_id, payout)
        apply_company_economy("монетка", True, bet, payout)
        await event.reply(
            f"🪙 Монетка: <b>{actual}</b>\n🎉 Победа! Выигрыш: <b>{fmt(payout)} GROM</b>",
            parse_mode="html",
        )
    else:
        apply_company_economy("монетка", False, bet)
        await event.reply(
            f"🪙 Монетка: <b>{actual}</b>\n❌ Проигрыш: <b>{fmt(bet)} GROM</b>",
            parse_mode="html",
        )


# ========== СПРАВКА ==========
HELP_TEXT = (
    "📋 <b>Все команды бота</b>\n\n"
    "<b>💰 Баланс и переводы</b>\n"
    "• <code>б</code> — посмотреть баланс\n"
    "• <code>п [сумма]</code> (ответом на сообщение) — перевести GROM\n"
    "• <code>п [id] [сумма]</code> — перевод по ID\n\n"
    "<b>🎰 Рулетка</b>\n"
    "• <code>[ставка] [тип]</code> — поставить на рулетку\n"
    "  Типы: <code>к</code> красное, <code>ч</code> чёрное, <code>н</code> нечётное, <code>чт</code> чётное, число 0–36\n"
    "• <code>го</code> — запустить рулетку (≥13 сек после первой ставки)\n"
    "• <code>ставки</code> — показать свои активные ставки\n"
    "• <code>отмена</code> — отменить свои ставки\n"
    "• <code>лог</code> — история последних розыгрышей\n\n"
    "<b>💣 Мины</b>\n"
    "• <code>мины [ставка] [кол-во мин]</code> — начать игру (мин от 1 до 24, по умолчанию 5)\n\n"
    "<b>🏗 Башня</b>\n"
    "• <code>башня [ставка]</code> — поднимайся по этажам, на каждом одна из 5 дверей — ловушка, 9 этажей\n\n"
    "<b>🐸 Квак</b>\n"
    "• <code>квак [ставка]</code> — как башня, но с каждым раундом мин из 5 окошек всё больше\n\n"
    "<b>🎴 HiLo</b>\n"
    "• <code>hilo [ставка]</code> — угадывай, следующая карта больше или меньше текущей\n\n"
    "<b>🪙 Монетка</b>\n"
    "• <code>монета [ставка] орёл/решка</code> (или «монетка») — классический подброс монеты (x1.92)\n\n"
    "<b>🃏 Блэкджек</b>\n"
    "• <code>блэк [ставка]</code> — начать раздачу против дилера\n\n"
    "<b>🚀 Краш</b>\n"
    "• <code>краш [ставка] [автовывод]</code> — множитель растёт, забери выигрыш до краха. "
    "Например <code>краш 100 1.4</code> — заберёт сам на x1.4, если получится\n\n"
    "<b>⚔️ Дуэли</b>\n"
    "• <code>дуэль @игрок [сумма]</code> — вызвать на дуэль\n"
    "• <code>принять</code> — принять дуэль\n\n"
    "<b>🏰 Кланы</b>\n"
    "• <code>клан</code> — информация о вашем клане\n"
    "• <code>создать клан [название]</code> — создать клан (1000 GROM)\n"
    "• <code>вступить [название]</code> — вступить в клан\n"
    "• <code>выйти</code> — выйти из клана\n"
    "• <code>вклад [сумма]</code> — пополнить казну клана\n\n"
    "<b>🏢 Компании</b>\n"
    "• Кнопка «🏢 Компания» → «🏢 Купить компанию» — купить свою компанию (цену смотри в меню)\n"
    "• Кнопка «🏢 Компания» — баланс, игры в собственности и открытые аукционы\n"
    "• Кнопка «💰 Баланс» — быстро посмотреть баланс своей компании\n"
    "• <code>шанс [игра] [0-70]</code> — владелец компании настраивает % шанса своей игры (макс. 70)\n"
    "• <code>пополнить компанию [сумма]</code> — погасить долг компании / пополнить баланс\n"
    f"• <code>вывод компании [сумма]</code> — заявка на вывод от {fmt(COMPANY_WITHDRAW_MIN)} GROM (проверяет админ)\n"
    "• <code>стата ком</code> — какая компания чем владеет (доступно в любом чате)\n"
    "• Игры на аукцион выставляет только администратор\n\n"
    "<b>🧾 Чеки</b>\n"
    "• <code>активировать [код]</code> — активировать чек и получить GROM (коды выдаёт администратор)\n\n"
    "<b>📊 Прочее</b>\n"
    "• <code>топ</code> — таблица лидеров\n"
    "• 🎁 Бонус — 1000 GROM раз в 24 часа"
)


@client.on(events.NewMessage(func=lambda e: is_cmd(e.raw_text, "help") or (e.raw_text and e.raw_text.lower().strip() in ["помощь", "help"])))
async def cmd_help(event):
    await event.reply(HELP_TEXT, parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text == "👤 Профиль"))
async def profile_button(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    name = sender.first_name if sender else str(user_id)
    prof = get_profile(user_id)
    balance = get_user_balance(user_id)
    rank = get_user_rank(user_id)
    wins = prof["duel_wins"]
    losses = prof["duel_losses"]
    draws = prof["duel_draws"]
    total_duels = wins + losses + draws

    house_line = ""
    if prof["house"]:
        emoji, hname = next(((e, n) for e, n in HOUSES if n == prof["house"]), ("🏠", prof["house"]))
        house_line = f"\n{emoji} Факультет: **{hname}**"

    clan_line = ""
    if prof["clan_id"]:
        clan = data["clans"].get(str(prof["clan_id"]))
        if clan:
            clan_line = f"\n🏰 Клан: **{clan['name']}**"

    _, comp = get_company_by_owner(user_id)
    company_line = f"\n🏢 Компания: **{comp['name']}**" if comp else ""

    duel_line = (
        f"⚔️ Дуэли: **{total_duels}** (🏆{wins} / ❌{losses} / 🤝{draws})"
        if total_duels else "⚔️ Дуэли: нет"
    )

    await event.reply(
        f"{emoji_tag('profile', '👤')} **{name}**\n"
        f"{emoji_tag('id', '🆔')} ID: `{user_id}`{house_line}{clan_line}{company_line}\n\n"
        f"{emoji_tag('balance', '💰')} Баланс: **{fmt(balance)} GROM**\n"
        f"🏅 Место в топе: **#{rank}**\n"
        f"{duel_line}",
        parse_mode=custom_md,
    )


# ========== МЕНЮ: ХОГВАРТС ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "🔮 Хогвартс"))
async def hogwarts_button(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    name = sender.first_name if sender else str(user_id)
    prof = get_profile(user_id)

    if prof["house"]:
        emoji, hname = next(((e, n) for e, n in HOUSES if n == prof["house"]), ("🏠", prof["house"]))
        await event.reply(
            f"🔮 {mention(user_id, name)}, ты уже принадлежишь к факультету <b>{emoji} {hname}</b>!",
            parse_mode="html",
        )
        return

    emoji, hname = random.choice(HOUSES)
    prof["house"] = hname
    save_data(data)

    await event.reply(
        f"🔮 Шляпа-распределитель думает...\n\n"
        f"...{emoji} <b>{hname.upper()}!</b>\n\n"
        f"{mention(user_id, name)}, добро пожаловать на факультет <b>{hname}</b>!",
        parse_mode="html",
    )


# ========== МЕНЮ: КОМАНДЫ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "📋 Команды"))
async def commands_button(event):
    await event.reply(HELP_TEXT, parse_mode="html")


# ========== МЕНЮ: ДОНАТ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "🛒 Донат"))
async def donate_button(event):
    buttons = [
        [Button.inline(f"{fmt(grom)} GROM за {stars} Stars", data=f"buy_grom_stars:{stars}:{grom}")]
        for stars, grom in STAR_PACKS
    ]
    await event.reply(
        "💎 <b>ДОНАТ</b> 💎\n"
        "━━━━━━━━━━━━━━━\n\n"
        "За донатом пишите 👉 @Fernir_Umbra",
        buttons=buttons,
        parse_mode="html",
    )


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("buy_grom_stars:")))
async def buy_grom_stars(event):
    _, stars_s, grom_s = event.data.decode().split(":")
    stars, grom = int(stars_s), int(grom_s)

    payload = f"grom_pack:{stars}:{grom}:{event.sender_id}".encode()
    invoice = Invoice(
        currency="XTR",
        prices=[LabeledPrice(label=f"{fmt(grom)} GROM", amount=stars)],
        test=False,
    )
    media = InputMediaInvoice(
        title=f"{fmt(grom)} GROM",
        description=f"Пакет {fmt(grom)} GROM для бота GROM за {stars} Telegram Stars.",
        invoice=invoice,
        payload=payload,
        provider="",
        provider_data=DataJSON(data="{}"),
    )
    try:
        await client(SendMediaRequest(
            peer=event.sender_id,
            media=media,
            message="",
            random_id=random.getrandbits(63),
        ))
        if event.is_private:
            await event.answer()
        else:
            await event.answer("📩 Счёт отправлен тебе в личные сообщения.", alert=True)
    except Exception:
        await event.answer(
            "❗ Не получилось отправить счёт в личку — сначала напиши боту /start в личных сообщениях, потом попробуй снова.",
            alert=True,
        )


@client.on(events.Raw(UpdateBotPrecheckoutQuery))
async def stars_precheckout(update):
    try:
        await client(SetBotPrecheckoutResultsRequest(query_id=update.query_id, success=True))
    except Exception:
        pass


@client.on(events.NewMessage(func=lambda e: e.message.action and isinstance(e.message.action, MessageActionPaymentSentMe)))
async def stars_payment_success(event):
    action = event.message.action
    try:
        payload = action.payload.decode() if isinstance(action.payload, (bytes, bytearray)) else str(action.payload)
        parts = payload.split(":")
        stars = int(parts[1])
        grom = int(parts[2])
        buyer_id = int(parts[3])
    except Exception:
        stars = action.total_amount
        grom = 0
        buyer_id = event.sender_id

    if grom <= 0:
        return

    add_balance(buyer_id, grom)
    charge_id = getattr(action, "charge_id", None) or getattr(action, "telegram_payment_charge_id", None) or str(random.getrandbits(63))
    data["star_purchases"][str(charge_id)] = {
        "user_id": buyer_id,
        "stars": stars,
        "grom": grom,
        "at": datetime.now().isoformat(),
    }
    save_data(data)

    buyer_name = await get_display_name(buyer_id)
    await event.reply(
        f"✅ Оплата получена! Начислено: <b>{fmt(grom)} GROM</b> за {stars} ⭐\n"
        f"💰 Новый баланс: {fmt(get_user_balance(buyer_id))} GROM",
        parse_mode="html",
    )
    await notify_main_admins(
        f"⭐ {mention(buyer_id, buyer_name)} купил {fmt(grom)} GROM за {stars} Stars."
    )


# ========== МЕНЮ: ЧАТЫ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "💬 Чаты"))
async def chats_button(event):
    await event.reply(
        "💬 <b>НАШИ ЧАТЫ</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🎮 @gromobotchat — игровой чат\n"
        "📢 @piar_grom — пиар чат",
        buttons=[
            [Button.url("🎮 Игровой чат", "https://t.me/gromobotchat")],
            [Button.url("📢 Пиар чат", "https://t.me/piar_grom")],
        ],
        parse_mode="html",
    )


# ========== МЕНЮ: ИГРЫ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "🎮 Игры"))
async def games_button(event):
    await event.reply(
        "🎮 <b>Доступные игры</b>\n\n"
        "🎰 <b>Рулетка</b>\n"
        "Ставь GROM на цвет, чётность или число.\n"
        "Примеры: <code>100 к</code> <code>500 ч</code> <code>1000 7</code>\n"
        "Затем напиши <code>го</code> чтобы крутить.\n"
        "Выигрыш: x2 (цвет/чётность) или x35 (число)\n\n"
        "💣 <b>Мины</b>\n"
        "Открывай клетки на поле 5×5, множитель растёт с каждой безопасной клеткой.\n"
        "Команда: <code>мины 500 5</code> (ставка, кол-во мин 1-24, по умолчанию 5)\n\n"
        "🏗 <b>Башня</b>\n"
        "На каждом этаже выбери одну из 5 дверей (1 ловушка), 9 этажей.\n"
        "Команда: <code>башня 500</code>\n\n"
        "🐸 <b>Квак</b>\n"
        "Как башня, но с каждым раундом мин всё больше (5 окошек, до 4 раундов).\n"
        "Команда: <code>квак 500</code>\n\n"
        "🎴 <b>HiLo</b>\n"
        "Угадывай, будет следующая карта больше или меньше текущей.\n"
        "Команда: <code>hilo 500</code>\n\n"
        "🪙 <b>Монетка</b>\n"
        "Классический подброс — орёл или решка, выигрыш x1.92.\n"
        "Команда: <code>монета 500 орёл</code>\n\n"
        "🃏 <b>Блэкджек</b>\n"
        "Классика против дилера — набери 21 или обыграй его карты.\n"
        "Команда: <code>блэк 500</code>\n\n"
        "🚀 <b>Краш</b>\n"
        "Множитель растёт в реальном времени — жми «Забрать» до краха!\n"
        "Команда: <code>краш 500</code> или с автовыводом <code>краш 500 1.4</code>\n\n"
        "⚔️ <b>Дуэль</b>\n"
        "Вызови другого игрока на бросок кубика.\n"
        "Кто выбросил больше — забирает банк.\n"
        "Команда: <code>дуэль @username 500</code>",
        parse_mode="html",
    )


# ========== МЕНЮ: ПОЛИТИКА ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "Политика"))
async def policy_button(event):
    await event.reply(
        "📜 <b>Правила и политика</b>\n\n"
        "1. GROM — игровая валюта без реальной стоимости.\n"
        "2. Запрещено использовать ботов и скрипты для накрутки.\n"
        "3. Запрещено оскорблять других участников.\n"
        "4. Администрация вправе изменить баланс любого пользователя.\n"
        "5. Использование бота означает согласие с этими правилами.\n\n"
        "По вопросам обращайтесь к администратору.",
        parse_mode="html",
    )


# ========== МЕНЮ: ЯЗЫК ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "Изменить язык"))
async def language_button(event):
    keyboard = [[Button.inline("🇷🇺 Русский ✅", data="lang_ru"), Button.inline("🇬🇧 English", data="lang_en")]]
    await event.reply("🌐 Выберите язык / Choose language:", buttons=keyboard)


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() in ["lang_ru", "lang_en"]))
async def language_callback(event):
    lang = event.data.decode().split("_")[1]
    prof = get_profile(event.sender_id)
    prof["lang"] = lang
    save_data(data)
    if lang == "ru":
        await event.edit("🇷🇺 Язык установлен: <b>Русский</b>", parse_mode="html")
    else:
        await event.edit("🇬🇧 Language set: <b>English</b>", parse_mode="html")
    await event.answer()


# ========== КЛАНЫ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower() == "клан"))
async def clan_info(event):
    user_id = event.sender_id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None

    if not clan_id or clan_id not in data["clans"]:
        await event.reply(
            "🏰 Ты не состоишь в клане.\n\n"
            "• <code>создать клан [название]</code> — создать клан (1000 GROM)\n"
            "• <code>вступить [название]</code> — вступить в существующий клан",
            parse_mode="html",
        )
        return
    clan = data["clans"][clan_id]
    owner_name = await get_display_name(clan["owner_id"])
    await event.reply(
        f"🏰 <b>{clan['name']}</b>\n\n"
        f"👑 Лидер: {mention(clan['owner_id'], owner_name)}\n"
        f"👥 Участников: <b>{len(clan['members'])}</b>\n"
        f"💰 Казна: <b>{fmt(clan['pool'])} GROM</b>\n\n"
        f"• <code>вклад [сумма]</code> — пополнить казну\n"
        f"• <code>выйти</code> — покинуть клан",
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^создать клан\s+.+", e.raw_text.lower().strip())))
async def create_clan(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    prof = get_profile(user_id)

    if prof["clan_id"] and str(prof["clan_id"]) in data["clans"]:
        await event.reply("❌ Ты уже состоишь в клане. Сначала выйди: <code>выйти</code>", parse_mode="html")
        return

    cost = 1000
    if get_user_balance(user_id) < cost:
        await event.reply("Недостаточно GROM на балансе")
        return

    parts = event.raw_text.strip().split(maxsplit=2)
    clan_name = parts[2].strip() if len(parts) >= 3 else parts[-1].strip()

    for c in data["clans"].values():
        if c["name"].lower() == clan_name.lower():
            await event.reply("❌ Клан с таким названием уже существует.")
            return

    clan_id = str(data["next_clan_id"])
    data["next_clan_id"] += 1
    add_balance(user_id, -cost)
    data["clans"][clan_id] = {
        "name": clan_name,
        "owner_id": user_id,
        "members": [user_id],
        "pool": 0,
    }
    prof["clan_id"] = clan_id
    save_data(data)

    await event.reply(
        f"🏰 Клан <b>{clan_name}</b> создан!\n"
        f"💸 Списано {cost} GROM. Баланс: {fmt(get_user_balance(user_id))} GROM\n\n"
        f"Другие игроки могут вступить: <code>вступить {clan_name}</code>",
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^вступить\s+.+", e.raw_text.lower().strip())))
async def join_clan(event):
    user_id = event.sender_id
    prof = get_profile(user_id)

    if prof["clan_id"] and str(prof["clan_id"]) in data["clans"]:
        await event.reply("❌ Ты уже в клане. Сначала выйди: <code>выйти</code>", parse_mode="html")
        return

    parts = event.raw_text.strip().split(maxsplit=1)
    clan_name = parts[1].strip() if len(parts) >= 2 else ""

    target_clan_id = None
    for cid, c in data["clans"].items():
        if c["name"].lower() == clan_name.lower():
            target_clan_id = cid
            break

    if not target_clan_id:
        await event.reply(f"❌ Клан «{clan_name}» не найден.")
        return

    clan = data["clans"][target_clan_id]
    clan["members"].append(user_id)
    prof["clan_id"] = target_clan_id
    save_data(data)

    await event.reply(f"✅ Ты вступил в клан <b>{clan['name']}</b>!\n👥 Участников: {len(clan['members'])}", parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower().strip() == "выйти"))
async def leave_clan(event):
    user_id = event.sender_id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None

    if not clan_id or clan_id not in data["clans"]:
        await event.reply("❌ Ты не состоишь в клане.")
        return

    clan = data["clans"][clan_id]
    if clan["owner_id"] == user_id:
        await event.reply("👑 Ты лидер клана. Сначала передай лидерство или расформируй клан.")
        return

    clan["members"].remove(user_id)
    prof["clan_id"] = None
    save_data(data)
    await event.reply(f"✅ Ты вышел из клана <b>{clan['name']}</b>.", parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^вклад\s+\d+$", e.raw_text.lower().strip())))
async def clan_deposit(event):
    user_id = event.sender_id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None

    if not clan_id or clan_id not in data["clans"]:
        await event.reply("❌ Ты не состоишь в клане.")
        return

    parts = event.raw_text.strip().split()
    amount = int(parts[1])
    if amount <= 0:
        await event.reply("❌ Сумма должна быть больше 0.")
        return
    if get_user_balance(user_id) < amount:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -amount)
    clan = data["clans"][clan_id]
    clan["pool"] += amount
    save_data(data)

    await event.reply(
        f"✅ Ты внёс <b>{fmt(amount)} GROM</b> в казну клана <b>{clan['name']}</b>.\n"
        f"💰 Казна: {fmt(clan['pool'])} GROM | Твой баланс: {fmt(get_user_balance(user_id))} GROM",
        parse_mode="html",
    )


# ========== КОМПАНИИ ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text == "🏢 Компания"))
async def company_button(event):
    user_id = event.sender_id
    cid, comp = get_company_by_owner(user_id)

    if not comp:
        await event.reply(
            f"{emoji_tag('companies', '🏢')} У тебя пока нет компании.\n\n"
            f"{emoji_tag('balance', '💰')} Цена: **{fmt(get_company_price())} GROM**\n\n"
            f"Владелец компании может участвовать в аукционах игр — их выставляет администратор.",
            buttons=[[Button.inline("🏢 Купить компанию", data="start_buy_company")]],
            parse_mode=custom_md,
        )
        return

    owned = [g for g, ccid in data["game_owner"].items() if ccid == cid]
    games_text = "\n".join(f"• {GAME_LABELS.get(g, g)}" for g in owned) if owned else "Пока нет игр в собственности"

    auctions = {aid: a for aid, a in data["game_auctions"].items() if a["status"] == "open"}
    buttons = [[Button.inline("💰 Баланс", data="company_balance")]]
    for aid, a in auctions.items():
        label = GAME_LABELS.get(a["game"], a["game"])
        buttons.append([Button.inline(f"Купить {label} — {fmt(a['price'])} GROM", data=f"buy_auction:{aid}")])

    frozen_line = ""
    if comp.get("frozen"):
        deadline = datetime.fromisoformat(comp["frozen_until"])
        left = deadline - datetime.now()
        hours_left = max(0, left.total_seconds()) / 3600
        frozen_line = (
            f"\n\n⛔ **Компания заморожена!** Долг: {fmt(-comp['balance'])} GROM.\n"
            f"Осталось на оплату: ~{hours_left:.1f} ч. Иначе игры будут изъяты."
        )

    await event.reply(
        f"{emoji_tag('companies', '🏢')} **{comp['name']}**\n\n"
        f"{emoji_tag('balance', '💰')} Баланс компании: **{fmt(comp['balance'])} GROM**\n\n"
        f"🎮 **Игры в собственности:**\n{games_text}\n\n"
        f"📤 **Открытые аукционы игр:**" + ("" if auctions else " нет") + frozen_line,
        buttons=buttons,
        parse_mode=custom_md,
    )


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "company_balance"))
async def company_balance_button(event):
    _, comp = get_company_by_owner(event.sender_id)
    if not comp:
        await event.answer("У тебя нет компании.", alert=True)
        return
    await event.answer(f"💰 Баланс компании «{comp['name']}»: {fmt(comp['balance'])} GROM", alert=True)


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^пополнить компани[юи]\s+\d+$", norm(e.raw_text.lower().strip()))))
async def company_topup(event):
    user_id = event.sender_id
    cid, comp = get_company_by_owner(user_id)
    if not comp:
        await event.reply("❌ У тебя нет компании.")
        return

    amount = int(norm(event.raw_text.strip()).split()[-1])
    if amount <= 0:
        await event.reply("❗ Сумма должна быть больше 0.")
        return
    if get_user_balance(user_id) < amount:
        await event.reply("Недостаточно GROM на балансе")
        return

    add_balance(user_id, -amount)
    comp["balance"] += amount
    save_data(data)
    check_company_debt(cid, comp)

    await event.reply(
        f"✅ Пополнено на <b>{fmt(amount)} GROM</b>.\n"
        f"💰 Новый баланс компании: {fmt(comp['balance'])} GROM",
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^вывод компании\s+\d+$", e.raw_text.lower().strip())))
async def company_withdraw_request(event):
    user_id = event.sender_id
    cid, comp = get_company_by_owner(user_id)
    if not comp:
        await event.reply("❌ У тебя нет компании.")
        return

    amount = int(event.raw_text.strip().split()[-1])
    if amount < COMPANY_WITHDRAW_MIN:
        await event.reply(f"❗ Минимальная сумма для вывода — {fmt(COMPANY_WITHDRAW_MIN)} GROM.")
        return
    if comp["balance"] < amount:
        await event.reply("❌ На балансе компании недостаточно средств.")
        return

    wid = str(data["next_withdrawal_id"])
    data["next_withdrawal_id"] += 1
    data["company_withdrawals"][wid] = {
        "company_id": cid,
        "user_id": user_id,
        "amount": amount,
        "status": "pending",
    }
    save_data(data)

    await event.reply(
        f"📨 Заявка на вывод <b>{fmt(amount)} GROM</b> отправлена администратору на проверку.",
        parse_mode="html",
    )
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(
                admin_id,
                f"📨 <b>Новая заявка на вывод компании</b>\n"
                f"Компания: {comp['name']} (#{cid})\n"
                f"Владелец: {mention(user_id, await get_display_name(user_id))}\n"
                f"Сумма: {fmt(amount)} GROM\n"
                f"Заявка: #{wid}\n\n"
                f"Открой /adm → «🏢 Заявки на вывод» для решения.",
                parse_mode="html",
            )
        except Exception:
            pass


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode() == "start_buy_company"))
async def start_buy_company(event):
    user_id = event.sender_id
    _, comp = get_company_by_owner(user_id)
    if comp:
        await event.answer("❌ У тебя уже есть компания.", alert=True)
        return
    if get_user_balance(user_id) < get_company_price():
        await event.answer("Недостаточно GROM на балансе.", alert=True)
        return

    waiting_for_company_name[user_id] = True
    await event.answer()
    await event.reply(
        "✏️ <b>Введите название компании</b>\n\nПросто отправьте следующим сообщением название вашей будущей компании.",
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and waiting_for_company_name.get(e.sender_id)))
async def buy_company_set_name(event):
    user_id = event.sender_id
    text = event.raw_text.strip()

    if text.startswith("/"):
        return

    waiting_for_company_name.pop(user_id, None)

    _, comp = get_company_by_owner(user_id)
    if comp:
        await event.reply("❌ У тебя уже есть компания.")
        return
    price = get_company_price()
    if get_user_balance(user_id) < price:
        await event.reply("Недостаточно GROM на балансе")
        return

    name = text
    for c in data["companies"].values():
        if c["name"].lower() == name.lower():
            await event.reply(
                "❌ Компания с таким названием уже существует.\n"
                "Нажмите «🏢 Компания» → «Купить компанию» и попробуйте снова."
            )
            return

    add_balance(user_id, -price)
    new_id = str(data["next_company_id"])
    data["next_company_id"] += 1
    data["companies"][new_id] = {
        "name": name,
        "owner_id": user_id,
        "balance": 0,
        "frozen": False,
        "frozen_until": None,
    }
    save_data(data)

    await event.reply(
        f"🏢 Компания <b>{name}</b> куплена за {fmt(price)} GROM!\n"
        f"Теперь тебе доступны аукционы игр — кнопка «🏢 Компания».",
        parse_mode="html",
    )


# ========== ЧЕКИ (ваучеры) ==========
@client.on(events.NewMessage(func=lambda e: e.raw_text and re.match(r"^активировать\s+\S+$", norm(e.raw_text.lower().strip()))))
async def activate_check(event):
    user_id = event.sender_id
    code_key = norm(event.raw_text.strip()).lower().split(maxsplit=1)[1]
    check = data["checks"].get(code_key)

    if not check:
        await event.reply("❌ Чек с таким кодом не найден.")
        return
    if user_id in check["used_by"]:
        await event.reply("❌ Ты уже активировал этот чек.")
        return
    if check["activations_left"] <= 0:
        await event.reply("❌ Лимит активаций этого чека исчерпан.")
        return

    check["activations_left"] -= 1
    check["used_by"].append(user_id)
    add_balance(user_id, check["amount"])
    save_data(data)

    await event.reply(
        f"✅ Чек активирован! Начислено: <b>{fmt(check['amount'])} GROM</b>\n"
        f"💰 Новый баланс: {fmt(get_user_balance(user_id))} GROM\n"
        f"🔢 Осталось активаций: {check['activations_left']}/{check['activations_total']}",
        parse_mode="html",
    )


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("delcheck:")))
async def delete_check(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return
    code_key = event.data.decode().split(":", 1)[1]
    check = data["checks"].pop(code_key, None)
    save_data(data)
    if not check:
        await event.answer("Чек уже удалён.", alert=True)
        return
    await event.edit(f"🗑 Чек «{check['display_code']}» удалён.")
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and (e.data.decode().startswith("wd_ok:") or e.data.decode().startswith("wd_no:"))))
async def handle_withdrawal_decision(event):
    if admin_rank(event.sender_id) < RANK_SENIOR:
        await event.answer("Доступ запрещён", alert=True)
        return

    action, wid = event.data.decode().split(":", 1)
    w = data["company_withdrawals"].get(wid)
    if not w or w["status"] != "pending":
        await event.answer("Заявка уже обработана.", alert=True)
        return

    comp = data["companies"].get(w["company_id"])
    if not comp:
        await event.answer("Компания не найдена.", alert=True)
        return

    if action == "wd_ok":
        if comp["balance"] < w["amount"]:
            await event.answer("У компании уже недостаточно средств.", alert=True)
            return
        comp["balance"] -= w["amount"]
        add_balance(w["user_id"], w["amount"])
        w["status"] = "approved"
        save_data(data)
        check_company_debt(w["company_id"], comp)
        await event.edit(f"✅ Заявка #{wid} одобрена. {fmt(w['amount'])} GROM выведено на баланс игрока.")
        try:
            await client.send_message(
                w["user_id"],
                f"✅ Твоя заявка на вывод <b>{fmt(w['amount'])} GROM</b> из компании «{comp['name']}» одобрена.\n"
                f"💰 Новый баланс: {fmt(get_user_balance(w['user_id']))} GROM",
                parse_mode="html",
            )
        except Exception:
            pass
    else:
        w["status"] = "rejected"
        save_data(data)
        await event.edit(f"❌ Заявка #{wid} отклонена.")
        try:
            await client.send_message(
                w["user_id"],
                f"❌ Твоя заявка на вывод {fmt(w['amount'])} GROM из компании «{comp['name']}» отклонена администратором.",
                parse_mode="html",
            )
        except Exception:
            pass
    await event.answer()


@client.on(events.CallbackQuery(func=lambda e: e.data and e.data.decode().startswith("buy_auction:")))
async def buy_auction(event):
    aid = event.data.decode().split(":", 1)[1]
    auction = data["game_auctions"].get(aid)
    if not auction or auction["status"] != "open":
        await event.answer("Аукцион недоступен.", alert=True)
        return

    cid, comp = get_company_by_owner(event.sender_id)
    if not comp:
        await event.answer("У тебя нет компании.", alert=True)
        return
    if get_user_balance(event.sender_id) < auction["price"]:
        await event.answer("Недостаточно GROM на балансе.", alert=True)
        return

    add_balance(event.sender_id, -auction["price"])
    data["game_owner"][auction["game"]] = cid
    auction["status"] = "sold"
    save_data(data)

    label = GAME_LABELS.get(auction["game"], auction["game"])
    await event.answer(f"✅ Игра {label} куплена!", alert=True)
    await event.edit(
        f"✅ Игра <b>{label}</b> продана компании <b>{comp['name']}</b>.",
        buttons=None,
        parse_mode="html",
    )


@client.on(events.NewMessage(func=lambda e: e.raw_text and e.raw_text.lower().strip() == "стата ком"))
async def companies_public_stat(event):
    if not data["companies"]:
        await event.reply("🏢 Пока нет ни одной компании.")
        return

    lines = ["🏢 <b>Компании и их игры</b>\n"]
    for cid, c in data["companies"].items():
        owned = [GAME_LABELS.get(g, g) for g, ccid in data["game_owner"].items() if ccid == cid]
        owner_name = await get_display_name(c["owner_id"])
        games_text = ", ".join(owned) if owned else "нет игр"
        lines.append(f"🏢 <b>{c['name']}</b> — {mention(c['owner_id'], owner_name)}\nИгры: {games_text}")

    await event.reply("\n\n".join(lines), parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.raw_text and (e.raw_text == "🎁 Бонус" or norm(e.raw_text.lower().strip()) == "бонус")))
async def bonus_button(event):
    user_id = event.sender_id

    if not event.is_private:
        me = await client.get_me()
        await event.reply(
            "❗ Бонус можно забрать только в ЛС бота!",
            buttons=[[Button.url("🎁 Забрать бонус", f"https://t.me/{me.username}?start=bonus")]],
        )
        return

    can, remaining = can_take_bonus(user_id)
    if can:
        add_balance(user_id, 1000)
        set_bonus_taken(user_id)
        await event.reply(f"🎁 Вам начислено: 1 000 GROM\n💰 Новый баланс: {fmt(get_user_balance(user_id))}")
    else:
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        await event.reply(f"⏳ Осталось подождать {hours:02d}:{minutes:02d} до следующего бонуса")


# ========== ЗАПУСК =========
async def send_chunks(chat_id: int, text: str, parse_mode: str = "html", buttons=None):
    MAX = 4096
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = current + "\n" + line if current else line
        if len(candidate) > MAX:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        await client.send_message(
            chat_id, chunk, parse_mode=parse_mode, buttons=buttons if is_last else None
        )


async def send_roulette_result(chat_id, result_num, color_symbol, bet_results, names, per_user):
    lines = [f"Рулетка: {result_num}{color_symbol}"]
    winners = []
    for user_id, amount, bet_type, bet_value, won, change in bet_results:
        link = mention(user_id, names[user_id])
        label = bet_label_str(bet_type, bet_value)
        lines.append(f"{link} {fmt(amount)} GROM на {label}")
        if won:
            win_amount = change + amount
            winners.append(f"{link} ставка {fmt(amount)} GROM выиграл {fmt(win_amount)} на {label}")

    if winners:
        lines.append("")
        lines.extend(winners)

    buttons = [[
        Button.inline("Повторить", data=f"rpt:{chat_id}"),
        Button.inline("Удвоить", data=f"dbl:{chat_id}"),
    ]] if per_user else None

    await send_chunks(chat_id, "\n".join(lines), buttons=buttons)


async def main():
    await client.start(bot_token=TOKEN)
    print("Бот запущен!")
    asyncio.create_task(company_debt_watcher())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
