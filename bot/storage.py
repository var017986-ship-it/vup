from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class Mirror:
    id: int
    token: str
    telegram_bot_id: int
    username: str
    owner_id: int
    enabled: bool


@dataclass(frozen=True)
class Section:
    id: int
    mirror_id: int
    title: str
    body: str


@dataclass(frozen=True)
class MenuButton:
    id: int
    section_id: int
    title: str
    emoji: str
    action_type: str
    payload: str
    style: str


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript("""
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS mirrors (
                  id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, telegram_bot_id INTEGER NOT NULL UNIQUE,
                  username TEXT NOT NULL, owner_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS settings (
                  mirror_id INTEGER PRIMARY KEY REFERENCES mirrors(id) ON DELETE CASCADE,
                  welcome_text TEXT NOT NULL DEFAULT 'Добро пожаловать!',
                  root_section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS sections (
                  id INTEGER PRIMARY KEY, mirror_id INTEGER NOT NULL REFERENCES mirrors(id) ON DELETE CASCADE,
                  title TEXT NOT NULL, body TEXT NOT NULL, sort_order INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS buttons (
                  id INTEGER PRIMARY KEY, section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
                  title TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
                  payload TEXT NOT NULL, style TEXT NOT NULL DEFAULT 'normal', sort_order INTEGER NOT NULL
                );
            """)
            await db.commit()

    async def create_mirror(self, token: str, bot_id: int, username: str, owner_id: int) -> Mirror:
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT INTO mirrors (token, telegram_bot_id, username, owner_id) VALUES (?, ?, ?, ?)",
                (token, bot_id, username, owner_id),
            )
            mirror_id = cursor.lastrowid
            await db.execute("INSERT INTO settings (mirror_id) VALUES (?)", (mirror_id,))
            await db.commit()
        return Mirror(mirror_id, token, bot_id, username, owner_id, True)

    async def list_mirrors(self, enabled_only: bool = False) -> list[Mirror]:
        query = "SELECT id, token, telegram_bot_id, username, owner_id, enabled FROM mirrors"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        async with self._connect() as db:
            rows = await (await db.execute(query)).fetchall()
        return [Mirror(*row[:-1], bool(row[-1])) for row in rows]

    async def get_mirror(self, mirror_id: int) -> Mirror | None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT id, token, telegram_bot_id, username, owner_id, enabled FROM mirrors WHERE id = ?", (mirror_id,))).fetchone()
        return Mirror(*row[:-1], bool(row[-1])) if row else None

    async def set_mirror_enabled(self, mirror_id: int, enabled: bool) -> None:
        await self._execute("UPDATE mirrors SET enabled = ? WHERE id = ?", (enabled, mirror_id))

    async def delete_mirror(self, mirror_id: int) -> None:
        await self._execute("DELETE FROM mirrors WHERE id = ?", (mirror_id,))

    async def welcome(self, mirror_id: int) -> str:
        async with self._connect() as db:
            return (await (await db.execute("SELECT welcome_text FROM settings WHERE mirror_id = ?", (mirror_id,))).fetchone())[0]

    async def set_welcome(self, mirror_id: int, text: str) -> None:
        await self._execute("UPDATE settings SET welcome_text = ? WHERE mirror_id = ?", (text, mirror_id))

    async def root(self, mirror_id: int) -> Section | None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT s.id,s.mirror_id,s.title,s.body FROM sections s JOIN settings x ON x.root_section_id=s.id WHERE x.mirror_id=?", (mirror_id,))).fetchone()
        return Section(*row) if row else None

    async def sections(self, mirror_id: int) -> list[Section]:
        async with self._connect() as db:
            rows = await (await db.execute("SELECT id,mirror_id,title,body FROM sections WHERE mirror_id=? ORDER BY sort_order,id", (mirror_id,))).fetchall()
        return [Section(*row) for row in rows]

    async def section(self, section_id: int) -> Section | None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT id,mirror_id,title,body FROM sections WHERE id=?", (section_id,))).fetchone()
        return Section(*row) if row else None

    async def add_section(self, mirror_id: int, title: str, body: str) -> int:
        return await self._insert("INSERT INTO sections (mirror_id,title,body,sort_order) VALUES (?, ?, ?, COALESCE((SELECT MAX(sort_order)+1 FROM sections WHERE mirror_id=?),1))", (mirror_id, title, body, mirror_id))

    async def set_root(self, mirror_id: int, section_id: int) -> None:
        section = await self.section(section_id)
        if not section or section.mirror_id != mirror_id:
            raise ValueError("Раздел не найден в этом боте.")
        await self._execute("UPDATE settings SET root_section_id=? WHERE mirror_id=?", (section_id, mirror_id))

    async def buttons(self, section_id: int) -> list[MenuButton]:
        async with self._connect() as db:
            rows = await (await db.execute("SELECT id,section_id,title,emoji,action_type,payload,style FROM buttons WHERE section_id=? ORDER BY sort_order,id", (section_id,))).fetchall()
        return [MenuButton(*row) for row in rows]

    async def button(self, button_id: int) -> MenuButton | None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT id,section_id,title,emoji,action_type,payload,style FROM buttons WHERE id=?", (button_id,))).fetchone()
        return MenuButton(*row) if row else None

    async def add_button(self, section_id: int, title: str, emoji: str, action_type: str, payload: str, style: str) -> int:
        return await self._insert("INSERT INTO buttons (section_id,title,emoji,action_type,payload,style,sort_order) VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(sort_order)+1 FROM buttons WHERE section_id=?),1))", (section_id,title,emoji,action_type,payload,style,section_id))

    async def update_section(self, section_id: int, field: str, value: str) -> None:
        if field not in {"title", "body"}:
            raise ValueError("Недопустимое поле раздела.")
        await self._execute(f"UPDATE sections SET {field} = ? WHERE id = ?", (value, section_id))

    async def delete_section(self, section_id: int) -> bool:
        async with self._connect() as db:
            root = await (await db.execute("SELECT COUNT(*) FROM settings WHERE root_section_id = ?", (section_id,))).fetchone()
            links = await (await db.execute("SELECT COUNT(*) FROM buttons WHERE action_type = 'submenu' AND payload = ?", (str(section_id),))).fetchone()
            if root[0] or links[0]:
                return False
            await db.execute("DELETE FROM sections WHERE id = ?", (section_id,))
            await db.commit()
            return True

    async def update_button(self, button_id: int, field: str, value: str) -> None:
        if field not in {"title", "emoji", "style"}:
            raise ValueError("Недопустимое поле кнопки.")
        await self._execute(f"UPDATE buttons SET {field} = ? WHERE id = ?", (value, button_id))

    async def update_button_action(self, button_id: int, kind: str, payload: str) -> None:
        if kind not in {"text", "url", "submenu"}:
            raise ValueError("Недопустимый тип кнопки.")
        await self._execute("UPDATE buttons SET action_type = ?, payload = ? WHERE id = ?", (kind, payload, button_id))

    async def delete_button(self, button_id: int) -> None:
        await self._execute("DELETE FROM buttons WHERE id = ?", (button_id,))

    async def move_section(self, section_id: int, direction: str) -> None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT mirror_id, sort_order FROM sections WHERE id = ?", (section_id,))).fetchone()
            if not row:
                return
            operator = "<" if direction == "up" else ">"
            order = "DESC" if direction == "up" else "ASC"
            other = await (await db.execute(
                f"SELECT id, sort_order FROM sections WHERE mirror_id = ? AND sort_order {operator} ? ORDER BY sort_order {order}, id {order} LIMIT 1",
                (row[0], row[1]),
            )).fetchone()
            if other:
                await db.execute("UPDATE sections SET sort_order = ? WHERE id = ?", (other[1], section_id))
                await db.execute("UPDATE sections SET sort_order = ? WHERE id = ?", (row[1], other[0]))
                await db.commit()

    async def move_button(self, button_id: int, direction: str) -> None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT section_id, sort_order FROM buttons WHERE id = ?", (button_id,))).fetchone()
            if not row:
                return
            operator = "<" if direction == "up" else ">"
            order = "DESC" if direction == "up" else "ASC"
            other = await (await db.execute(
                f"SELECT id, sort_order FROM buttons WHERE section_id = ? AND sort_order {operator} ? ORDER BY sort_order {order}, id {order} LIMIT 1",
                (row[0], row[1]),
            )).fetchone()
            if other:
                await db.execute("UPDATE buttons SET sort_order = ? WHERE id = ?", (other[1], button_id))
                await db.execute("UPDATE buttons SET sort_order = ? WHERE id = ?", (row[1], other[0]))
                await db.commit()

    async def _execute(self, query: str, values: tuple[object, ...]) -> None:
        async with self._connect() as db:
            await db.execute(query, values)
            await db.commit()

    async def _insert(self, query: str, values: tuple[object, ...]) -> int:
        async with self._connect() as db:
            cursor = await db.execute(query, values)
            await db.commit()
            return cursor.lastrowid
