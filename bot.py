import asyncio
import io
import os
import re
import sqlite3
import sys
import traceback
from datetime import datetime

import httpx
from PIL import Image, ImageDraw, ImageFont
from vkbottle.bot import Bot, Message

# --- убрать длинные служебные логи vkbottle: оставить только предупреждения и ошибки ---
try:
    from loguru import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="WARNING")
except ImportError:
    pass
import logging
logging.getLogger("vkbottle").setLevel(logging.WARNING)

# ================= НАСТРОЙКИ =================
VK_TOKEN = os.getenv("VK_TOKEN", "vk1.a.3FiWKbH1u6g0dtlpp0E9WXxkmb52DRBRTMFhQMu9pOpMa7fnwQur9K_e6UUN8rBfzitU5RPjMb3TFycsOSbiYnDz8z62RTVKIVxoma9xaahkPDncBOv5AUn-n88gCGiSuhHg8-J0_qjCBvw1ciGlXfAAFliMIaerTw8FRCUxGIgww6bGoF4Uvh69sTSmWcb-LaV6z866GQ_LM5xORSA53w")
SCRIPT_URL = os.getenv("SCRIPT_URL", "https://script.google.com/macros/s/AKfycbwyx1W_8PUE8QxpGRa1zZioZFH92_iTOtx09pIWIqhAIFHv6K50NwTNp9EAPVW8LG-H/exec")
SCRIPT_SECRET = os.getenv("SCRIPT_SECRET", "724422")
# VK ID администраторов (всегда имеют доступ, только они выдают /vd и /unvd)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "875762552").split(",") if x.strip()}
DB_PATH = "bot.db"
FONT_REG = ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"]
FONT_BOLD = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"]
# =============================================

bot = Bot(VK_TOKEN)

FACTION_LIST = [
    "Правительство", "Мэрия", "Верховный суд", "Полиция ЛС", "ФБР", "СВАТ", "Полицейская академия",
    "Больница ЛС", "Радиоцентр", "Русская мафия", "Итальянская мафия", "Японская мафия",
    "Похоронное Бюро", "Байкеры",
]
FACTION_HINT = "🔢 Номера фракций:\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(FACTION_LIST, 1))

HELP = (
    "📖 Команды бота:\n\n"
    "/sl @user <фракция 1-14> Nick_Name дд.мм — назначить лидера (можно ответом на сообщение)\n"
    "/rl @user — убрать лидера из таблицы (или ответом)\n"
    "/сменить @user Новый_Ник — сменить ник (или ответом)\n"
    "/список — список лидеров + фото таблицы\n"
    "/чат — добавить этот чат в базу бота\n"
    "/vd @user — выдать доступ к командам (только админ)\n"
    "/unvd @user — забрать доступ (только админ)\n"
    "/help — эта справка\n\n"
    "Пример: /sl @durov 3 Ivan_Petrov 01.10\n\n"
    + FACTION_HINT
)

# ---------- база данных ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS chats (peer_id INTEGER PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS notified (vk_id TEXT, end TEXT, PRIMARY KEY (vk_id, end))")
db.commit()


def has_access(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    return db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None


def chat_known(peer_id: int) -> bool:
    if peer_id < 2_000_000_000:  # личные сообщения
        return True
    return db.execute("SELECT 1 FROM chats WHERE peer_id=?", (peer_id,)).fetchone() is not None


# ---------- таблица (Apps Script) ----------
async def sheet(action: str, **data) -> dict:
    payload = {"secret": SCRIPT_SECRET, "action": action, **data}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=40) as c:
            r = await c.post(SCRIPT_URL, json=payload)
        return r.json()
    except Exception as e:  # noqa
        return {"ok": False, "error": f"Ошибка связи с таблицей: {e}"}


# ---------- вспомогательное ----------
MENTION = re.compile(r"^\[id(\d+)\|[^\]]*\]\s*")
NAME = re.compile(r"^(?:@|https?://(?:m\.)?vk\.com/)?([A-Za-z0-9_.]+)\s*")
NICK = re.compile(r"^[A-Za-z0-9]+_[A-Za-z0-9_]+$")


async def get_target(m: Message, rest: str):
    """Возвращает (vk_id | None, остаток текста)."""
    if m.reply_message:
        uid = m.reply_message.from_id
        return (uid if uid and uid > 0 else None), rest
    mm = MENTION.match(rest)
    if mm:
        return int(mm.group(1)), rest[mm.end():]
    mm = NAME.match(rest)
    if not mm:
        return None, rest
    try:
        res = await m.ctx_api.users.get(user_ids=[mm.group(1)])
        return (res[0].id if res else None), rest[mm.end():]
    except Exception:
        return None, rest


def clean_name(s: str) -> str:
    return re.sub(r"[^\w\s\-\.\(\)]", "", s).strip()


FONT_URLS = {
    "DejaVuSans.ttf": [
        "https://github.com/shwars/simpdf/raw/refs/heads/main/fonts/DejaVuSans.ttf",
    ],
    "DejaVuSans-Bold.ttf": [
        "https://github.com/shwars/simpdf/raw/refs/heads/main/fonts/DejaVuSans-Bold.ttf",
        "https://huggingface.co/spaces/MK-316/QRcode-with-title/resolve/main/dejavu-sans-bold.ttf",
    ],
}


def ensure_fonts():
    """Если шрифтов нет (например, на хостинге) — скачать DejaVu рядом с bot.py."""
    for fname, urls in FONT_URLS.items():
        if os.path.exists(fname):
            continue
        for url in urls:
            try:
                r = httpx.get(url, follow_redirects=True, timeout=30)
                if r.status_code == 200 and len(r.content) > 100_000:
                    with open(fname, "wb") as fh:
                        fh.write(r.content)
                    print(f"Шрифт скачан: {fname}", flush=True)
                    break
            except Exception as e:  # noqa
                print(f"Не удалось скачать {fname}: {e}", flush=True)
        else:
            print(f"ВНИМАНИЕ: шрифт {fname} не найден и не скачался. Загрузи его рядом с bot.py вручную.", flush=True)


def load_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise FileNotFoundError("Положи DejaVuSans.ttf и DejaVuSans-Bold.ttf рядом с bot.py")


def render_table(rows: list) -> bytes:
    bold = FONT_BOLD + FONT_REG  # если жирного шрифта нет — берём обычный
    f, fb, ft = load_font(FONT_REG, 20), load_font(bold, 20), load_font(bold, 28)
    cols = [("№", 50), ("Фракция", 290), ("Ник", 230), ("Должность", 190),
            ("Назначен", 130), ("Конец срока", 150), ("Статус", 120)]
    w = sum(c[1] for c in cols) + 20
    title_h, head_h, row_h = 70, 46, 42
    h = title_h + head_h + row_h * len(rows) + 20
    img = Image.new("RGB", (w, h), "#ffffff")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, title_h], fill="#161b33")
    d.text((w // 2, title_h // 2), "ЛИДЕРЫ ФРАКЦИЙ — ENVY", font=ft, fill="#ffffff", anchor="mm")
    y = title_h
    d.rectangle([0, y, w, y + head_h], fill="#2c3e50")
    x = 10
    for name, cw in cols:
        d.text((x + 6, y + head_h // 2), name, font=fb, fill="#ffffff", anchor="lm")
        x += cw
    y += head_h
    for i, r in enumerate(rows):
        d.rectangle([0, y, w, y + row_h], fill="#eaf1fb" if i % 2 == 0 else "#f7f9fd")
        vals = [str(r["n"]), clean_name(r["faction"]), r["nick"], r["pos"], r["date"], r["end"], r["status"]]
        x = 10
        for j, (v, (_, cw)) in enumerate(zip(vals, cols)):
            color = "#1a1a1a"
            if j == 6:
                color = "#1e8e3e" if v == "Активен" else "#d93025"
            d.text((x + 6, y + row_h // 2), v, font=f, fill=color, anchor="lm")
            x += cw
        y += row_h
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def log_reply(peer_id: int, text: str, photo: bool = False):
    t = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    body = text.replace("\n", "\n    ")
    print(f"[{t}] БОТ -> peer={peer_id}{' (+фото)' if photo else ''}:\n    {body}", flush=True)


async def upload_photo(png: bytes, peer_id: int) -> str:
    """Загрузка картинки в VK напрямую через API (не зависит от версии vkbottle)."""
    r = await bot.api.request("photos.getMessagesUploadServer", {"peer_id": peer_id})
    url = r["response"]["upload_url"]
    async with httpx.AsyncClient(timeout=60) as c:
        up = (await c.post(url, files={"photo": ("table.png", png, "image/png")})).json()
    r = await bot.api.request("photos.saveMessagesPhoto",
                              {"server": up["server"], "photo": up["photo"], "hash": up["hash"]})
    p = r["response"][0]
    att = f"photo{p['owner_id']}_{p['id']}"
    if p.get("access_key"):
        att += f"_{p['access_key']}"
    return att


def log_cmd(m: Message, text: str, allowed: bool):
    t = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"[{t}] peer={m.peer_id} user={m.from_id} {'OK    ' if allowed else 'DENIED'} | {text}", flush=True)


# ---------- обработчик ----------
@bot.on.message()
async def handler(m: Message):
    text = (m.text or "").strip()
    text = re.sub(r"^\[club\d+\|[^\]]*\]\s*", "", text)  # убрать упоминание бота
    if not text.startswith("/"):
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    allowed = has_access(m.from_id)
    log_cmd(m, text, allowed)  # в консоли видны ВСЕ команды
    if not allowed:
        return  # нет доступа — бот молчит
    if cmd != "/чат" and not chat_known(m.peer_id):
        return  # чат не добавлен — бот молчит

    async def out(s, **kw):
        log_reply(m.peer_id, s, photo="attachment" in kw)
        return await m.answer(s, disable_mentions=True, **kw)

    # ----- /help -----
    if cmd == "/help":
        await out(HELP)

    # ----- /чат -----
    elif cmd == "/чат":
        if m.peer_id < 2_000_000_000:
            await out("Эту команду нужно писать в беседе.")
            return
        cur = db.execute("INSERT OR IGNORE INTO chats VALUES (?)", (m.peer_id,))
        db.commit()
        info = f"ID чата: {m.peer_id} (номер беседы: {m.peer_id - 2_000_000_000})"
        if cur.rowcount:
            await out(f"✅ Чат добавлен в базу. Бот работает в этой беседе.\n{info}")
        else:
            await out(f"ℹ️ Этот чат уже в базе.\n{info}")

    # ----- /vd /unvd -----
    elif cmd in ("/vd", "/unvd"):
        if m.from_id not in ADMIN_IDS:
            await out("⛔ Только администратор бота может менять доступ.")
            return
        uid, _ = await get_target(m, rest)
        if not uid:
            await out("Укажи пользователя: /vd @user или ответом на сообщение.")
            return
        if cmd == "/vd":
            db.execute("INSERT OR IGNORE INTO users VALUES (?)", (uid,))
            await out(f"✅ [id{uid}|Пользователь] получил доступ к командам.")
        else:
            db.execute("DELETE FROM users WHERE user_id=?", (uid,))
            await out(f"✅ У [id{uid}|пользователя] забран доступ.")
        db.commit()

    # ----- /sl -----
    elif cmd == "/sl":
        uid, rest2 = await get_target(m, rest)
        if not uid:
            await out("Не нашёл пользователя. Формат: /sl @user 3 Nick_Name 01.10\n\n" + FACTION_HINT)
            return
        mm = re.fullmatch(r"(\d{1,2})\s+(\S+)\s+(\d{1,2})\.(\d{1,2})", rest2.strip())
        if not mm or not NICK.match(mm.group(2)):
            await out("Формат: /sl @user <фракция 1-14> Nick_Name дд.мм\nПример: /sl @durov 3 Ivan_Petrov 01.10\n\n"
                      + FACTION_HINT)
            return
        n, nick, day, month = int(mm.group(1)), mm.group(2), int(mm.group(3)), int(mm.group(4))
        res = await sheet("set", vk_id=uid, faction=n, nick=nick, day=day, month=month)
        if res.get("ok"):
            extra = f"\n♻️ Заменён: {res['replaced']}" if res.get("replaced") else ""
            await out(f"✅ {nick} назначен: {clean_name(res['faction'])}\n"
                      f"Статус: Активен, срок до {res['end']}{extra}")
        else:
            await out(f"❌ {res.get('error', 'Ошибка')}")

    # ----- /rl -----
    elif cmd == "/rl":
        uid, _ = await get_target(m, rest)
        if not uid:
            await out("Укажи пользователя: /rl @user или ответом на сообщение.")
            return
        res = await sheet("remove", vk_id=uid)
        if res.get("ok"):
            await out(f"🗑 {res['nick']} убран из таблицы ({clean_name(res['faction'])}).")
        else:
            await out(f"❌ {res.get('error', 'Ошибка')}")

    # ----- /сменить -----
    elif cmd == "/сменить":
        uid, rest2 = await get_target(m, rest)
        nick = rest2.strip()
        if not uid or not NICK.match(nick):
            await out("Формат: /сменить @user Новый_Ник (или ответом на сообщение)")
            return
        res = await sheet("rename", vk_id=uid, nick=nick)
        if res.get("ok"):
            await out(f"✏️ Ник изменён: {res['old']} → {nick} ({clean_name(res['faction'])})")
        else:
            await out(f"❌ {res.get('error', 'Ошибка')}")

    # ----- /список -----
    elif cmd == "/список":
        res = await sheet("list")
        if not res.get("ok"):
            await out(f"❌ {res.get('error', 'Ошибка')}")
            return
        rows = res["rows"]
        if not rows:
            await out("Список пуст.")
            return
        lines = [f"{r['n']}. [id{r['vk_id']}|{r['nick']}] — {clean_name(r['faction'])} ({r['status']})"
                 for r in rows]
        caption = "📋 Лидеры фракций:\n" + "\n".join(lines)
        try:
            png = await asyncio.to_thread(render_table, rows)
            photo = await upload_photo(png, m.peer_id)
            await out(caption, attachment=photo)
        except Exception:
            print("Ошибка отправки фото:", flush=True)
            traceback.print_exc()
            await out(caption)


# ---------- уведомления об истёкшем сроке ----------
CHECK_EVERY = 300  # секунд (5 минут)


async def expiry_watcher():
    await asyncio.sleep(10)
    while True:
        try:
            res = await sheet("list")
            if res.get("ok"):
                chats = [r[0] for r in db.execute("SELECT peer_id FROM chats")]
                for r in res["rows"]:
                    if r["status"] != "Истёк" or not chats:
                        continue
                    key = (str(r["vk_id"]), r["end"])
                    if db.execute("SELECT 1 FROM notified WHERE vk_id=? AND end=?", key).fetchone():
                        continue
                    text = (f"@all у [id{r['vk_id']}|{r['nick']}] ({clean_name(r['faction'])}) "
                            f"истёк срок, пора его повышать 📈")
                    for pid in chats:
                        try:
                            log_reply(pid, text)
                            await bot.api.messages.send(peer_id=pid, message=text, random_id=0)
                        except Exception as e:  # noqa
                            print(f"Не удалось отправить в {pid}: {e}", flush=True)
                    db.execute("INSERT OR IGNORE INTO notified VALUES (?, ?)", key)
                    db.commit()
                    print(f"[уведомление] истёк срок: {r['nick']} ({r['vk_id']})", flush=True)
        except Exception as e:  # noqa
            print("Ошибка проверки сроков:", e, flush=True)
        await asyncio.sleep(CHECK_EVERY)


async def main():
    await asyncio.to_thread(ensure_fonts)
    asyncio.create_task(expiry_watcher())
    print("Бот запущен. Ожидаю команды...", flush=True)
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
