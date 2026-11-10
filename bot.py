# -*- coding: utf-8 -*-
"""
Telegram-бот для управления SA:MP сервером DolgRussia (#358642).
Библиотека бота: python-telegram-bot (v20+).
Что умеет: статус/игроки сервера, баны/разбаны, кик по ID (RCON), выдача денег и
доната, логи файлом .txt, чтение и правка БД, конфиг и файлы по FTP,
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
import os
import re
import socket
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone

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

# --- RCON (нужен для кика). Пусто = бот сам прочитает rcon_password из server.cfg по FTP
RCON_PASS = ""
SERVER_CFG_PATH = "server.cfg"

# --- Логи ---
SERVER_LOG_PATH = "server_log.txt"   # серверный лог SA:MP на FTP
TZ_OFFSET_HOURS = 3                  # часовой пояс для времени в логах (Москва)

# --- Как бот подписывается в банах ---
ADMIN_NAME = "TG-Admin"

# --- Таблица аккаунтов мода (взято из исходников мода) ---
ACC_TABLE = "accounts"
ACC_NAME = "name"
MONEY_COL = "money"
DONATE_COL = "rub"
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("samp-bot")

SECRET_COL = re.compile(r"pass|hash|salt|token|secret", re.I)
READ_ONLY_SQL = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.I)
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
                InlineKeyboardButton("🧾 Логи", callback_data="m:logs"),
            ],
            [InlineKeyboardButton("❓ Команды", callback_data="m:help")],
        ]
    )


def ask_confirm(user_id, kind, payload, text, alt=None):
    """Кнопки подтверждения. alt=(подпись, kind, payload) — вторая кнопка с другим действием."""
    group = uuid.uuid4().hex[:8]
    cid = uuid.uuid4().hex[:8]
    PENDING[cid] = (user_id, kind, payload, group)
    rows = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"ok:{cid}"),
            InlineKeyboardButton("✖️ Отмена", callback_data=f"no:{cid}"),
        ]
    ]
    if alt:
        label, akind, apayload = alt
        aid = uuid.uuid4().hex[:8]
        PENDING[aid] = (user_id, akind, apayload, group)
        rows.insert(0, [InlineKeyboardButton(label, callback_data=f"ok:{aid}")])
    return text, InlineKeyboardMarkup(rows)


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
#                 RCON (кик) и разрешение цели
# ------------------------------------------------------------
_rcon_cache = {}


def get_rcon_password():
    if RCON_PASS:
        return RCON_PASS
    if "pw" in _rcon_cache:
        return _rcon_cache["pw"]
    data = ftp_download(SERVER_CFG_PATH)
    m = re.search(rb"(?mi)^\s*rcon_password\s+(\S+)", data)
    if not m:
        raise RuntimeError("В server.cfg нет rcon_password (впиши RCON_PASS в bot.py)")
    pw = m.group(1).decode("utf-8", "replace")
    if pw.lower() == "changeme":
        raise RuntimeError("В server.cfg стоит rcon_password changeme — SA:MP с ним RCON не принимает. Смени пароль в конфиге")
    _rcon_cache["pw"] = pw
    return pw


def rcon(cmd: str) -> str:
    pw = get_rcon_password().encode("utf-8")
    ip = socket.gethostbyname(SAMP_HOST)
    body = cmd.encode("cp1251", errors="replace")
    packet = (
        b"SAMP"
        + bytes(int(x) for x in ip.split("."))
        + struct.pack("<H", SAMP_PORT)
        + b"x"
        + struct.pack("<H", len(pw)) + pw
        + struct.pack("<H", len(body)) + body
    )
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    out = []
    try:
        s.sendto(packet, (ip, SAMP_PORT))
        while True:
            try:
                data, _ = s.recvfrom(8192)
            except socket.timeout:
                break
            d = data[11:]
            n = int.from_bytes(d[:2], "little")
            out.append(d[2 : 2 + n].decode("cp1251", errors="replace"))
            s.settimeout(0.6)
    finally:
        s.close()
    text = "\n".join(out)
    if "invalid rcon password" in text.lower():
        _rcon_cache.pop("pw", None)
        raise RuntimeError("Сервер отклонил RCON-пароль")
    return text


def resolve_online(arg: str):
    """arg — игровой ID (число до 3 цифр) или ник. Возвращает (ник, id|None)."""
    if arg.isdigit() and len(arg) <= 3:
        pid = int(arg)
        for p in samp_players():
            if p[0] == pid:
                return p[1], pid
        raise ValueError(f"Игрока с ID {pid} нет на сервере")
    try:
        for p in samp_players():
            if p[1].lower() == arg.lower():
                return p[1], p[0]
    except Exception:  # noqa
        pass
    return arg, None


def kick_player(pid: int, expected_name: str):
    """Кик по ID с проверкой, что в этом слоте всё ещё тот же игрок."""
    cur = {p[0]: p[1] for p in samp_players()}
    if pid not in cur:
        return "gone"
    if cur[pid].lower() != expected_name.lower():
        raise ValueError(f"В слоте {pid} теперь другой игрок ({cur[pid]}) — кик отменён")
    rcon(f"kick {pid}")
    time.sleep(1.5)
    after = {p[0]: p[1] for p in samp_players()}
    return "kicked" if after.get(pid, "").lower() != expected_name.lower() else "still"


def get_account(name: str):
    rows, _ = db_run(
        f"SELECT id, `{ACC_NAME}` AS name, admin, `{MONEY_COL}` AS money, "
        f"`{DONATE_COL}` AS donate, last_ip FROM `{ACC_TABLE}` WHERE `{ACC_NAME}`=%s LIMIT 1",
        (name,),
    )
    return rows[0] if rows else None


# ------------------------------------------------------------
#                      БАНЫ / ДЕНЬГИ (как в моде)
# ------------------------------------------------------------
def do_ban(acc_id, name, ip, days, reason):
    """Пишем в обе таблицы, как делает мод: listban (по ней проверяется вход) и ban_list."""
    now = int(time.time())
    end = 0 if days == 0 else now + days * 86400  # 0 = навсегда (так мод это читает)
    conn = db_connect()
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT COUNT(*) FROM listban WHERE acc_id=%s AND (unban_time=0 OR unban_time>%s))"
                " + (SELECT COUNT(*) FROM ban_list WHERE user_id=%s AND (ban_time=0 OR ban_time>%s)) AS n",
                (acc_id, now, acc_id, now),
            )
            if cur.fetchone()["n"]:
                conn.rollback()
                return "already"
            cur.execute("DELETE FROM ban_list WHERE user_id=%s", (acc_id,))
            cur.execute("DELETE FROM listban WHERE acc_id=%s", (acc_id,))
            cur.execute(
                "INSERT INTO ban_list (user_id,time,ban_time,ip,description,admin) VALUES (%s,%s,%s,%s,%s,%s)",
                (acc_id, now, end, ip or "", reason[:32], ADMIN_NAME),
            )
            cur.execute(
                "INSERT INTO listban (acc_id,player_name,admin_name,reason,ban_time,unban_time) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (acc_id, name, ADMIN_NAME, reason, now, end),
            )
        conn.commit()
        return "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def do_unban(acc_id):
    conn = db_connect()
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            a = cur.execute("DELETE FROM listban WHERE acc_id=%s", (acc_id,))
            b = cur.execute("DELETE FROM ban_list WHERE user_id=%s", (acc_id,))
        conn.commit()
        return a + b
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def do_give(acc_id, col, delta):
    """Относительное изменение (money = money + X) в пределах 0..2147483647."""
    _, n = db_run(
        f"UPDATE `{ACC_TABLE}` SET `{col}` = `{col}` + %s "
        f"WHERE id=%s AND `{col}` + %s BETWEEN 0 AND 2147483647 LIMIT 1",
        (delta, acc_id, delta),
    )
    return n


AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_actions.log")


def fmt_ts(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return str(ts)
    if ts <= 0:
        return "—"
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M:%S")


def audit(user, text):
    line = f"{fmt_ts(time.time())} | tg:{user.id} {user.first_name} | {text}\n"
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        log.warning("не смог записать аудит-лог")


# ------------------------------------------------------------
#                        ЛОГИ (файлом .txt)
# ------------------------------------------------------------
def ftp_tail(path, max_bytes=2_000_000):
    ftp = ftp_connect()
    try:
        ftp.voidcmd("TYPE I")
        try:
            size = ftp.size(path) or 0
        except Exception:  # noqa
            size = 0
        rest = max(0, size - max_bytes)
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write, rest=rest or None)
        data = buf.getvalue()
        if rest and b"\n" in data:
            data = data[data.index(b"\n") + 1 :]  # выкидываем обрезанную первую строку
        return data
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa
            pass


def rows_to_text(rows, fmt):
    return "\n".join(fmt(r) for r in rows) if rows else "(пусто)"


def build_log(kind, who, limit):
    """Возвращает (текст, заголовок) для /log."""
    like = f"%{who}%" if who else None
    if kind == "server":
        data = ftp_tail(SERVER_LOG_PATH)
        lines = decode_text(data).splitlines()[-limit:]
        return "\n".join(lines) or "(пусто)", f"server_log — последние {len(lines)} строк"

    if kind == "action":
        sql = (
            "SELECT l.time AS t, l.type AS ty, l.uip AS ip, l.description AS d, a.`name` AS n "
            "FROM action_log l LEFT JOIN `accounts` a ON a.id=l.acc_id "
        )
        args = []
        if like:
            sql += "WHERE a.`name` LIKE %s OR l.description LIKE %s "
            args = [like, like]
        sql += "ORDER BY l.time DESC LIMIT %s"
        rows, _ = db_run(sql, args + [limit])
        return rows_to_text(
            rows, lambda r: f"[{fmt_ts(r['t'])}] {r['n'] or '?'} ({r['ip']}) type={r['ty']}: {r['d']}"
        ), "Действия админов (action_log)"

    if kind == "ac":
        sql = "SELECT `id`, `type` AS ty, `date` AS dt, `name` AS n, `reason` AS r, `data` AS d FROM `aclogs` "
        args = []
        if like:
            sql += "WHERE `name` LIKE %s "
            args = [like]
        sql += "ORDER BY `id` DESC LIMIT %s"
        rows, _ = db_run(sql, args + [limit])
        return rows_to_text(
            rows, lambda r: f"[{r['dt']} {str(r['d']).split('|')[0].strip()}] {r['n']} | {r['ty']} | {r['r']} | {r['d']}"
        ), "Античит (aclogs)"

    if kind == "bans":
        sql = "SELECT player_name AS n, admin_name AS a, reason AS r, ban_time AS b, unban_time AS u FROM listban "
        args = []
        if like:
            sql += "WHERE player_name LIKE %s "
            args = [like]
        sql += "ORDER BY ban_time DESC LIMIT %s"
        rows, _ = db_run(sql, args + [limit])
        return rows_to_text(
            rows,
            lambda r: f"{r['n']} | забанил {r['a']} | {fmt_ts(r['b'])} → "
            f"{'навсегда' if int(r['u'] or 0) <= 0 else fmt_ts(r['u'])} | {r['r']}",
        ), "Баны (listban)"

    if kind == "market":
        sql = "SELECT created_at AS t, seller_name AS s, buyer_name AS b, item_name AS i, item_count AS c, price AS p, status AS st FROM marketplace_history "
        args = []
        if like:
            sql += "WHERE seller_name LIKE %s OR buyer_name LIKE %s "
            args = [like, like]
        sql += "ORDER BY created_at DESC LIMIT %s"
        rows, _ = db_run(sql, args + [limit])
        return rows_to_text(
            rows,
            lambda r: f"[{fmt_ts(r['t'])}] {r['i']} x{r['c']} | продавец {r['s']} → покупатель {r['b']} | цена {r['p']} | status {r['st']}",
        ), "Маркетплейс (marketplace_history)"

    if kind == "bot":
        try:
            with open(AUDIT_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()[-limit:]
        except FileNotFoundError:
            lines = []
        if who:
            lines = [l for l in lines if who.lower() in l.lower()]
        return "\n".join(lines) or "(пусто)", "Действия через бота"

    raise ValueError("Неизвестный тип лога")


LOG_HELP = (
    "<b>Логи</b> — приходят файлом .txt\n"
    "/log server [N] — server_log.txt (последние N строк, по умолчанию 500)\n"
    "/log action [ник] [N] — действия админов в игре\n"
    "/log ac [ник] [N] — античит\n"
    "/log bans [ник] [N] — баны\n"
    "/log market [ник] [N] — маркетплейс\n"
    "/log bot [текст] [N] — что делали через этого бота"
)


def log_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📜 Сервер", callback_data="lg:server"),
                InlineKeyboardButton("🛡 Админы", callback_data="lg:action"),
            ],
            [
                InlineKeyboardButton("🚨 Античит", callback_data="lg:ac"),
                InlineKeyboardButton("⛔ Баны", callback_data="lg:bans"),
            ],
            [
                InlineKeyboardButton("🛒 Маркет", callback_data="lg:market"),
                InlineKeyboardButton("🤖 Бот", callback_data="lg:bot"),
            ],
        ]
    )


async def send_log(update, kind, who="", limit=500):
    text, title = await asyncio.to_thread(build_log, kind, who, limit)
    stamp = datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS))).strftime("%Y%m%d_%H%M")
    head = f"{title}\nСформировано: {stamp} (UTC+{TZ_OFFSET_HOURS})\n{'=' * 40}\n"
    await target_message(update).reply_document(
        document=io.BytesIO((head + text).encode("utf-8")),
        filename=f"log_{kind}_{stamp}.txt",
        caption=title,
    )


# ------------------------------------------------------------
#                         КОМАНДЫ
# ------------------------------------------------------------
HELP = (
    "<b>Сервер</b>\n"
    "/status — онлайн и инфо\n"
    "/players — кто играет (ID | ник)\n"
    "/kick &lt;ID&gt; [причина] — кикнуть по игровому ID (RCON)\n\n"
    "<b>Игроки</b>\n"
    "/player &lt;ник&gt; — данные аккаунта\n"
    "/ban &lt;ник|ID&gt; &lt;дни, 0=навсегда&gt; &lt;причина&gt;\n"
    "/unban &lt;ник&gt;\n"
    "/givemoney &lt;ник|ID&gt; &lt;сумма&gt; — деньги (минус = забрать)\n"
    "/givedonate &lt;ник|ID&gt; &lt;сумма&gt; — донат (минус = забрать)\n"
    "/set &lt;ник&gt; &lt;колонка&gt; &lt;значение&gt; — любое поле аккаунта\n\n"
    "<b>Логи (.txt)</b> — /log\n\n"
    "<b>База</b>\n"
    "/tables, /table &lt;имя&gt;, /sql &lt;SELECT&gt;, /sqlw &lt;UPDATE/INSERT/DELETE&gt;\n\n"
    "<b>Файлы</b>\n"
    "/cfg, /cfgset &lt;ключ&gt; &lt;значение&gt;, /ls [папка], /get &lt;путь&gt;\n\n"
    "<i>Если игрок онлайн, мод хранит его данные в памяти и при выходе может "
    "затереть правки в БД. Поэтому деньги/донат онлайн-игроку бот выдаёт после кика.</i>"
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
async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await send(update, "Использование: /kick &lt;ID&gt; [причина]")
        return
    pid = int(context.args[0])
    reason = " ".join(context.args[1:])
    pl = {p[0]: p[1] for p in await asyncio.to_thread(samp_players)}
    if pid not in pl:
        await send(update, f"Игрока с ID {pid} нет на сервере")
        return
    text, kb = ask_confirm(
        update.effective_user.id,
        "kick",
        (pid, pl[pid], reason),
        f"Кикнуть <b>{html.escape(pl[pid])}</b> (ID {pid})?"
        + (f"\nПричина: {html.escape(reason)}" if reason else ""),
    )
    await send(update, text, kb)


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3 or not context.args[1].isdigit():
        await send(update, "Использование: /ban &lt;ник|ID&gt; &lt;дни, 0=навсегда&gt; &lt;причина&gt;\nПример: /ban Ivan_Petrov 7 читы")
        return
    days = int(context.args[1])
    if days > 3650:
        await send(update, "Максимум 3650 дней, на больше — 0 (навсегда)")
        return
    reason = " ".join(context.args[2:])
    name, pid = await asyncio.to_thread(resolve_online, context.args[0])
    acc = await asyncio.to_thread(get_account, name)
    if not acc:
        await send(update, f"Аккаунт {html.escape(name)} не найден в БД")
        return
    warn = f"\n⚠️ У цели админ-уровень {acc['admin']}" if int(acc["admin"] or 0) > 0 else ""
    kick_note = f"\nИгрок онлайн (ID {pid}) — после бана будет кикнут." if pid is not None else ""
    text, kb = ask_confirm(
        update.effective_user.id,
        "ban",
        (acc["id"], acc["name"], acc["last_ip"], days, reason, pid),
        f"Забанить <b>{html.escape(acc['name'])}</b> на "
        f"<b>{'навсегда' if days == 0 else f'{days} дн.'}</b>\n"
        f"Причина: {html.escape(reason)}{warn}{kick_note}",
    )
    await send(update, text, kb)


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send(update, "Использование: /unban &lt;ник&gt;")
        return
    acc = await asyncio.to_thread(get_account, context.args[0])
    if not acc:
        await send(update, "Аккаунт не найден в БД")
        return
    text, kb = ask_confirm(
        update.effective_user.id,
        "unban",
        (acc["id"], acc["name"]),
        f"Разбанить <b>{html.escape(acc['name'])}</b>?",
    )
    await send(update, text, kb)


async def _give(update, context, col, label, cmdname):
    if len(context.args) < 2 or not re.fullmatch(r"[+-]?\d+", context.args[1]):
        await send(update, f"Использование: /{cmdname} &lt;ник|ID&gt; &lt;сумма&gt; (минус — забрать)")
        return
    delta = int(context.args[1])
    if delta == 0:
        await send(update, "Сумма 0 — нечего менять")
        return
    name, pid = await asyncio.to_thread(resolve_online, context.args[0])
    acc = await asyncio.to_thread(get_account, name)
    if not acc:
        await send(update, f"Аккаунт {html.escape(name)} не найден в БД")
        return
    cur = int(acc["money"] if col == MONEY_COL else acc["donate"])
    new = cur + delta
    if not 0 <= new <= 2147483647:
        await send(update, f"Результат {new} выходит за пределы 0…2147483647 (сейчас {cur})")
        return
    base = (
        f"<b>{html.escape(acc['name'])}</b>: {label}\n"
        f"{cur:,} → <b>{new:,}</b> ({'+' if delta > 0 else ''}{delta:,})".replace(",", " ")
    )
    uid = update.effective_user.id
    if pid is None:
        text, kb = ask_confirm(uid, "give", (acc["id"], acc["name"], col, delta, None), base)
    else:
        text, kb = ask_confirm(
            uid,
            "give",
            (acc["id"], acc["name"], col, delta, pid),
            base + f"\n\n🟡 Игрок онлайн (ID {pid}). Чтобы мод не затёр выдачу, сначала кикну его, потом начислю.",
            alt=("⚠️ Без кика (может затереться)", "give", (acc["id"], acc["name"], col, delta, None)),
        )
    await send(update, text, kb)


@admin_only
async def cmd_givemoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _give(update, context, MONEY_COL, "деньги", "givemoney")


@admin_only
async def cmd_givedonate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _give(update, context, DONATE_COL, "донат", "givedonate")


@admin_only
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args)
    if not args:
        await send(update, LOG_HELP, log_menu())
        return
    kind = args.pop(0).lower()
    limit = 500
    if args and args[-1].isdigit():
        limit = max(1, min(int(args.pop()), 5000))
    who = " ".join(args)
    if kind not in ("server", "action", "ac", "bans", "market", "bot"):
        await send(update, LOG_HELP, log_menu())
        return
    await target_message(update).reply_text("⏳ Собираю лог…")
    await send_log(update, kind, who, limit)


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
    text = f"Таблиц: {len(rows)} (кол-во строк примерное)\n" + "\n".join(
        f"{r['t']}  (~{r['r']})" for r in rows
    )
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
async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send(update, "Использование: /player &lt;ник&gt;")
        return
    q = " ".join(context.args)
    rows, _ = await asyncio.to_thread(
        db_run, f"SELECT * FROM `{ACC_TABLE}` WHERE `{ACC_NAME}` LIKE %s LIMIT 5", (f"%{q}%",)
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
    cols = await asyncio.to_thread(get_columns, ACC_TABLE)
    if col not in cols:
        await send(update, "Нет такой колонки. Доступные:\n" + ", ".join(cols), pre=True)
        return
    rows, _ = await asyncio.to_thread(
        db_run, f"SELECT `{col}` AS v FROM `{ACC_TABLE}` WHERE `{ACC_NAME}`=%s", (nick,)
    )
    if len(rows) != 1:
        await send(update, f"Нужен ровно один игрок с ником {nick}, найдено: {len(rows)}")
        return
    old = "***" if SECRET_COL.search(col) else rows[0]["v"]
    text, kb = ask_confirm(
        update.effective_user.id,
        "set",
        (ACC_TABLE, ACC_NAME, col, nick, value),
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
async def run_confirmed(q, user, kind, payload):
    if kind == "set":
        t, n, col, nick, value = payload
        _, affected = await asyncio.to_thread(
            db_run, f"UPDATE `{t}` SET `{col}`=%s WHERE `{n}`=%s LIMIT 1", (value, nick)
        )
        audit(user, f"SET {nick}.{col}={'***' if SECRET_COL.search(col) else value}")
        await q.edit_message_text(f"✅ Готово, изменено строк: {affected}")

    elif kind == "sqlw":
        _, affected = await asyncio.to_thread(db_run, payload)
        audit(user, f"SQLW {payload}")
        await q.edit_message_text(f"✅ Выполнено, затронуто строк: {affected}")

    elif kind == "cfgset":
        key, value = payload
        old = await asyncio.to_thread(ftp_download, SETTINGS_PATH)
        pattern = re.compile(rb"(?m)^(\s*" + re.escape(key.encode()) + rb"\s*=\s*)(.*?)(\r?)$")
        new = pattern.sub(lambda m: m.group(1) + value.encode("utf-8") + m.group(3), old, count=1)
        await asyncio.to_thread(ftp_upload, SETTINGS_PATH + ".bak", old)
        await asyncio.to_thread(ftp_upload, SETTINGS_PATH, new)
        audit(user, f"CFGSET {key}={value}")
        await q.edit_message_text(f"✅ {key} = {value}\nБэкап: {SETTINGS_PATH}.bak")

    elif kind == "kick":
        pid, name, reason = payload
        res = await asyncio.to_thread(kick_player, pid, name)
        audit(user, f"KICK id={pid} {name} reason={reason}")
        msg = {
            "kicked": f"✅ {name} (ID {pid}) кикнут",
            "gone": f"ℹ️ {name} уже вышел",
            "still": f"⚠️ Команда отправлена, но {name} всё ещё на сервере. Проверь RCON-пароль и /players",
        }[res]
        await q.edit_message_text(msg)

    elif kind == "ban":
        acc_id, name, ip, days, reason, pid = payload
        res = await asyncio.to_thread(do_ban, acc_id, name, ip, days, reason)
        if res == "already":
            await q.edit_message_text(f"ℹ️ У {name} уже есть активный бан. Сначала /unban")
            return
        audit(user, f"BAN {name} days={days} reason={reason}")
        text = f"✅ {name} забанен на {'навсегда' if days == 0 else f'{days} дн.'}"
        if pid is not None:
            try:
                k = await asyncio.to_thread(kick_player, pid, name)
                text += "\n👢 Кикнут с сервера" if k == "kicked" else f"\n⚠️ Кик: {k}"
            except Exception as e:  # noqa
                text += f"\n⚠️ Бан записан, но кикнуть не вышло: {e}"
        await q.edit_message_text(text)

    elif kind == "unban":
        acc_id, name = payload
        n = await asyncio.to_thread(do_unban, acc_id)
        audit(user, f"UNBAN {name}")
        await q.edit_message_text(f"✅ {name} разбанен" if n else f"ℹ️ У {name} не было банов")

    elif kind == "give":
        acc_id, name, col, delta, pid = payload
        label = "деньги" if col == MONEY_COL else "донат"
        note = ""
        if pid is not None:
            k = await asyncio.to_thread(kick_player, pid, name)
            if k == "still":
                await q.edit_message_text("⚠️ Не удалось кикнуть игрока (RCON?) — выдачу отменил, чтобы её не затёрло")
                return
            await asyncio.sleep(3)  # даём моду сохранить игрока при выходе
            note = "\n👢 Игрок был кикнут перед выдачей"
        n = await asyncio.to_thread(do_give, acc_id, col, delta)
        audit(user, f"GIVE {label} {name} {delta:+d}")
        if n == 1:
            await q.edit_message_text(f"✅ {name}: {label} {delta:+,}".replace(",", " ") + note)
        else:
            await q.edit_message_text("❌ Не вышло: результат вышел бы за пределы 0…2147483647 или аккаунт пропал")


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
        elif action == "logs":
            await send(update, LOG_HELP, log_menu())
        elif action == "help":
            await send(update, HELP, main_menu())
        return

    if data.startswith("lg:"):
        await target_message(update).reply_text("⏳ Собираю лог…")
        await send_log(update, data[3:])
        return

    if data.startswith(("ok:", "no:")):
        cid = data[3:]
        item = PENDING.pop(cid, None)
        if not item or item[0] != update.effective_user.id:
            await q.edit_message_text("Это подтверждение уже недействительно")
            return
        for k in [k for k, v in PENDING.items() if v[3] == item[3]]:
            PENDING.pop(k, None)  # гасим парные кнопки этого подтверждения
        if data.startswith("no:"):
            await q.edit_message_text("✖️ Отменено")
            return
        await run_confirmed(q, update.effective_user, item[1], item[2])


# ------------------------------------------------------------
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
        ("player", cmd_player),
        ("kick", cmd_kick),
        ("ban", cmd_ban),
        ("unban", cmd_unban),
        ("givemoney", cmd_givemoney),
        ("givedonate", cmd_givedonate),
        ("log", cmd_log),
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
