#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raffle Telegram Bot + embedded Telegram Mini App.
Single-file starter project. Python 3.10+
Install: python bot.py
Required environment:
  BOT_TOKEN=...
  MAIN_ADMIN_ID=123456789
  WEBAPP_URL=https://your-domain.example
Optional:
  TON_WALLET=EQ...
  TONCENTER_API_KEY=...
  TON_API_BASE=https://toncenter.com/api/v2
  CARD_PAYMENT_NOTE=...
  WEB_HOST=0.0.0.0
  WEB_PORT=8080

Important: TON watcher is a wallet-activity monitor only. It does NOT sell or
unlock digital Telegram bot/Mini App access. Telegram requires Stars for
digital goods/services sold inside bots and Mini Apps. This starter uses
admin-granted 30-day access for testing/management.
"""

import os
import sys
import subprocess
import importlib.util

REQUIRED = {"aiogram": "aiogram>=3.4,<4", "aiohttp": "aiohttp>=3.9,<4"}
missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
if missing:
    print("Installing dependencies:", ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

import asyncio
import hashlib
import hmac
import html
import json
import logging
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, ForceReply
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- CONFIG ----------------
# Reads a simple .env file from the same folder; real process environment wins.
def load_dotenv_file(path=".env"):
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError as exc:
        print(f"Could not read .env: {exc}")

load_dotenv_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# You can either set MAIN_ADMIN_ID in .env or replace 123456789 below.
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "123456789") or 0)
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "raffle_bot.sqlite3")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
TON_WALLET = os.getenv("TON_WALLET", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
TON_API_BASE = os.getenv("TON_API_BASE", "https://toncenter.com/api/v2").rstrip("/")
CARD_PAYMENT_NOTE = os.getenv("CARD_PAYMENT_NOTE", "Оплата картой согласуется с администратором.")
TON_POLL_SECONDS = int(os.getenv("TON_POLL_SECONDS", "25"))

if not BOT_TOKEN or MAIN_ADMIN_ID <= 0:
    raise SystemExit("Set BOT_TOKEN and a valid MAIN_ADMIN_ID in .env or at the top of bot.py.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rafflebot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db = None
ton_last_lt = {}
pending_admin_ops = {}

# ---------------- DATABASE ----------------
import sqlite3

def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with connect_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          joined_at TEXT NOT NULL, subscription_until TEXT
        );
        CREATE TABLE IF NOT EXISTS admins(
          user_id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('main','admin')),
          added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats(
          chat_id INTEGER PRIMARY KEY, title TEXT, chat_type TEXT, added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS raffles(
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
          creator_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          winner_id INTEGER, created_at TEXT NOT NULL, ends_at TEXT
        );
        CREATE TABLE IF NOT EXISTS entries(
          raffle_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
          value TEXT, created_at TEXT NOT NULL,
          PRIMARY KEY(raffle_id,user_id),
          FOREIGN KEY(raffle_id) REFERENCES raffles(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS templates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
          name TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blacklist(
          user_id INTEGER PRIMARY KEY, reason TEXT, added_by INTEGER, added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gifts_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          gift TEXT NOT NULL, admin_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ton_seen(
          tx_hash TEXT PRIMARY KEY, amount_nano TEXT, from_addr TEXT,
          comment TEXT, seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          method TEXT NOT NULL, amount TEXT, status TEXT NOT NULL,
          proof TEXT, created_at TEXT NOT NULL, reviewed_by INTEGER
        );
        """)
        c.execute("""INSERT INTO admins(user_id,role,added_at) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET role='main'""",
                  (MAIN_ADMIN_ID, "main", now_iso()))
        # Keep exactly one main administrator: the ID configured above.
        c.execute("DELETE FROM admins WHERE role='main' AND user_id<>?", (MAIN_ADMIN_ID,))
        raffle_cols = {row["name"] for row in c.execute("PRAGMA table_info(raffles)").fetchall()}
        if "ends_at" not in raffle_cols:
            c.execute("ALTER TABLE raffles ADD COLUMN ends_at TEXT")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ton_wallet',?)", (TON_WALLET,))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('card_note',?)", (CARD_PAYMENT_NOTE,))

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def is_admin(uid):
    if int(uid) == MAIN_ADMIN_ID:
        return True
    with connect_db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=? AND role='admin'", (uid,)).fetchone() is not None

def is_main(uid):
    return int(uid) == MAIN_ADMIN_ID

def active_access(uid):
    with connect_db() as c:
        r = c.execute("SELECT subscription_until FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r or not r["subscription_until"]:
        return False
    try:
        return datetime.fromisoformat(r["subscription_until"]) > datetime.now(timezone.utc)
    except Exception:
        return False

def set_access(uid, days=30):
    if not 1 <= int(days) <= 3650:
        raise ValueError("Срок доступа должен быть от 1 до 3650 дней.")
    until = datetime.now(timezone.utc) + timedelta(days=int(days))
    with connect_db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,joined_at) VALUES(?,?)", (uid, now_iso()))
        c.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (until.isoformat(), uid))
    return until

def get_setting(key, default=""):
    with connect_db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key, value):
    with connect_db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

# ---------------- KEYBOARDS ----------------
def main_menu(uid):
    rows = [
        [KeyboardButton(text="🎁 Розыгрыши"), KeyboardButton(text="📊 Моя подписка")],
        [KeyboardButton(text="🧩 Mini App", web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL.startswith("https://") else [KeyboardButton(text="🧩 Mini App")],
        [KeyboardButton(text="🛠 Админ-панель")] if is_admin(uid) else [KeyboardButton(text="💳 Способы оплаты")]
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def admin_kb(uid):
    b = InlineKeyboardBuilder()
    b.button(text="➕ Выдать 30 дней", callback_data="adm_grant_help")
    b.button(text="👥 Администраторы", callback_data="adm_admins")
    b.button(text="🎁 Создать розыгрыш", callback_data="adm_create_help")
    b.button(text="📈 Статистика", callback_data="adm_stats")
    b.button(text="🧪 Проверить TON", callback_data="adm_ton_test")
    if is_main(uid):
        b.button(text="💎 Настроить TON-кошелёк", callback_data="adm_ton_wallet")
        b.button(text="➕ Добавить младшего админа", callback_data="adm_add_help")
        b.button(text="➖ Удалить младшего админа", callback_data="adm_del_help")
    b.adjust(2, 2, 1, 2)
    return b.as_markup()

# ---------------- HELPERS ----------------
async def remember_user(user):
    with connect_db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,joined_at)
          VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name""",
          (user.id, user.username or "", user.first_name or "", now_iso()))

async def notify_admins(text):
    with connect_db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM admins").fetchall()]
    for uid in ids:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass

async def ensure_chat(chat):
    with connect_db() as c:
        c.execute("""INSERT INTO chats(chat_id,title,chat_type,added_at) VALUES(?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,chat_type=excluded.chat_type""",
        (chat.id, chat.title or "", str(chat.type), now_iso()))

def get_raffle(rid):
    with connect_db() as c:
        return c.execute("SELECT * FROM raffles WHERE id=?", (rid,)).fetchone()

async def create_raffle(chat_id, creator_id, kind, title, payload):
    duration = payload.get("duration_minutes")
    ends_at = None
    if duration is not None:
        duration = int(duration)
        if not 1 <= duration <= 43200:
            raise ValueError("Длительность должна быть от 1 до 43200 минут.")
        ends_at = (datetime.now(timezone.utc) + timedelta(minutes=duration)).isoformat()
    with connect_db() as c:
        cur = c.execute("""INSERT INTO raffles(chat_id,creator_id,kind,title,payload,created_at,ends_at)
        VALUES(?,?,?,?,?,?,?)""", (chat_id, creator_id, kind, title,
        json.dumps(payload, ensure_ascii=False), now_iso(), ends_at))
        return cur.lastrowid

async def post_raffle(chat_id, rid):
    r = get_raffle(rid)
    if not r:
        return
    p = json.loads(r["payload"])
    safe_title = html.escape(str(r["title"]))
    safe_desc = html.escape(str(p.get("description", "")))
    text = f"🎉 <b>{safe_title}</b>\n\n{safe_desc}\n\n"
    text += f"Участников: 0\nID розыгрыша: <code>{rid}</code>\n"
    if r["ends_at"]:
        text += f"Окончание: {html.escape(r['ends_at'])}\n"
    text += "Участвуй: нажми кнопку ниже или отправь условие в чат."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Участвовать", callback_data=f"join:{rid}")]
    ])
    await bot.send_message(chat_id, text, reply_markup=kb)

# ---------------- USER COMMANDS ----------------
@dp.message(CommandStart())
async def start(m: Message):
    await remember_user(m.from_user)
    if is_admin(m.from_user.id):
        await m.answer("Привет! Панель управления розыгрышами готова.", reply_markup=main_menu(m.from_user.id))
    elif active_access(m.from_user.id):
        await m.answer("Доступ активен. Открой Mini App или выбери нужный раздел.", reply_markup=main_menu(m.from_user.id))
    else:
        await m.answer(
            "Привет! Это бот розыгрышей.\n\n"
            "Доступ к функциям выдаётся на 30 дней. Для тестирования доступ может "
            "выдать администратор. Покупка цифрового доступа через TON/карту внутри "
            "Telegram здесь не реализована из-за правил Telegram.",
            reply_markup=main_menu(m.from_user.id)
        )

@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>\nID чата: <code>{m.chat.id}</code>")

@dp.message(Command("grant"))
async def cmd_grant(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Использование: /grant USER_ID [дни]. По умолчанию 30.")
        return
    uid = int(parts[1])
    if len(parts) > 2 and not parts[2].isdigit():
        await m.answer("Количество дней должно быть положительным числом.")
        return
    days = int(parts[2]) if len(parts) > 2 else 30
    try:
        until = set_access(uid, days)
    except ValueError as e:
        await m.answer(str(e)); return
    await m.answer(f"Доступ выдан до {until.strftime('%d.%m.%Y %H:%M UTC')}.")
    try:
        await bot.send_message(uid, f"✅ Вам выдан доступ на {days} дней.")
    except Exception:
        pass

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("Админ-панель:", reply_markup=admin_kb(m.from_user.id))
    else:
        await m.answer("Нет доступа.")

@dp.message(Command("create"))
async def cmd_create(m: Message):
    if not is_admin(m.from_user.id):
        return
    # /create CHAT_ID kind title | description
    raw = (m.text or "").partition(" ")[2]
    try:
        parts = raw.split("|")
        if len(parts) < 2:
            raise ValueError()
        chat_s, kind, title = parts[0].strip().split(" ", 2)
        chat_id = int(chat_s)
        if kind not in {"random", "777", "guess", "phrase"}:
            raise ValueError()
        desc = parts[1].strip()[:3000]
        if not title.strip():
            raise ValueError("Укажите название.")
        title = title.strip()[:100]
        payload = {"description": desc, "duration_minutes": 60}
        if len(parts) >= 3 and parts[2].strip():
            payload["duration_minutes"] = int(parts[2].strip())
        if kind == "guess":
            if len(parts) < 4 or not parts[3].strip().isdigit():
                raise ValueError("Для guess после длительности укажите число.")
            payload["number"] = parts[3].strip()
        if kind == "phrase":
            if len(parts) < 4 or not parts[3].strip():
                raise ValueError("Для phrase после длительности укажите фразу.")
            payload["phrase"] = parts[3].strip()[:200]
    except Exception:
        await m.answer(
            "Формат:\n/create CHAT_ID random Название | Описание | Длительность_в_минутах\n"
            "Например: /create -1001234567890 random Розыгрыш | Условия | 60\n"
            "Для угадайки: /create CHAT_ID guess Название | Условия | 60 | 42\n"
            "Для фразы: /create CHAT_ID phrase Название | Условия | 60 | секрет\n"
            "Поддерживаются: random, 777, guess, phrase. Сначала добавьте бота в чат и выдайте права."
        )
        return
    try:
        rid = await create_raffle(chat_id, m.from_user.id, kind, title, payload)
    except Exception as e:
        await m.answer(f"Не удалось создать розыгрыш: {e}")
        return
    try:
        await post_raffle(chat_id, rid)
        await m.answer(f"Розыгрыш #{rid} опубликован.")
    except Exception as e:
        await m.answer(f"Не удалось опубликовать. Проверьте ID чата и права бота.\n{e}")

@dp.message(F.new_chat_members)
async def bot_added(m: Message):
    for member in m.new_chat_members:
        if member.id == (await bot.get_me()).id:
            await ensure_chat(m.chat)
            await m.answer("Спасибо за добавление! Выдайте мне права администратора для публикации розыгрышей и работы с каналом.")

@dp.my_chat_member()
async def bot_chat_member(update):
    await ensure_chat(update.chat)
    try:
        await bot.send_message(update.chat.id, "Я готов проводить розыгрыши! Выдайте необходимые права администратора.")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("join:"))
async def join_raffle(q: CallbackQuery):
    rid = int(q.data.split(":")[1])
    r = get_raffle(rid)
    if not r or r["status"] != "active":
        await q.answer("Розыгрыш уже завершён.", show_alert=True); return
    with connect_db() as c:
        if c.execute("SELECT 1 FROM blacklist WHERE user_id=?", (q.from_user.id,)).fetchone():
            await q.answer("Вы не можете участвовать.", show_alert=True); return
        cur = c.execute("INSERT OR IGNORE INTO entries(raffle_id,user_id,username,value,created_at) VALUES(?,?,?,?,?)",
                        (rid, q.from_user.id, q.from_user.username or "", "", now_iso()))
        n = c.execute("SELECT COUNT(*) n FROM entries WHERE raffle_id=?", (rid,)).fetchone()["n"]
    await remember_user(q.from_user)
    await q.answer("Вы участвуете!" if cur.rowcount else "Вы уже участвуете!")
    try:
        await q.message.edit_text(
            q.message.text.split("Участников:")[0] + f"Участников: {n}\nID розыгрыша: <code>{rid}</code>\nУчаствуй кнопкой ниже.",
            reply_markup=q.message.reply_markup
        )
    except Exception:
        pass

@dp.message(F.reply_to_message, F.text)
async def admin_prompt_reply(m: Message):
    uid = m.from_user.id if m.from_user else 0
    action = pending_admin_ops.get(uid)
    if not action:
        return
    if not is_main(uid):
        pending_admin_ops.pop(uid, None)
        return
    raw_id = (m.text or "").strip()
    if not raw_id.isdigit():
        await m.answer("Нужен числовой Telegram ID. Откройте админ-меню и повторите действие.")
        pending_admin_ops.pop(uid, None)
        return
    target = int(raw_id)
    if target == MAIN_ADMIN_ID:
        await m.answer("Этот пользователь уже главный администратор.")
    else:
        with connect_db() as c:
            if action == "add":
                c.execute("INSERT INTO admins(user_id,role,added_at) VALUES(?,?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET role='admin', added_at=excluded.added_at",
                          (target, "admin", now_iso()))
                await m.answer(f"✅ Младший администратор {target} добавлен.")
    pending_admin_ops.pop(uid, None)

@dp.message(F.text)
async def text_router(m: Message):
    if m.from_user:
        await remember_user(m.from_user)
    if m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await ensure_chat(m.chat)
        text = (m.text or "").strip()
        if not m.from_user or m.from_user.is_bot:
            return
        with connect_db() as c:
            if c.execute("SELECT 1 FROM blacklist WHERE user_id=?", (m.from_user.id,)).fetchone():
                return
            active = c.execute("SELECT * FROM raffles WHERE chat_id=? AND status='active'", (m.chat.id,)).fetchall()
            winners = []
            for r in active:
                p = json.loads(r["payload"])
                kind = r["kind"]
                matched = ((kind == "777" and text == "777") or
                           (kind == "phrase" and bool(p.get("phrase")) and text.casefold() == str(p["phrase"]).casefold()) or
                           (kind == "guess" and bool(p.get("number")) and text == str(p["number"])))
                if matched:
                    cur = c.execute("UPDATE raffles SET status='finished',winner_id=? WHERE id=? AND status='active'",
                                    (m.from_user.id, r["id"]))
                    if cur.rowcount:
                        c.execute("INSERT OR IGNORE INTO entries(raffle_id,user_id,username,value,created_at) VALUES(?,?,?,?,?)",
                                  (r["id"], m.from_user.id, m.from_user.username or "", text, now_iso()))
                        winners.append((r["id"], r["title"]))
        for rid, title in winners:
            await bot.send_message(m.chat.id,
                f"🏆 Победитель розыгрыша <b>{html.escape(str(title))}</b>: {m.from_user.mention_html()}")
        return
    t = (m.text or "").strip()
    if t == "📊 Моя подписка":
        with connect_db() as c:
            row = c.execute("SELECT subscription_until FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
        until = row["subscription_until"] if row else None
        await m.answer(f"Подписка: {'активна до ' + until if active_access(m.from_user.id) else 'не активна'}.")
    elif t == "🎁 Розыгрыши":
        with connect_db() as c:
            rows = c.execute("SELECT id,title,kind,status FROM raffles ORDER BY id DESC LIMIT 10").fetchall()
        await m.answer("Последние розыгрыши:\n" + ("\n".join(f"#{r['id']} — {r['title']} ({r['status']})" for r in rows) if rows else "Пока нет."))
    elif t == "🛠 Админ-панель":
        if is_admin(m.from_user.id):
            await m.answer("Админ-панель:", reply_markup=admin_kb(m.from_user.id))
    elif t == "💳 Способы оплаты":
        note = html.escape(get_setting("card_note", CARD_PAYMENT_NOTE))
        await m.answer("Покупка цифрового доступа автоматически не подключена.\n\n" + note + "\n\nДля тестового доступа обратитесь к администратору.")
    elif t == "🧩 Mini App":
        if WEBAPP_URL.startswith("https://"):
            await m.answer("Откройте Mini App через кнопку меню.", reply_markup=main_menu(m.from_user.id))
        else:
            await m.answer("Mini App URL пока не настроен. Укажите WEBAPP_URL с HTTPS.")

@dp.message(Command("finish"))
async def finish_raffle_cmd(m: Message):
    if not is_admin(m.from_user.id):
        return
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        await m.answer("Использование: /finish RAFFLE_ID")
        return
    rid = int(p[1])
    r = get_raffle(rid)
    if not r or r["status"] != "active":
        await m.answer("Активный розыгрыш с таким ID не найден.")
        return
    await finish_raffle(rid, reason="Завершён администратором")
    await m.answer(f"Розыгрыш #{rid} завершён.")

async def finish_raffle(rid, reason="Время вышло"):
    with connect_db() as c:
        r = c.execute("SELECT * FROM raffles WHERE id=? AND status='active'", (rid,)).fetchone()
        if not r:
            return False
        entries = c.execute("SELECT user_id,username FROM entries WHERE raffle_id=?", (rid,)).fetchall()
        winner = random.choice(entries) if entries else None
        c.execute("UPDATE raffles SET status='finished',winner_id=? WHERE id=? AND status='active'",
                  (winner["user_id"] if winner else None, rid))
    if winner:
        label = f"🏆 Победитель розыгрыша <b>{html.escape(str(r['title']))}</b>: "
        label += f"<a href='tg://user?id={winner['user_id']}'>{html.escape(winner['username'] or str(winner['user_id']))}</a>"
    else:
        label = f"Розыгрыш <b>{html.escape(str(r['title']))}</b> завершён без участников."
    await bot.send_message(r["chat_id"], label + f"\n<i>{html.escape(reason)}</i>")
    return True

async def raffle_timer():
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with connect_db() as c:
                rows = c.execute("SELECT id FROM raffles WHERE status='active' AND ends_at IS NOT NULL AND ends_at<=?",
                                 (now,)).fetchall()
            for row in rows:
                try:
                    await finish_raffle(row["id"], "Время розыгрыша истекло")
                except Exception as exc:
                    log.warning("Could not finish raffle %s: %s", row["id"], exc)
        except Exception as exc:
            log.exception("Raffle timer error: %s", exc)
        await asyncio.sleep(5)

# ---------------- ADMIN CALLBACKS ----------------
@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(q: CallbackQuery):
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("Нет доступа", show_alert=True); return
    action = q.data
    if action == "adm_grant_help":
        await q.message.answer("Выдать доступ на 30 дней:\n/grant USER_ID\nМожно указать срок: /grant USER_ID 30")
    elif action == "adm_create_help":
        await q.message.answer(
            "Создание розыгрыша:\n"
            "/create CHAT_ID random Название | Описание | 60\n"
            "/create CHAT_ID 777 Название | Описание | 60\n"
            "/create CHAT_ID guess Название | Описание | 60 | 42\n"
            "/create CHAT_ID phrase Название | Описание | 60 | секрет\n\n"
            "Длительность указывается в минутах. По умолчанию — 60."
        )
    elif action == "adm_add_help" and is_main(uid):
        pending_admin_ops[uid] = "add"
        await q.message.answer("Отправьте Telegram ID нового младшего администратора:", reply_markup=ForceReply(selective=True))
    elif action == "adm_del_help" and is_main(uid):
        with connect_db() as c:
            rows = c.execute("SELECT user_id FROM admins WHERE role='admin' ORDER BY user_id").fetchall()
        if not rows:
            await q.message.answer("Младших администраторов пока нет.")
        else:
            kb = InlineKeyboardBuilder()
            for row in rows:
                kb.button(text=f"Удалить {row['user_id']}", callback_data=f"adm_remove_{row['user_id']}")
            kb.adjust(1)
            await q.message.answer("Выберите младшего администратора для удаления:", reply_markup=kb.as_markup())
    elif action.startswith("adm_remove_") and is_main(uid):
        try:
            target = int(action.rsplit("_", 1)[1])
            if target == MAIN_ADMIN_ID:
                raise ValueError()
            with connect_db() as c:
                c.execute("DELETE FROM admins WHERE user_id=? AND role='admin'", (target,))
            await q.message.answer(f"Младший администратор {target} удалён (если он существовал).")
        except Exception:
            await q.message.answer("Не удалось удалить администратора.")
    elif action == "adm_admins":
        with connect_db() as c:
            rows = c.execute("SELECT user_id,role FROM admins ORDER BY CASE role WHEN 'main' THEN 0 ELSE 1 END,user_id").fetchall()
        await q.message.answer("Администраторы:\n" + "\n".join(
            f"{r['user_id']} — {'главный администратор' if r['role']=='main' else 'младший администратор'}"
            for r in rows))
    elif action == "adm_stats":
        with connect_db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            chats = c.execute("SELECT COUNT(*) n FROM chats").fetchone()["n"]
            raffles = c.execute("SELECT COUNT(*) n FROM raffles").fetchone()["n"]
            active = c.execute("SELECT COUNT(*) n FROM raffles WHERE status='active'").fetchone()["n"]
        await q.message.answer(f"📊 Статистика\nПользователей: {users}\nЧатов: {chats}\nРозыгрышей: {raffles}\nАктивных: {active}")
    elif action == "adm_ton_wallet" and is_main(uid):
        await q.message.answer("Изменить TON-кошелёк:\n/setton WALLET_ADDRESS")
    elif action == "adm_ton_test":
        ok, detail = await ton_healthcheck()
        await q.message.answer(("✅ " if ok else "❌ ") + "Проверка TON-монитора\n" + detail)
    await q.answer()

@dp.message(Command("addadmin"))
async def add_admin(m: Message):
    if not is_main(m.from_user.id): return
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        await m.answer("Использование: /addadmin USER_ID"); return
    target = int(p[1])
    if target == MAIN_ADMIN_ID:
        await m.answer("Этот ID уже является главным администратором."); return
    with connect_db() as c:
        c.execute("INSERT INTO admins(user_id,role,added_at) VALUES(?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET role='admin', added_at=excluded.added_at",
                  (target, "admin", now_iso()))
    await m.answer(f"Младший администратор {target} добавлен.")

@dp.message(Command("deladmin"))
async def del_admin(m: Message):
    if not is_main(m.from_user.id): return
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        await m.answer("Использование: /deladmin USER_ID"); return
    with connect_db() as c:
        c.execute("DELETE FROM admins WHERE user_id=? AND role='admin'", (int(p[1]),))
    await m.answer("Обычный администратор удалён, если он существовал.")

@dp.message(Command("setton"))
async def set_ton(m: Message):
    if not is_main(m.from_user.id): return
    wallet = (m.text or "").partition(" ")[2].strip()
    if len(wallet) < 20:
        await m.answer("Укажите корректный адрес: /setton WALLET_ADDRESS"); return
    set_setting("ton_wallet", wallet)
    await m.answer("TON-адрес сохранён. Монитор будет отслеживать входящие транзакции на него.")

@dp.message(Command("cardnote"))
async def set_card_note(m: Message):
    if not is_main(m.from_user.id): return
    note = (m.text or "").partition(" ")[2].strip()
    if not note:
        await m.answer("Использование: /cardnote текст инструкции"); return
    set_setting("card_note", note)
    await m.answer("Инструкция для карточных платежей сохранена.")

@dp.message(Command("blacklist"))
async def blacklist_cmd(m: Message):
    if not is_admin(m.from_user.id): return
    p = (m.text or "").split(maxsplit=2)
    if len(p) < 2 or not p[1].isdigit():
        await m.answer("Использование: /blacklist USER_ID [причина]"); return
    with connect_db() as c:
        c.execute("INSERT OR REPLACE INTO blacklist(user_id,reason,added_by,added_at) VALUES(?,?,?,?)",
                  (int(p[1]), p[2] if len(p)>2 else "", m.from_user.id, now_iso()))
    await m.answer("Пользователь добавлен в чёрный список.")

@dp.message(Command("unblacklist"))
async def unblacklist_cmd(m: Message):
    if not is_admin(m.from_user.id):
        return
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        await m.answer("Использование: /unblacklist USER_ID")
        return
    with connect_db() as c:
        cur = c.execute("DELETE FROM blacklist WHERE user_id=?", (int(p[1]),))
    await m.answer("Пользователь удалён из чёрного списка." if cur.rowcount else "Пользователя не было в чёрном списке.")

# ---------------- TON WATCHER (MONITOR ONLY) ----------------
async def ton_request(path, params=None):
    headers = {}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{TON_API_BASE}/{path}", params=params or {}, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {data}")
            return data

async def ton_healthcheck():
    wallet = get_setting("ton_wallet", TON_WALLET)
    if not wallet:
        return False, "TON-кошелёк не задан. Главный админ: /setton WALLET_ADDRESS"
    try:
        data = await ton_request("getAddressInformation", {"address": wallet})
        if data.get("ok") is False:
            return False, str(data)
        return True, f"API отвечает. Кошелёк: {wallet[:8]}…{wallet[-6:]}"
    except Exception as e:
        return False, f"Ошибка API: {e}"

async def ton_watcher():
    while True:
        wallet = get_setting("ton_wallet", TON_WALLET)
        if wallet:
            try:
                data = await ton_request("getTransactions", {"address": wallet, "limit": 20})
                txs = data.get("result", []) if isinstance(data, dict) else []
                baseline_key = "ton_baseline:" + wallet
                if get_setting(baseline_key, "0") != "1":
                    # First run for this wallet: mark existing transactions as seen,
                    # so old deposits are not reported as new.
                    with connect_db() as c:
                        for tx in txs:
                            tid = tx.get("transaction_id") or {}
                            tx_hash = tid.get("hash") or tx.get("hash")
                            lt = str(tid.get("lt") or "")
                            if tx_hash:
                                c.execute("INSERT OR IGNORE INTO ton_seen(tx_hash,amount_nano,from_addr,comment,seen_at) VALUES(?,?,?,?,?)",
                                          (f"{lt}:{tx_hash}", "0", "", "baseline", now_iso()))
                    set_setting(baseline_key, "1")
                    log.info("TON watcher baseline initialized for configured wallet.")
                else:
                    for tx in reversed(txs):
                        tid = tx.get("transaction_id") or {}
                        tx_hash = tid.get("hash") or tx.get("hash")
                        lt = str(tid.get("lt") or "")
                        if not tx_hash:
                            continue
                        key = f"{lt}:{tx_hash}"
                        with connect_db() as c:
                            exists = c.execute("SELECT 1 FROM ton_seen WHERE tx_hash=?", (key,)).fetchone()
                        if exists:
                            continue
                        in_msg = tx.get("in_msg") or {}
                        value = str(in_msg.get("value", "0") or "0")
                        src = str(in_msg.get("source", "") or "")
                        msg_data = in_msg.get("msg_data") or {}
                        comment = str(in_msg.get("message") or msg_data.get("text") or "")
                        try:
                            amount_nano = int(value)
                        except (TypeError, ValueError):
                            amount_nano = 0
                        with connect_db() as c:
                            c.execute("INSERT OR IGNORE INTO ton_seen(tx_hash,amount_nano,from_addr,comment,seen_at) VALUES(?,?,?,?,?)",
                                      (key, value, src, comment[:300], now_iso()))
                        # getTransactions may include outgoing transactions; notify only
                        # for a positive incoming message value.
                        if amount_nano <= 0:
                            continue
                        ton = amount_nano / 1_000_000_000
                        await notify_admins(
                            "💎 <b>Обнаружен входящий TON-перевод</b>\n"
                            f"Сумма: <code>{ton:.9f} TON</code>\n"
                            f"Отправитель: <code>{html.escape(src or 'не определён')}</code>\n"
                            f"Комментарий: <code>{html.escape(comment[:120] or 'нет')}</code>\n"
                            f"Хэш: <code>{html.escape(str(tx_hash))}</code>\n\n"
                            "Это уведомление о транзакции, не подтверждение оплаты подписки."
                        )
            except Exception as e:
                log.warning("TON watcher error: %s", e)
        await asyncio.sleep(max(10, TON_POLL_SECONDS))

# ---------------- MINI APP ----------------
HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Raffle Control</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:dark;--bg:#101218;--card:#1b202b;--line:#343c4b;--muted:#aab2c0;--accent:#657cf5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f5f6fa;font:16px system-ui;padding:18px}
h2{margin:8px 0 4px}.muted{color:var(--muted);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin:12px 0}
.stat{display:flex;gap:10px}.stat>div{flex:1}.num{font-size:25px;font-weight:800}
button{border:0;border-radius:12px;background:var(--accent);color:white;padding:12px 15px;font-weight:700;width:100%;margin-top:8px}
input,select{width:100%;padding:12px;border:1px solid var(--line);background:#111722;color:white;border-radius:10px;margin:6px 0}
</style></head><body>
<h2>🎁 Raffle Control</h2><div class="muted" id="hello">Панель управления</div>
<div class="card stat" id="adminStats"><div><div class="muted">Пользователи</div><div class="num" id="users">—</div></div><div><div class="muted">Розыгрыши</div><div class="num" id="raffles">—</div></div></div>
<div class="card"><b>Мои розыгрыши</b><div id="list" class="muted">Загрузка…</div></div>
<div class="card" id="createCard"><b>Создать розыгрыш</b>
<input id="chat" placeholder="ID группы, например -100...">
<input id="title" placeholder="Название">
<select id="kind"><option value="random">Случайный победитель</option><option value="777">Первый, кто напишет 777</option><option value="guess">Угадай число</option><option value="phrase">Ключевая фраза</option></select>
<input id="param" placeholder="Число или фраза (для соответствующего режима)">
<input id="desc" placeholder="Описание / условия">
<input id="duration" type="number" min="1" max="43200" value="60" placeholder="Длительность в минутах">
<button onclick="create()">Создать</button><div id="result" class="muted"></div></div>
<div class="card"><b>Подписка</b><div id="subscription" class="muted">Загрузка…</div></div>
<script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const initData=tg?.initData||"";
async function api(path,body){let r=await fetch(path,{method:body?"POST":"GET",headers:{"Content-Type":"application/json","X-Telegram-Init-Data":initData},body:body?JSON.stringify(body):undefined});return r.json()}
async function load(){try{let d=await api("/api/dashboard");document.getElementById("users").textContent=d.users??"—";document.getElementById("raffles").textContent=d.raffles??"—";document.getElementById("hello").textContent=d.name||"Панель управления";document.getElementById("adminStats").style.display=d.is_admin?"flex":"none";document.getElementById("createCard").style.display=d.is_admin?"block":"none";document.getElementById("subscription").textContent=d.subscription||"Нет активного доступа";document.getElementById("list").innerHTML=(d.items||[]).map(x=>`<p>#${x.id} — ${esc(x.title)} <span class="muted">(${esc(x.status)})</span></p>`).join("")||"Пока пусто"}catch(e){document.getElementById("list").textContent="Не удалось загрузить данные"}}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function create(){let d={chat_id:document.getElementById("chat").value,title:document.getElementById("title").value,kind:document.getElementById("kind").value,param:document.getElementById("param").value,description:document.getElementById("desc").value,duration_minutes:document.getElementById("duration").value};let r=await api("/api/raffles",d);document.getElementById("result").textContent=r.message||r.error||"Готово";load()}
load();
</script></body></html>"""

def validate_init_data(init_data):
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", None)
        if not received:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 86400:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception:
        return None

async def api_dashboard(request):
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        return web.json_response({"error": "Откройте приложение из Telegram"}, status=401)
    uid = int(user["id"])
    with connect_db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        total = c.execute("SELECT COUNT(*) n FROM raffles").fetchone()["n"]
        items = c.execute("SELECT id,title,status FROM raffles WHERE creator_id=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()
        sub = c.execute("SELECT subscription_until FROM users WHERE user_id=?", (uid,)).fetchone()
    until = sub["subscription_until"] if sub else None
    return web.json_response({
        "name": user.get("first_name", "Пользователь"),
        "is_admin": is_admin(uid),
        "users": users if is_admin(uid) else "—",
        "raffles": total if is_admin(uid) else "—",
        "items": [dict(x) for x in items] if is_admin(uid) else [],
        "subscription": ("Активна до " + until) if active_access(uid) else "Не активна"
    })

async def api_create_raffle(request):
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        return web.json_response({"error": "Нет авторизации"}, status=401)
    uid = int(user["id"])
    if not is_admin(uid):
        return web.json_response({"error": "Недостаточно прав"}, status=403)
    try:
        data = await request.json()
        chat_id = int(data["chat_id"])
        title = str(data["title"]).strip()[:100]
        kind = str(data["kind"])
        param = str(data.get("param", "")).strip()
        if not title or kind not in {"random","777","guess","phrase"}:
            raise ValueError("Проверьте название и тип розыгрыша")
        payload = {"description": str(data.get("description",""))[:500]}
        if kind == "guess":
            if not param.isdigit():
                raise ValueError("Для режима guess укажите целое число.")
            payload["number"] = param
        if kind == "phrase":
            if not param:
                raise ValueError("Для режима phrase укажите ключевую фразу.")
            payload["phrase"] = param[:200]
        duration = int(data.get("duration_minutes") or 60)
        if not 1 <= duration <= 43200:
            raise ValueError("Длительность должна быть от 1 до 43200 минут.")
        payload["duration_minutes"] = duration
        rid = await create_raffle(chat_id, uid, kind, title, payload)
        await post_raffle(chat_id, rid)
        return web.json_response({"message": f"Розыгрыш #{rid} опубликован"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=HTML, content_type="text/html"))
    app.router.add_get("/api/dashboard", api_dashboard)
    app.router.add_post("/api/raffles", api_create_raffle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    log.info("Mini App web server listening on %s:%s", WEB_HOST, WEB_PORT)
    return runner

async def main():
    init_db()
    runner = await web_server()
    tasks = [asyncio.create_task(dp.start_polling(bot)),
             asyncio.create_task(ton_watcher()),
             asyncio.create_task(raffle_timer())]
    log.info("Bot started. SQLite: %s", DB_PATH)
    try:
        await asyncio.gather(*tasks)
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
