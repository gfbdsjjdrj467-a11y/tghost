# -*- coding: utf-8 -*-
"""
Telegram-бот для управления SA:MP сервером DolgRussia (#358642).
Библиотека бота: python-telegram-bot (v20+).
Что умеет: статус/игроки сервера, чтение и правка БД, конфиг и файлы по FTP,
уведомления когда сервер упал / поднялся.

Установка:
    pip install "python-telegram-bot[job-queue]" pymysql
Запуск:
    python bot.py
"""

import asyncio
import functools
import html
import io
import ftplib
import logging
import re
import socket
import struct
import uuid

import pymysql
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ============================================================
#                        НАСТРОЙКИ
# ============================================================
BOT_TOKEN = "9174918610:AAG5J3aX4ZniES-7cWeZFphU_giM0i5FnUg"

# Telegram ID админов (узнать можно у @userinfobot). Остальным бот не отвечает.
ADMIN_IDS = [8890502021]

# --- Игровой сервер (панель: "Адрес") ---
SAMP_HOST = "178.130.53.99"
SAMP_PORT = 1337

# --- MySQL (панель: "MySQL доступ"). Бот снаружи, поэтому УДАЛЁННЫЙ хост.
# 127.0.0.1 работает только если бот запущен на самом игровом сервере.
DB_HOST = "178.130.53.37"
DB_PORT = 3306
DB_USER = "gs358642"
DB_PASS = "oogLLKAhCQAB"
DB_NAME = "gs358642"

# --- FTP (панель: "FTP доступ") ---
FTP_HOST = "178.130.53.99"
FTP_PORT = 21
FTP_USER = "gs358642"
# Если не логинится — скопируй пароль кнопкой в панели: I (большая "и") и l (маленькая "эл") легко спутать
FTP_PASS = "d1xDIdsW4mS2"

# Путь к настройкам мода на FTP (относительно корня FTP)
SETTINGS_PATH = "scriptfiles/bykranin_server_settings.ini"

# Как часто проверять онлайн сервера (сек). 0 = выключить уведомления
MONITOR_INTERVAL = 60
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("samp-bot")

SECRET_COL = re.compile(r"pass|hash|salt|token|secret", re.I)
READ_ONLY_SQL = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.I)
NAME_COLS = ("name", "nick", "nickname", "username", "login", "player")
USER_TABLE_HINTS = ("accounts", "users", "players", "account", "user", "player")
PENDING = {}  # id -> (user_id, kind, payload)


# ------------------------------------------------------------
#                       ВСПОМОГАТЕЛЬНОЕ
# ------------------------------------------------------------
def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Нет доступа", show_alert=True)
            elif update.message:
                await update.message.reply_text(
                    f"⛔ Нет доступа. Твой ID: {user.id if user else '?'}"
                )
            return
        try:
            await func(update, context)
        except Exception as e:  # noqa
            log.exception("handler error")
            await send(update, f"❌ Ошибка: {e}")

    return wrapper


def target_message(update: Update):
    if update.callback_query:
        return update.callback_query.message
    return update.message


async def send(update: Update, text: str, markup=None, pre=False):
    """Отправка с разбивкой на куски (лимит Telegram 4096 символов)."""
    msg = target_message(update)
    lines, chunks, cur = text.split("\n"), [], ""
    for line in lines:
        while len(line) > 3500:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:3500])
            line = line[3500:]
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip() or not chunks:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        body = f"<pre>{html.escape(ch)}</pre>" if pre else ch
        await msg.reply_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=markup if i == len(chunks) - 1 else None,
            disable_web_page_preview=True,
        )


def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📡 Статус", callback_data="m:status"),
                InlineKeyboardButton("👥 Игроки", callback_data="m:players"),
            ],
            [
                InlineKeyboardButton("🗄 Таблицы БД", callback_data="m:tables"),
                InlineKeyboardButton("⚙️ Конфиг мода", callback_data="m:cfg"),
            ],
            [
                InlineKeyboardButton("📁 Файлы FTP", callback_data="m:ls"),
                InlineKeyboardButton("❓ Команды", callback_data="m:help"),
            ],
        ]
    )


def ask_confirm(user_id, kind, payload, text):
    cid = uuid.uuid4().hex[:8]
    PENDING[cid] = (user_id, kind, payload)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"ok:{cid}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=f"no:{cid}"),
            ]
        ]
    )
    return text, kb


# ------------------------------------------------------------
#                    SA:MP QUERY (UDP)
# ------------------------------------------------------------
def samp_query(opcode: bytes) -> bytes:
    ip = socket.gethostbyname(SAMP_HOST)
    packet = (
        b"SAMP"
        + bytes(int(x) for x in ip.split("."))
        + struct.pack("<H", SAMP_PORT)
        + opcode
    )
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(packet, (ip, SAMP_PORT))
        data, _ = s.recvfrom(8192)
    finally:
        s.close()
    return data[11:]


def _read_str(buf, pos, size_bytes):
    n = int.from_bytes(buf[pos : pos + size_bytes], "little")
    pos += size_bytes
    return buf[pos : pos + n].decode("cp1251", errors="replace"), pos + n


def samp_info():
    d = samp_query(b"i")
    passworded = d[0]
    players, maxp = struct.unpack("<HH", d[1:5])
    host, pos = _read_str(d, 5, 4)
    mode, pos = _read_str(d, pos, 4)
    lang, pos = _read_str(d, pos, 4)
    return {
        "password": bool(passworded),
        "players": players,
        "max": maxp,
        "hostname": host,
        "mode": mode,
        "lang": lang,
    }


def samp_players():
    d = samp_query(b"c")
    count = struct.unpack("<H", d[:2])[0]
    pos, out = 2, []
    for _ in range(count):
        pid = d[pos]
        pos += 1
        name, pos = _read_str(d, pos, 1)
        score, ping = struct.unpack("<iI", d[pos : pos + 8])
        pos += 8
        out.append((pid, name, score, ping))
    return out


# ------------------------------------------------------------
#                          MySQL
# ------------------------------------------------------------
def db_connect():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=20,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def db_run(sql, args=None):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute(sql, args)
            rows = cur.fetchall() if cur.description else []
            return rows, n
    finally:
        conn.close()


def get_columns(table):
    rows, _ = db_run(
        "SELECT column_name AS c FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (DB_NAME, table),
    )
    return [r["c"] for r in rows]


def detect_user_table():
    """Ищем таблицу аккаунтов сами: у неё есть колонка с ником + money/level/и т.п."""
    rows, _ = db_run(
        "SELECT table_name AS t, column_name AS c FROM information_schema.columns "
        "WHERE table_schema=%s",
        (DB_NAME,),
    )
    by = {}
    for r in rows:
        by.setdefault(r["t"], []).append(r["c"])
    best = None
    for t, cols in by.items():
        lc = {c.lower(): c for c in cols}
        name = next((lc[n] for n in NAME_COLS if n in lc), None)
        if not name:
            continue
        score = 10 if t.lower() in USER_TABLE_HINTS else 0
        score += sum(
            k in lc
            for k in ("money", "level", "lvl", "admin", "password", "pass", "donate", "exp", "bank")
        )
        if best is None or score > best[0]:
            best = (score, t, name)
    return (best[1], best[2]) if best else (None, None)


def user_table(context):
    cached = context.application.bot_data.get("user_table")
    if cached and cached[0]:
        return cached
    cached = detect_user_table()
    context.application.bot_data["user_table"] = cached
    return cached


def fmt_row(row):
    lines = []
    for k, v in row.items():
        if SECRET_COL.search(k):
            v = "***"
        elif isinstance(v, (bytes, bytearray)):
            v = f"<{len(v)} bytes>"
        else:
            v = str(v)
            if len(v) > 120:
                v = v[:120] + "…"
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


# ------------------------------------------------------------
#                            FTP
# ------------------------------------------------------------
def ftp_connect():
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp


def ftp_list(path):
    ftp = ftp_connect()
    try:
        lines = []
        ftp.retrlines(f"LIST {path}" if path else "LIST", lines.append)
        return lines
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa
            pass


def ftp_download(path, limit=45 * 1024 * 1024):
    ftp = ftp_connect()
    try:
        ftp.voidcmd("TYPE I")
        try:
            size = ftp.size(path)
        except Exception:  # noqa
            size = None
        if size and size > limit:
            raise ValueError(f"Файл {size/1024/1024:.1f} МБ — больше лимита Telegram (50 МБ)")
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        return buf.getvalue()
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa
            pass


def ftp_upload(path, data: bytes):
    ftp = ftp_connect()
    try:
        ftp.voidcmd("TYPE I")
        ftp.storbinary(f"STOR {path}", io.BytesIO(data))
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa
            pass


def decode_text(data: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# ------------------------------------------------------------
#                         КОМАНДЫ
# ------------------------------------------------------------
HELP = (
    "<b>Команды</b>\n"
    "/status — онлайн и инфо сервера\n"
    "/players — кто сейчас играет\n"
    "/tables — таблицы базы\n"
    "/table &lt;имя&gt; — колонки и кол-во строк\n"
    "/usertable &lt;таблица&gt; — вручную указать таблицу аккаунтов\n"
    "/player &lt;ник&gt; — найти игрока в БД\n"
    "/set &lt;ник&gt; &lt;колонка&gt; &lt;значение&gt; — изменить поле игрока (с подтверждением)\n"
    "/sql &lt;SELECT...&gt; — читающий запрос\n"
    "/sqlw &lt;UPDATE/INSERT/DELETE...&gt; — запись (с подтверждением)\n"
    "/cfg — показать настройки мода\n"
    "/cfgset &lt;ключ&gt; &lt;значение&gt; — изменить настройку (с бэкапом .bak)\n"
    "/ls [папка] — список файлов FTP\n"
    "/get &lt;путь&gt; — скачать файл с FTP\n\n"
    "<i>Если игрок онлайн, мод может перезаписать правки в БД при выходе — "
    "правь тех, кто оффлайн.</i>"
)


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update, "🎮 <b>DolgRussia — панель сервера</b>", main_menu())


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update, HELP, main_menu())


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = await asyncio.to_thread(samp_info)
    except Exception:  # noqa
        await send(update, f"🔴 Сервер <b>{SAMP_HOST}:{SAMP_PORT}</b> не отвечает", main_menu())
        return
    text = (
        f"🟢 <b>{html.escape(info['hostname'])}</b>\n"
        f"Адрес: <code>{SAMP_HOST}:{SAMP_PORT}</code>\n"
        f"Онлайн: <b>{info['players']}/{info['max']}</b>\n"
        f"Мод: {html.escape(info['mode'])}\n"
        f"Язык: {html.escape(info['lang'])}\n"
        f"Пароль: {'да' if info['password'] else 'нет'}"
    )
    await send(update, text, main_menu())


@admin_only
async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pl = await asyncio.to_thread(samp_players)
    except Exception:  # noqa
        await send(update, "🔴 Не удалось получить список игроков (сервер молчит)")
        return
    if not pl:
        await send(update, "Сейчас на сервере никого нет")
        return
    lines = [f"{pid:>3} | {name:<24} | score {score:<6} | ping {ping}" for pid, name, score, ping in pl]
    await send(update, f"Игроков: {len(pl)}\n" + "\n".join(lines), pre=True)


@admin_only
async def cmd_tables(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows, _ = await asyncio.to_thread(
        db_run,
        "SELECT table_name AS t, table_rows AS r FROM information_schema.tables "
        "WHERE table_schema=%s ORDER BY table_name",
        (DB_NAME,),
    )
    if not rows:
        await send(update, "В базе нет таблиц")
        return
    ut = await asyncio.to_thread(user_table, context)
    text = f"Таблиц: {len(rows)} (кол-во строк примерное)\n" + "\n".join(
        f"{r['t']}  (~{r['r']})" for r in rows
    )
    text += f"\n\nТаблица аккаунтов: {ut[0]} / колонка ника: {ut[1]}"
    await send(update, text, pre=True)


@admin_only
async def cmd_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send(update, "Использование: /table &lt;имя&gt;")
        return
    t = context.args[0]
    cols = await asyncio.to_thread(get_columns, t)
    if not cols:
        await send(update, "Такой таблицы нет")
        return
    rows, _ = await asyncio.to_thread(db_run, f"SELECT COUNT(*) AS n FROM `{t}`")
    await send(update, f"{t}: {rows[0]['n']} строк\nКолонки ({len(cols)}):\n" + ", ".join(cols), pre=True)


@admin_only
async def cmd_usertable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        t, n = await asyncio.to_thread(user_table, context)
        await send(update, f"Сейчас: таблица {t}, колонка ника {n}\nИзменить: /usertable &lt;таблица&gt; [колонка_ника]")
        return
    t = context.args[0]
    cols = await asyncio.to_thread(get_columns, t)
    if not cols:
        await send(update, "Такой таблицы нет")
        return
    lc = {c.lower(): c for c in cols}
    name = context.args[1] if len(context.args) > 1 else next((lc[n] for n in NAME_COLS if n in lc), None)
    if not name or name not in cols:
        await send(update, "Не нашёл колонку ника, укажи: /usertable &lt;таблица&gt; &lt;колонка&gt;")
        return
    context.application.bot_data["user_table"] = (t, name)
    await send(update, f"✅ Таблица аккаунтов: {t}, колонка ника: {name}")


@admin_only
async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send(update, "Использование: /player &lt;ник&gt;")
        return
    t, n = await asyncio.to_thread(user_table, context)
    if not t:
        await send(update, "Не нашёл таблицу аккаунтов. Укажи вручную: /usertable &lt;таблица&gt;")
        return
    q = " ".join(context.args)
    rows, _ = await asyncio.to_thread(
        db_run, f"SELECT * FROM `{t}` WHERE `{n}` LIKE %s LIMIT 5", (f"%{q}%",)
    )
    if not rows:
        await send(update, "Никого не найдено")
        return
    await send(update, "\n\n".join(fmt_row(r) for r in rows), pre=True)


@admin_only
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await send(update, "Использование: /set &lt;ник&gt; &lt;колонка&gt; &lt;значение&gt;")
        return
    nick, col, value = context.args[0], context.args[1], " ".join(context.args[2:])
    t, n = await asyncio.to_thread(user_table, context)
    if not t:
        await send(update, "Не нашёл таблицу аккаунтов. Укажи: /usertable &lt;таблица&gt;")
        return
    cols = await asyncio.to_thread(get_columns, t)
    if col not in cols:
        await send(update, "Нет такой колонки. Доступные:\n" + ", ".join(cols), pre=True)
        return
    rows, _ = await asyncio.to_thread(
        db_run, f"SELECT `{col}` AS v FROM `{t}` WHERE `{n}`=%s", (nick,)
    )
    if len(rows) != 1:
        await send(update, f"Нужен ровно один игрок с ником {nick}, найдено: {len(rows)}")
        return
    old = "***" if SECRET_COL.search(col) else rows[0]["v"]
    text, kb = ask_confirm(
        update.effective_user.id,
        "set",
        (t, n, col, nick, value),
        f"Изменить <b>{html.escape(nick)}</b>.<b>{html.escape(col)}</b>:\n"
        f"{html.escape(str(old))} → <b>{html.escape(value)}</b>",
    )
    await send(update, text, kb)


@admin_only
async def cmd_sql(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sql = update.message.text.partition(" ")[2].strip()
    if not sql:
        await send(update, "Использование: /sql SELECT * FROM таблица LIMIT 5")
        return
    sql = sql.rstrip(";")
    if ";" in sql or not READ_ONLY_SQL.match(sql):
        await send(update, "Тут только один читающий запрос (SELECT/SHOW/DESCRIBE). Для записи — /sqlw")
        return
    rows, _ = await asyncio.to_thread(db_run, sql)
    if not rows:
        await send(update, "Пусто")
        return
    out = "\n\n".join(fmt_row(r) for r in rows[:20])
    if len(rows) > 20:
        out += f"\n\n… показаны 20 из {len(rows)}"
    await send(update, out, pre=True)


@admin_only
async def cmd_sqlw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sql = update.message.text.partition(" ")[2].strip().rstrip(";")
    if not sql or ";" in sql:
        await send(update, "Использование: /sqlw UPDATE ... (один запрос)")
        return
    if re.match(r"^\s*(drop|truncate|alter|grant|revoke|create)\b", sql, re.I):
        await send(update, "DROP/TRUNCATE/ALTER/GRANT/CREATE через бота запрещены — делай это в phpMyAdmin")
        return
    text, kb = ask_confirm(
        update.effective_user.id,
        "sqlw",
        sql,
        f"Выполнить запрос?\n<code>{html.escape(sql)}</code>",
    )
    await send(update, text, kb)


@admin_only
async def cmd_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await asyncio.to_thread(ftp_download, SETTINGS_PATH)
    await send(update, decode_text(data), pre=True)


@admin_only
async def cmd_cfgset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await send(update, "Использование: /cfgset &lt;ключ&gt; &lt;значение&gt;")
        return
    key, value = context.args[0], " ".join(context.args[1:])
    data = await asyncio.to_thread(ftp_download, SETTINGS_PATH)
    pattern = re.compile(rb"(?m)^(\s*" + re.escape(key.encode()) + rb"\s*=\s*)(.*?)(\r?)$")
    m = pattern.search(data)
    if not m:
        await send(update, f"Ключа {html.escape(key)} нет в файле. Смотри /cfg")
        return
    text, kb = ask_confirm(
        update.effective_user.id,
        "cfgset",
        (key, value),
        f"<b>{html.escape(key)}</b>: {html.escape(decode_text(m.group(2)))} → <b>{html.escape(value)}</b>\n"
        "Перед записью сделаю бэкап .bak. Мод подхватит после рестарта сервера.",
    )
    await send(update, text, kb)


@admin_only
async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = " ".join(context.args) if context.args else ""
    lines = await asyncio.to_thread(ftp_list, path)
    await send(update, f"/{path}\n" + ("\n".join(lines) if lines else "пусто"), pre=True)


@admin_only
async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send(update, "Использование: /get &lt;путь/к/файлу&gt;")
        return
    path = " ".join(context.args)
    await target_message(update).reply_text("⏳ Качаю…")
    data = await asyncio.to_thread(ftp_download, path)
    await target_message(update).reply_document(
        document=io.BytesIO(data), filename=path.split("/")[-1] or "file"
    )


# ------------------------------------------------------------
#                       КНОПКИ / ПОДТВЕРЖДЕНИЯ
# ------------------------------------------------------------
@admin_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("m:"):
        action = data[2:]
        if action == "status":
            await cmd_status(update, context)
        elif action == "players":
            await cmd_players(update, context)
        elif action == "tables":
            await cmd_tables(update, context)
        elif action == "cfg":
            await cmd_cfg(update, context)
        elif action == "ls":
            await cmd_ls(update, context)
        elif action == "help":
            await send(update, HELP, main_menu())
        return

    if data.startswith(("ok:", "no:")):
        cid = data[3:]
        item = PENDING.pop(cid, None)
        if not item or item[0] != update.effective_user.id:
            await q.edit_message_text("Это подтверждение уже недействительно")
            return
        if data.startswith("no:"):
            await q.edit_message_text("✖️ Отменено")
            return
        _, kind, payload = item
        if kind == "set":
            t, n, col, nick, value = payload
            _, affected = await asyncio.to_thread(
                db_run, f"UPDATE `{t}` SET `{col}`=%s WHERE `{n}`=%s LIMIT 1", (value, nick)
            )
            await q.edit_message_text(f"✅ Готово, изменено строк: {affected}")
        elif kind == "sqlw":
            _, affected = await asyncio.to_thread(db_run, payload)
            await q.edit_message_text(f"✅ Выполнено, затронуто строк: {affected}")
        elif kind == "cfgset":
            key, value = payload
            old = await asyncio.to_thread(ftp_download, SETTINGS_PATH)
            pattern = re.compile(rb"(?m)^(\s*" + re.escape(key.encode()) + rb"\s*=\s*)(.*?)(\r?)$")
            new = pattern.sub(
                lambda m: m.group(1) + value.encode("utf-8") + m.group(3), old, count=1
            )
            await asyncio.to_thread(ftp_upload, SETTINGS_PATH + ".bak", old)
            await asyncio.to_thread(ftp_upload, SETTINGS_PATH, new)
            await q.edit_message_text(f"✅ {key} = {value}\nБэкап: {SETTINGS_PATH}.bak")


# ------------------------------------------------------------
#                   МОНИТОРИНГ (уведомления)
# ------------------------------------------------------------
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(samp_info)
        alive = True
    except Exception:  # noqa
        alive = False
    prev = context.application.bot_data.get("alive")
    context.application.bot_data["alive"] = alive
    if prev is None or prev == alive:
        return
    text = "🟢 Сервер снова онлайн" if alive else "🔴 Сервер перестал отвечать!"
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, text)
        except Exception:  # noqa
            log.warning("не смог написать админу %s", admin)


# ------------------------------------------------------------
#                            ЗАПУСК
# ------------------------------------------------------------
def main():
    if "ВСТАВЬ" in BOT_TOKEN:
        raise SystemExit("Вставь токен бота в BOT_TOKEN в начале bot.py")

    app = Application.builder().token(BOT_TOKEN).build()

    for name, fn in (
        ("start", cmd_start),
        ("help", cmd_help),
        ("status", cmd_status),
        ("players", cmd_players),
        ("tables", cmd_tables),
        ("table", cmd_table),
        ("usertable", cmd_usertable),
        ("player", cmd_player),
        ("set", cmd_set),
        ("sql", cmd_sql),
        ("sqlw", cmd_sqlw),
        ("cfg", cmd_cfg),
        ("cfgset", cmd_cfgset),
        ("ls", cmd_ls),
        ("get", cmd_get),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))

    if MONITOR_INTERVAL and app.job_queue:
        app.job_queue.run_repeating(monitor, interval=MONITOR_INTERVAL, first=5)
    elif MONITOR_INTERVAL:
        log.warning('JobQueue выключен: pip install "python-telegram-bot[job-queue]"')

    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
