import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_VALUE = os.getenv("ADMIN_ID", os.getenv("ADMIN_TELEGRAM_ID", "0")).strip()
try:
    ADMIN_ID = int(ADMIN_VALUE)
except ValueError:
    ADMIN_ID = 0
DB_PATH = Path(os.getenv("DATABASE_PATH", "bot.db"))

if not TOKEN or ADMIN_ID <= 0:
    raise RuntimeError("Set BOT_TOKEN and numeric ADMIN_ID in .env or hosting variables")


class DB:
    def __init__(self) -> None:
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS mirrors(id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, bot_id INTEGER UNIQUE NOT NULL, username TEXT NOT NULL, owner_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS settings(mirror_id INTEGER PRIMARY KEY, welcome TEXT NOT NULL DEFAULT 'Добро пожаловать!', root_id INTEGER);
        CREATE TABLE IF NOT EXISTS sections(id INTEGER PRIMARY KEY, mirror_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, position INTEGER NOT NULL, FOREIGN KEY(mirror_id) REFERENCES mirrors(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS buttons(id INTEGER PRIMARY KEY, section_id INTEGER NOT NULL, title TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, payload TEXT NOT NULL, style TEXT NOT NULL DEFAULT 'normal', position INTEGER NOT NULL, FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE);
        """)
        self.db.commit()

    def mirrors(self): return self.db.execute("SELECT * FROM mirrors ORDER BY id").fetchall()
    def mirror(self, mid): return self.db.execute("SELECT * FROM mirrors WHERE id=?", (mid,)).fetchone()
    def enabled(self): return [m for m in self.mirrors() if m["enabled"]]
    def section(self, sid): return self.db.execute("SELECT * FROM sections WHERE id=?", (sid,)).fetchone()
    def sections(self, mid): return self.db.execute("SELECT * FROM sections WHERE mirror_id=? ORDER BY position,id", (mid,)).fetchall()
    def buttons(self, sid): return self.db.execute("SELECT * FROM buttons WHERE section_id=? ORDER BY position,id", (sid,)).fetchall()
    def root(self, mid): return self.db.execute("SELECT s.* FROM sections s JOIN settings x ON x.root_id=s.id WHERE x.mirror_id=?", (mid,)).fetchone()
    def welcome(self, mid): return self.db.execute("SELECT welcome FROM settings WHERE mirror_id=?", (mid,)).fetchone()[0]
    def commit(self): self.db.commit()


db = DB()
controller_router = Router()
mirror_routers: dict[int, Router] = {}
mirror_tasks: dict[int, asyncio.Task] = {}


def button(text, data=None, url=None, style="normal"):
    values = {"text": text}
    if data: values["callback_data"] = data
    if url: values["url"] = url
    if style != "normal": values["style"] = style
    return InlineKeyboardButton(**values)


def markup(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def admin_ok(user_id): return user_id == ADMIN_ID
def owner_ok(user_id, mirror): return mirror and user_id == mirror["owner_id"]


async def controller_menu(message):
    await message.answer("Главная панель", reply_markup=markup([[button("Добавить зеркало", "c:add")], [button("Мои зеркала", "c:list")]]))


class AddMirror(StatesGroup): token = State(); owner = State()
class AddSection(StatesGroup): title = State(); body = State()
class AddButton(StatesGroup): title = State(); emoji = State(); kind = State(); payload = State(); style = State()


@controller_router.message(Command("admin"))
async def controller_admin(message: Message):
    if admin_ok(message.from_user.id): await controller_menu(message)


@controller_router.callback_query(F.data == "c:add")
async def mirror_add_start(query: CallbackQuery, state: FSMContext):
    if not admin_ok(query.from_user.id): return
    await state.set_state(AddMirror.token); await query.message.answer("Отправьте токен бота из @BotFather.\n/cancel - отмена"); await query.answer()


@controller_router.message(AddMirror.token)
async def mirror_token(message: Message, state: FSMContext):
    if not admin_ok(message.from_user.id): return
    token = (message.text or "").strip()
    try:
        probe = Bot(token); info = await probe.get_me(); await probe.session.close()
        if info.id == (await Bot(TOKEN).get_me()).id: raise ValueError
    except Exception:
        await message.answer("Неверный токен или это токен главного бота."); return
    await state.update_data(token=token, bot_id=info.id, username=info.username or info.first_name); await state.set_state(AddMirror.owner); await message.answer("Введите числовой Telegram ID владельца:")


@controller_router.message(AddMirror.owner)
async def mirror_owner(message: Message, state: FSMContext):
    if not admin_ok(message.from_user.id): return
    try: owner = int((message.text or "").strip()); assert owner > 0
    except (ValueError, AssertionError): await message.answer("ID должен быть положительным числом."); return
    data = await state.get_data()
    try:
        cur = db.db.execute("INSERT INTO mirrors(token,bot_id,username,owner_id) VALUES(?,?,?,?)", (data["token"],data["bot_id"],data["username"],owner)); mid=cur.lastrowid; db.db.execute("INSERT INTO settings(mirror_id) VALUES(?)",(mid,)); db.commit()
        await start_mirror(db.mirror(mid))
    except Exception: await message.answer("Не удалось добавить зеркало."); await state.clear(); return
    await state.clear(); await message.answer(f"Зеркало @{data['username']} добавлено.")


@controller_router.callback_query(F.data == "c:list")
async def mirror_list(query: CallbackQuery):
    if not admin_ok(query.from_user.id): return
    rows = [[button(f"{'🟢' if m['enabled'] else '🔴'} @{m['username']}", f"c:item:{m['id']}")] for m in db.mirrors()]
    rows.append([button("Назад", "c:home")]); await query.message.edit_text("Зеркала:", reply_markup=markup(rows)); await query.answer()


@controller_router.callback_query(F.data.startswith("c:item:"))
async def mirror_item(query: CallbackQuery):
    if not admin_ok(query.from_user.id): return
    m=db.mirror(int(query.data.split(":")[-1])); action="Выключить" if m["enabled"] else "Включить"
    await query.message.edit_text(f"@{m['username']}\nВладелец: {m['owner_id']}\nСтатус: {'включён' if m['enabled'] else 'выключен'}", reply_markup=markup([[button(action,f"c:toggle:{m['id']}")],[button("Удалить",f"c:delete:{m['id']}")],[button("Назад","c:list")]])); await query.answer()


@controller_router.callback_query(F.data.startswith("c:toggle:"))
async def mirror_toggle(query: CallbackQuery):
    if not admin_ok(query.from_user.id): return
    mid=int(query.data.split(":")[-1]); m=db.mirror(mid); db.db.execute("UPDATE mirrors SET enabled=? WHERE id=?",(0 if m["enabled"] else 1,mid)); db.commit()
    if m["enabled"]: await stop_mirror(mid)
    else: await start_mirror(db.mirror(mid))
    await mirror_list(query)


@controller_router.callback_query(F.data.startswith("c:delete:"))
async def mirror_delete(query: CallbackQuery):
    if not admin_ok(query.from_user.id): return
    mid=int(query.data.split(":")[-1]); await stop_mirror(mid); db.db.execute("DELETE FROM mirrors WHERE id=?",(mid,)); db.commit(); await mirror_list(query)


@controller_router.callback_query(F.data == "c:home")
async def controller_home(query: CallbackQuery):
    if admin_ok(query.from_user.id): await query.message.edit_text("Главная панель", reply_markup=markup([[button("Добавить зеркало", "c:add")],[button("Мои зеркала", "c:list")]]))
    await query.answer()


def create_mirror_router(m):
    r=Router(name=f"mirror_{m['id']}")
    mid=m["id"]
    @r.message(Command("start"))
    async def start(message):
        root=db.root(mid)
        if not root: await message.answer("Бот ещё не настроен."); return
        await message.answer(db.welcome(mid)); await show_section(message,root["id"])
    @r.callback_query(F.data.startswith("p:"))
    async def public(query):
        _, kind, value, previous=query.data.split(":")
        b=db.db.execute("SELECT b.*,s.mirror_id FROM buttons b JOIN sections s ON s.id=b.section_id WHERE b.id=?",(int(value),)).fetchone()
        if not b or b["mirror_id"] != mid: await query.answer(); return
        if kind=="text": await query.message.answer(b["payload"])
        else: await show_section(query.message,db.section(int(value)),int(previous) if previous!="0" else None,True)
        await query.answer()
    @r.message(Command("admin"))
    async def owner_admin(message):
        if owner_ok(message.from_user.id,m): await message.answer("Панель владельца",reply_markup=markup([[button("Разделы",f"o:sections:{mid}")]]))
    @r.callback_query(F.data.startswith("o:sections:"))
    async def owner_sections(query):
        if owner_ok(query.from_user.id,m): await show_owner_sections(query.message,mid); await query.answer()
    @r.callback_query(F.data == f"o:new:{mid}")
    async def section_start(query,state):
        if owner_ok(query.from_user.id,m): await state.set_state(AddSection.title); await query.message.answer("Название раздела:"); await query.answer()
    @r.message(AddSection.title)
    async def section_title(message,state):
        if not owner_ok(message.from_user.id,m): return
        await state.update_data(title=(message.text or "")[:64]); await state.set_state(AddSection.body); await message.answer("Текст раздела:")
    @r.message(AddSection.body)
    async def section_body(message,state):
        if not owner_ok(message.from_user.id,m): return
        d=await state.get_data(); cur=db.db.execute("INSERT INTO sections(mirror_id,title,body,position) VALUES(?,?,?,COALESCE((SELECT MAX(position)+1 FROM sections WHERE mirror_id=?),1))",(mid,d["title"],(message.text or "")[:4096],mid)); sid=cur.lastrowid
        if not db.root(mid): db.db.execute("UPDATE settings SET root_id=? WHERE mirror_id=?",(sid,mid)); db.commit()
        await state.clear(); await message.answer("Раздел создан.")
    @r.callback_query(F.data.startswith(f"o:buttons:{mid}:"))
    async def owner_buttons(query):
        if owner_ok(query.from_user.id,m): await show_owner_buttons(query.message,int(query.data.split(":")[-1])); await query.answer()
    @r.callback_query(F.data.startswith(f"o:newbutton:{mid}:"))
    async def button_start(query,state):
        if owner_ok(query.from_user.id,m): await state.update_data(section_id=int(query.data.split(":")[-1])); await state.set_state(AddButton.title); await query.message.answer("Название кнопки:"); await query.answer()
    @r.message(AddButton.title)
    async def button_title(message,state):
        if owner_ok(message.from_user.id,m): await state.update_data(title=(message.text or "")[:64]); await state.set_state(AddButton.emoji); await message.answer("Эмодзи или -:")
    @r.message(AddButton.emoji)
    async def button_emoji(message,state):
        if owner_ok(message.from_user.id,m): await state.update_data(emoji="" if message.text=="-" else (message.text or "")[:16]); await state.set_state(AddButton.kind); await message.answer("Тип: text, url или submenu")
    @r.message(AddButton.kind)
    async def button_kind(message,state):
        kind=(message.text or "").lower().strip()
        if kind not in ("text","url","submenu"): await message.answer("Введите text, url или submenu"); return
        await state.update_data(kind=kind); await state.set_state(AddButton.payload); await message.answer("Введите текст, ссылку или ID раздела:")
    @r.message(AddButton.payload)
    async def button_payload(message,state):
        d=await state.get_data(); payload=(message.text or "").strip()
        if d["kind"]=="url" and urlparse(payload).scheme not in ("http","https"): await message.answer("Нужна ссылка http:// или https://"); return
        if d["kind"]=="submenu" and (not db.section(int(payload)) or db.section(int(payload))["mirror_id"]!=mid): await message.answer("Раздел не найден"); return
        await state.update_data(payload=payload); await state.set_state(AddButton.style); await message.answer("Стиль: normal, primary, success или danger")
    @r.message(AddButton.style)
    async def button_save(message,state):
        d=await state.get_data(); style=(message.text or "").lower().strip()
        if style not in ("normal","primary","success","danger"): await message.answer("Неверный стиль"); return
        db.db.execute("INSERT INTO buttons(section_id,title,emoji,kind,payload,style,position) VALUES(?,?,?,?,?,?,COALESCE((SELECT MAX(position)+1 FROM buttons WHERE section_id=?),1))",(d["section_id"],d["title"],d["emoji"],d["kind"],d["payload"],style,d["section_id"])); db.commit(); await state.clear(); await message.answer("Кнопка создана.")
    @r.message(Command("cancel"))
    async def cancel(message,state):
        if owner_ok(message.from_user.id,m): await state.clear(); await message.answer("Отменено.")
    return r


async def show_section(message, section, back=None, edit=False):
    rows=[]
    for b in db.buttons(section["id"]):
        text=f"{b['emoji']} {b['title']}".strip()
        if b["kind"]=="url": rows.append([button(text,url=b["payload"],style=b["style"])])
        else: rows.append([button(text,f"p:{'text' if b['kind']=='text' else 'menu'}:{b['id'] if b['kind']=='text' else b['payload']}:{section['id'] if b['kind']=='submenu' else 0}",style=b["style"])])
    if back: rows.append([button("Назад",f"p:menu:{back}:0")])
    text=f"<b>{section['title']}</b>\n\n{section['body']}"
    if edit: await message.edit_text(text,reply_markup=markup(rows))
    else: await message.answer(text,reply_markup=markup(rows))


async def show_owner_sections(message,mid):
    rows=[[button(f"{s['title']} [ID {s['id']}]",f"o:buttons:{mid}:{s['id']}")] for s in db.sections(mid)]; rows += [[button("Создать раздел",f"o:new:{mid}")]]; await message.edit_text("Разделы:",reply_markup=markup(rows))
async def show_owner_buttons(message,sid):
    s=db.section(sid); rows=[[button(f"{b['emoji']} {b['title']}","none")] for b in db.buttons(sid)]; rows += [[button("Создать кнопку",f"o:newbutton:{s['mirror_id']}:{sid}")]]; await message.edit_text(f"Кнопки раздела {s['title']}:",reply_markup=markup(rows))


async def start_mirror(m):
    if m["id"] in mirror_tasks: return
    bot=Bot(m["token"],default=DefaultBotProperties(parse_mode=ParseMode.HTML)); d=Dispatcher(storage=MemoryStorage()); r=create_mirror_router(m); d.include_router(r); mirror_routers[m["id"]]=r
    async def run():
        try: await d.start_polling(bot,allowed_updates=d.resolve_used_update_types())
        except asyncio.CancelledError: pass
        finally: await bot.session.close()
    mirror_tasks[m["id"]]=asyncio.create_task(run())
async def stop_mirror(mid):
    task=mirror_tasks.pop(mid,None)
    if task: task.cancel(); await asyncio.gather(task,return_exceptions=True)


async def main():
    logging.basicConfig(level=logging.INFO); controller=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); d=Dispatcher(storage=MemoryStorage()); d.include_router(controller_router)
    for m in db.enabled(): await start_mirror(m)
    try: await d.start_polling(controller)
    finally:
        for mid in list(mirror_tasks): await stop_mirror(mid)
        await controller.session.close(); db.db.close()


if __name__ == "__main__": asyncio.run(main())
