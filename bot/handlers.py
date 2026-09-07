from contextlib import suppress
from dataclasses import replace
from sqlite3 import IntegrityError, SQLITE_CONSTRAINT_PRIMARYKEY, SQLITE_CONSTRAINT_UNIQUE

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.storage import Mirror, Storage
from bot.validation import validate_text, validate_url


def clipped(text: str, maximum: int) -> str:
    return text.encode("utf-16-le")[:maximum * 2].decode("utf-16-le", errors="ignore")


def checked(text: str, label: str, maximum: int) -> str:
    text = validate_text(text, label, maximum)
    if len(text.encode("utf-16-le")) // 2 > maximum:
        raise ValueError(f"{label}: максимум {maximum} символов.")
    return text


def key(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=clipped(text, 64) or "-", callback_data=data)


def keys(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def say(message: Message, text: str, rows=None, edit: bool = False):
    # Plain text avoids inherited HTML parsing; split using Telegram's UTF-16 budget.
    chunks = []
    while text:
        chunk = clipped(text, 4096)
        chunks.append(chunk)
        text = text[len(chunk):]
    result = message
    for index, chunk in enumerate(chunks or ["-"]):
        markup = keys(rows) if rows is not None and index == len(chunks) - 1 else None
        if edit and index == 0:
            try:
                result = await message.edit_text(chunk, parse_mode=None, reply_markup=markup)
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
        else:
            result = await message.answer(chunk, parse_mode=None, reply_markup=markup)
    return result if isinstance(result, Message) else message


def private_owner(event, owner_id: int) -> bool:
    message = event.message if isinstance(event, CallbackQuery) else event
    return (isinstance(message, Message) and message.chat.type == "private"
            and event.from_user is not None and event.from_user.id == owner_id
            and message.chat.id == owner_id)


class SafeHandlers(BaseMiddleware):
    def __init__(self, owner_id: int, controller: bool = False):
        self.owner_id = owner_id
        self.controller = controller

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        protected = self.controller or (
            isinstance(event, CallbackQuery) and (event.data or "").startswith("own:")
        )
        if (not isinstance(message, Message) or message.chat.type != "private"
                or (protected and not private_owner(event, self.owner_id))):
            if isinstance(event, CallbackQuery):
                with suppress(Exception):
                    await event.answer("Недоступно")
            return
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer()
        try:
            return await handler(event, data)
        except Exception:
            # API/transport exceptions can contain bot tokens. Never echo or log them.
            with suppress(Exception):
                await say(message, "Операция не выполнена. Повторите попытку или используйте /cancel.")


async def clear_flow(state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data({k: v for k, v in data.items() if k in {"history", "public_message_id"}})


class ControllerFlow(StatesGroup):
    token = State()
    owner_id = State()


class OwnerFlow(StatesGroup):
    value = State()
    kind = State()
    submenu = State()
    style = State()


def controller_keyboard() -> InlineKeyboardMarkup:
    return keys([[key("Добавить зеркало", "ctl:add")], [key("Мои зеркала", "ctl:list:0")]])


async def controller_menu(message: Message) -> None:
    await message.answer("Панель владельца конструктора", parse_mode=None, reply_markup=controller_keyboard())


def controller_router(storage: Storage, admin_id: int, lifecycle: "MirrorLifecycle") -> Router:
    router = Router(name="controller")
    router.message.outer_middleware(SafeHandlers(admin_id, True))
    router.callback_query.outer_middleware(SafeHandlers(admin_id, True))

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await say(message, "Отменено.")

    @router.message(Command("admin", "start"))
    async def panel(message: Message, state: FSMContext):
        await state.clear()
        await controller_menu(message)

    @router.message(ControllerFlow.token)
    async def token_received(message: Message, state: FSMContext, bot: Bot):
        token = (message.text or "").strip()
        with suppress(Exception):
            await message.delete()
        probe = None
        try:
            probe = Bot(token=token)
            if probe.id == bot.id:
                await say(message, "Нельзя добавить самого бота-конструктора.")
                return
            info = await probe.get_me()
        except Exception:
            await say(message, "Не удалось проверить токен. Проверьте его и повторите попытку.")
            return
        finally:
            if probe is not None:
                with suppress(Exception):
                    await probe.session.close()
        await state.update_data(token=token, bot_id=info.id, username=info.username or info.first_name)
        await state.set_state(ControllerFlow.owner_id)
        await say(message, f"Бот {info.username or info.first_name} найден. Отправьте положительный Telegram ID владельца.")

    @router.message(ControllerFlow.owner_id)
    async def owner_received(message: Message, state: FSMContext):
        raw = (message.text or "").strip()
        if not raw.isascii() or not raw.isdigit() or len(raw) > 16 or not 0 < int(raw) < 2**52:
            await say(message, "ID должен быть положительным целым числом Telegram.")
            return
        data = await state.get_data()
        if not all(k in data for k in ("token", "bot_id", "username")):
            await state.clear()
            return
        try:
            item = await storage.create_mirror(data["token"], data["bot_id"], data["username"], int(raw))
        except IntegrityError as error:
            await state.clear()
            if getattr(error, "sqlite_errorcode", None) in {SQLITE_CONSTRAINT_UNIQUE, SQLITE_CONSTRAINT_PRIMARYKEY}:
                await say(message, "Этот токен или бот уже добавлен.")
                return
            raise
        except Exception:
            await state.clear()
            raise
        await state.clear()
        try:
            await lifecycle.start(item)
        except Exception:
            await storage.set_mirror_enabled(item.id, False)
            with suppress(Exception):
                await lifecycle.stop(item.id)
            await say(message, "Зеркало сохранено, но запуск не удался. Повторите включение в списке зеркал.")
            return
        await say(message, f"Зеркало @{item.username} запущено. Владелец: {item.owner_id}.")

    @router.callback_query(F.data.startswith("ctl:"))
    async def controls(query: CallbackQuery, state: FSMContext):
        parts = (query.data or "").split(":")
        action = parts[1]
        if action == "add" and len(parts) == 2:
            await state.clear()
            await state.set_state(ControllerFlow.token)
            await say(query.message, "Вставьте токен нового бота из @BotFather.\n/cancel - отмена")
        elif action == "home" and len(parts) == 2:
            await state.clear()
            await controller_menu(query.message)
        elif action == "list" and len(parts) in {2, 3}:
            page = number(parts[2]) if len(parts) == 3 else 0
            if page is None:
                return
            items = await storage.list_mirrors()
            rows = [[key(f"{'Вкл' if x.enabled else 'Выкл'} @{x.username}", f"ctl:mirror:{x.id}")] for x in items[page:page + 20]]
            rows += pagination("ctl:list", page, len(items))
            rows.append([key("Назад", "ctl:home")])
            await say(query.message, "Зеркала:", rows, True)
        elif action in {"mirror", "toggle", "delete", "confirm"} and len(parts) == 3:
            item_id = number(parts[2])
            item = await storage.get_mirror(item_id) if item_id else None
            if not item:
                return
            if action == "toggle":
                try:
                    if item.enabled:
                        await lifecycle.stop(item.id)
                        await storage.set_mirror_enabled(item.id, False)
                    else:
                        await lifecycle.start(replace(item, enabled=True))
                        await storage.set_mirror_enabled(item.id, True)
                except Exception:
                    if not item.enabled:
                        with suppress(Exception):
                            await lifecycle.stop(item.id)
                        await storage.set_mirror_enabled(item.id, False)
                    await say(query.message, "Не удалось изменить состояние зеркала. Повторите попытку.")
                    return
                item = await storage.get_mirror(item.id)
            elif action == "delete":
                await say(query.message, "Удалить зеркало и все его данные?", [[key("Удалить", f"ctl:confirm:{item.id}")], [key("Отмена", f"ctl:mirror:{item.id}")]], True)
                return
            elif action == "confirm":
                await lifecycle.stop(item.id)
                await storage.delete_mirror(item.id)
                await say(query.message, "Зеркало удалено.", [[key("К списку", "ctl:list:0")]], True)
                return
            if item:
                await say(query.message, f"@{item.username}\nВладелец: {item.owner_id}\nСтатус: {'включён' if item.enabled else 'выключен'}", [
                    [key("Выключить" if item.enabled else "Включить", f"ctl:toggle:{item.id}")],
                    [key("Удалить", f"ctl:delete:{item.id}")], [key("Назад", "ctl:list:0")]], True)

    return router


def number(value: str) -> int | None:
    if value.isascii() and value.isdigit() and len(value) <= 16:
        return int(value)
    return None


def pagination(prefix: str, offset: int, total: int):
    row = []
    if offset > 0:
        row.append(key("Ранее", f"{prefix}:{max(0, offset - 20)}"))
    if offset + 20 < total:
        row.append(key("Далее", f"{prefix}:{offset + 20}"))
    return [row] if row else []


def owner_keyboard() -> InlineKeyboardMarkup:
    return keys([[key("Разделы и кнопки", "own:sections:0")],
                 [key("Приветственный текст", "own:welcome")],
                 [key("Имя бота", "own:name")], [key("Описание бота", "own:description")]])


def mirror_router(storage: Storage, mirror: Mirror) -> Router:
    router = Router(name=f"mirror_{mirror.id}")
    router.message.outer_middleware(SafeHandlers(mirror.owner_id))
    router.callback_query.outer_middleware(SafeHandlers(mirror.owner_id))

    async def section(section_id):
        item = await storage.section(section_id) if isinstance(section_id, int) and section_id > 0 else None
        return item if item and item.mirror_id == mirror.id else None

    async def button(button_id):
        item = await storage.button(button_id) if isinstance(button_id, int) and button_id > 0 else None
        return item if item and await section(item.section_id) else None

    async def valid_flow(data):
        if "section_id" in data and not await section(data["section_id"]):
            return False
        if "button_id" in data:
            item = await button(data["button_id"])
            if not item or item.section_id != data.get("section_id"):
                return False
        if data.get("kind") == "submenu" and "payload" in data:
            return bool(await section(number(data["payload"])))
        return True

    async def prompt(message, state, field, text, maximum):
        await state.update_data(field=field, maximum=maximum)
        await state.set_state(OwnerFlow.value)
        await say(message, text + "\n/cancel - отмена")

    async def choose(message, state, choice, offset=0):
        await state.set_state(getattr(OwnerFlow, choice))
        if choice == "kind":
            rows = [[key(label, f"own:pick:{value}")] for label, value in [("Текст", "text"), ("Ссылка", "url"), ("Подменю", "submenu")]]
        elif choice == "style":
            rows = [[key(label, f"own:pick:{value}")] for label, value in [("Обычный", "normal"), ("Синий", "primary"), ("Зелёный", "success"), ("Красный", "danger")]]
        else:
            items = await storage.sections(mirror.id)
            rows = [[key(x.title, f"own:pick:{x.id}")] for x in items[offset:offset + 20]]
            rows += pagination("own:targets", offset, len(items))
        sent = await say(message, "Выберите вариант:", rows)
        await state.update_data(choice_message_id=sent.message_id)

    async def save_action(message, state):
        data = await state.get_data()
        if not await valid_flow(data):
            await clear_flow(state)
            return
        if data.get("button_id"):
            await storage.update_button_action(data["button_id"], data["kind"], data["payload"])
            await clear_flow(state)
            await say(message, "Действие кнопки сохранено.")
        else:
            await choose(message, state, "style")

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        if private_owner(message, mirror.owner_id):
            await clear_flow(state)
            await say(message, "Отменено.")

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext):
        await clear_flow(state)
        await state.update_data(history=[], public_message_id=None)
        root = await storage.root(mirror.id)
        if not root or not await section(root.id):
            await say(message, "Бот настраивается владельцем.")
            return
        await say(message, await storage.welcome(mirror.id))
        sent = await show_public(message, storage, mirror.id, root.id)
        await state.update_data(history=[root.id], public_message_id=sent.message_id)

    @router.message(Command("admin"))
    async def panel(message: Message, state: FSMContext):
        if private_owner(message, mirror.owner_id):
            await clear_flow(state)
            await message.answer("Админ-панель вашего бота", parse_mode=None, reply_markup=owner_keyboard())

    @router.callback_query(F.data.startswith("pub:"))
    async def public_action(query: CallbackQuery, state: FSMContext):
        parts = (query.data or "").split(":")
        data = await state.get_data()
        history = data.get("history", [])
        if not history or data.get("public_message_id") != query.message.message_id:
            return
        if not all([await section(value) for value in history]):
            await state.update_data(history=[], public_message_id=None)
            return
        if len(parts) == 3 and parts[1] == "back" and number(parts[2]) == history[-1] and len(history) > 1:
            history = history[:-1]
        elif len(parts) == 4 and parts[1] == "button" and number(parts[3]) == history[-1]:
            item = await button(number(parts[2]))
            if not item or item.section_id != history[-1]:
                return
            if item.action_type == "text":
                await say(query.message, item.payload)
                return
            if item.action_type != "submenu":
                return
            target = await section(number(item.payload))
            if not target or len(history) >= 100:
                return
            history = history + [target.id]
        else:
            return
        sent = await show_public(query.message, storage, mirror.id, history[-1], len(history) > 1, True)
        await state.update_data(history=history, public_message_id=sent.message_id)

    @router.callback_query(F.data.startswith("own:"))
    async def owner_action(query: CallbackQuery, state: FSMContext):
        parts = (query.data or "").split(":")
        if len(parts) < 2:
            return
        action = parts[1]
        if action in {"pick", "targets"} and len(parts) == 3:
            data = await state.get_data()
            current = await state.get_state()
            if data.get("choice_message_id") != query.message.message_id or not await valid_flow(data):
                return
            value = parts[2]
            if action == "targets":
                offset = number(value)
                if current == OwnerFlow.submenu.state and offset is not None:
                    await choose(query.message, state, "submenu", offset)
                return
            if current == OwnerFlow.kind.state and value in {"text", "url", "submenu"}:
                await state.update_data(kind=value)
                if value == "submenu":
                    await choose(query.message, state, "submenu")
                else:
                    await prompt(query.message, state, "payload", "Текст ответа:" if value == "text" else "Ссылка:", 4096 if value == "text" else 2048)
            elif current == OwnerFlow.submenu.state and await section(number(value)):
                await state.update_data(payload=value)
                await save_action(query.message, state)
            elif current == OwnerFlow.style.state and value in {"normal", "primary", "success", "danger"}:
                if data.get("button_id"):
                    await storage.update_button(data["button_id"], "style", value)
                elif all(k in data for k in ("section_id", "title", "emoji", "kind", "payload")):
                    await storage.add_button(data["section_id"], data["title"], data["emoji"], data["kind"], data["payload"], value)
                else:
                    return
                await clear_flow(state)
                await say(query.message, "Кнопка сохранена.")
            return
        if action in {"home", "welcome", "name", "description", "new_section"} and len(parts) == 2:
            await clear_flow(state)
            if action == "home":
                await query.message.answer("Админ-панель вашего бота", parse_mode=None, reply_markup=owner_keyboard())
            elif action == "new_section":
                await state.update_data(operation="new_section")
                await prompt(query.message, state, "title", "Название раздела:", 64)
            else:
                await state.update_data(operation=action)
                await prompt(query.message, state, action, {"welcome": "Приветственный текст:", "name": "Новое имя бота:", "description": "Описание бота (или '-' для удаления):"}[action], {"welcome": 4096, "name": 64, "description": 512}[action])
            return
        if action == "sections" and len(parts) in {2, 3}:
            offset = number(parts[2]) if len(parts) == 3 else 0
            if offset is not None:
                await clear_flow(state)
                await show_sections(query.message, storage, mirror.id, offset)
            return
        if len(parts) < 3:
            return
        item_id = number(parts[2])
        if action in {"section", "root", "buttons", "new_button", "sedit", "sdelete", "sconfirm", "smove"}:
            item = await section(item_id)
            if not item:
                return
            if action == "section" and len(parts) == 3:
                await clear_flow(state)
                await say(query.message, item.title, [
                    [key("Название", f"own:sedit:{item.id}:title"), key("Текст", f"own:sedit:{item.id}:body")],
                    [key("Выше", f"own:smove:{item.id}:up"), key("Ниже", f"own:smove:{item.id}:down")],
                    [key("Сделать главным", f"own:root:{item.id}")], [key("Кнопки", f"own:buttons:{item.id}:0")],
                    [key("Удалить", f"own:sdelete:{item.id}")], [key("Назад", "own:sections:0")]], True)
            elif action == "root" and len(parts) == 3:
                await storage.set_root(mirror.id, item.id)
                await say(query.message, "Главное меню обновлено.")
            elif action == "buttons" and len(parts) in {3, 4}:
                offset = number(parts[3]) if len(parts) == 4 else 0
                if offset is not None:
                    await clear_flow(state)
                    await show_buttons(query.message, storage, mirror.id, item.id, offset)
            elif action == "new_button" and len(parts) == 3:
                await clear_flow(state)
                await state.update_data(operation="new_button", section_id=item.id)
                await prompt(query.message, state, "title", "Название кнопки:", 48)
            elif action == "sedit" and len(parts) == 4 and parts[3] in {"title", "body"}:
                await clear_flow(state)
                await state.update_data(operation="section", section_id=item.id)
                await prompt(query.message, state, parts[3], "Новое значение:", 64 if parts[3] == "title" else 4096)
            elif action == "smove" and len(parts) == 4 and parts[3] in {"up", "down"}:
                await storage.move_section(item.id, parts[3])
                await show_sections(query.message, storage, mirror.id)
            elif action == "sdelete" and len(parts) == 3:
                await say(query.message, "Удалить раздел? Главный раздел и разделы с входящими ссылками удалить нельзя.", [[key("Удалить", f"own:sconfirm:{item.id}")], [key("Отмена", f"own:section:{item.id}")]], True)
            elif action == "sconfirm" and len(parts) == 3:
                deleted = await storage.delete_section(item.id)
                await clear_flow(state)
                await say(query.message, "Раздел удалён." if deleted else "Раздел главный или на него ссылаются кнопки.")
            return
        if action in {"button", "bedit", "bdelete", "bconfirm", "bmove", "kind", "style"}:
            item = await button(item_id)
            if not item:
                return
            if action == "button" and len(parts) == 3:
                await clear_flow(state)
                await say(query.message, item.title, [
                    [key("Название", f"own:bedit:{item.id}:title"), key("Эмодзи", f"own:bedit:{item.id}:emoji")],
                    [key("Действие / содержимое", f"own:kind:{item.id}")], [key("Стиль", f"own:style:{item.id}")],
                    [key("Выше", f"own:bmove:{item.id}:up"), key("Ниже", f"own:bmove:{item.id}:down")],
                    [key("Удалить", f"own:bdelete:{item.id}")], [key("Назад", f"own:buttons:{item.section_id}:0")]], True)
            elif action in {"kind", "style"} and len(parts) == 3:
                await clear_flow(state)
                await state.update_data(operation="button", button_id=item.id, section_id=item.section_id)
                await choose(query.message, state, action)
            elif action == "bedit" and len(parts) == 4 and parts[3] in {"title", "emoji"}:
                await clear_flow(state)
                await state.update_data(operation="button", button_id=item.id, section_id=item.section_id)
                await prompt(query.message, state, parts[3], "Новое значение (эмодзи: '-' для удаления):", 48 if parts[3] == "title" else 15)
            elif action == "bmove" and len(parts) == 4 and parts[3] in {"up", "down"}:
                await storage.move_button(item.id, parts[3])
                await show_buttons(query.message, storage, mirror.id, item.section_id)
            elif action == "bdelete" and len(parts) == 3:
                await say(query.message, "Удалить кнопку?", [[key("Удалить", f"own:bconfirm:{item.id}")], [key("Отмена", f"own:button:{item.id}")]], True)
            elif action == "bconfirm" and len(parts) == 3:
                await storage.delete_button(item.id)
                await clear_flow(state)
                await show_buttons(query.message, storage, mirror.id, item.section_id)

    @router.message(OwnerFlow.value)
    async def receive_value(message: Message, state: FSMContext, bot: Bot):
        if not private_owner(message, mirror.owner_id):
            return
        data = await state.get_data()
        if not await valid_flow(data):
            await clear_flow(state)
            return
        field = data.get("field")
        operation = data.get("operation")
        if not message.text or not field:
            await say(message, "Отправьте текст.")
            return
        try:
            value = checked(message.text, "Значение", data["maximum"])
            if field == "emoji" or operation == "description":
                value = "" if value == "-" else value
            if field == "payload":
                if data.get("kind") == "url":
                    value = validate_url(value)
                elif data.get("kind") != "text":
                    return
        except ValueError as error:
            await say(message, str(error))
            return
        if field == "payload":
            await state.update_data(payload=value)
            await save_action(message, state)
            return
        if operation == "new_section":
            if field == "title":
                await state.update_data(title=value)
                await prompt(message, state, "body", "Текст раздела:", 4096)
                return
            section_id = await storage.add_section(mirror.id, data["title"], value)
            if not await storage.root(mirror.id):
                await storage.set_root(mirror.id, section_id)
        elif operation == "new_button":
            await state.update_data(**{field: value})
            if field == "title":
                await prompt(message, state, "emoji", "Эмодзи или '-' если не нужно:", 15)
            else:
                await choose(message, state, "kind")
            return
        elif operation == "section":
            await storage.update_section(data["section_id"], field, value)
        elif operation == "button":
            await storage.update_button(data["button_id"], field, value)
        elif operation == "welcome":
            await storage.set_welcome(mirror.id, value)
        elif operation == "name":
            await bot.set_my_name(name=value)
        elif operation == "description":
            await bot.set_my_description(description=value)
        else:
            return
        await clear_flow(state)
        await say(message, "Сохранено.")

    @router.message(StateFilter(OwnerFlow.kind, OwnerFlow.submenu, OwnerFlow.style))
    async def selection_hint(message: Message):
        if private_owner(message, mirror.owner_id):
            await say(message, "Выберите вариант кнопкой в последнем сообщении или используйте /cancel.")

    return router


async def show_sections(message: Message, storage: Storage, mirror_id: int, offset: int = 0):
    items = await storage.sections(mirror_id)
    rows = [[key(item.title, f"own:section:{item.id}")] for item in items[offset:offset + 20] if item.mirror_id == mirror_id]
    rows += pagination("own:sections", offset, len(items))
    rows += [[key("Создать раздел", "own:new_section")], [key("Назад", "own:home")]]
    await say(message, "Разделы:", rows, True)


async def show_buttons(message: Message, storage: Storage, mirror_id: int, section_id: int, offset: int = 0):
    section = await storage.section(section_id)
    if not section or section.mirror_id != mirror_id:
        return
    items = await storage.buttons(section_id)
    rows = [[key(f"{item.emoji} {item.title}".strip(), f"own:button:{item.id}")] for item in items[offset:offset + 20] if item.section_id == section_id]
    rows += pagination(f"own:buttons:{section_id}", offset, len(items))
    rows += [[key("Создать кнопку", f"own:new_button:{section_id}")], [key("Назад", f"own:section:{section_id}")]]
    await say(message, "Кнопки раздела:", rows, True)


async def show_public(message: Message, storage: Storage, mirror_id: int, section_id: int, back: bool = False, edit: bool = False):
    section = await storage.section(section_id)
    if not section or section.mirror_id != mirror_id:
        return message
    rows = []
    for item in await storage.buttons(section.id):
        if item.section_id != section.id:
            continue
        label = clipped(f"{item.emoji} {item.title}".strip(), 64) or "-"
        style = item.style if item.style in {"primary", "success", "danger"} else None
        if item.action_type == "url":
            try:
                url = validate_url(item.payload)
            except ValueError:
                continue
            rows.append([InlineKeyboardButton(text=label, url=url, style=style)])
        elif item.action_type in {"text", "submenu"}:
            if item.action_type == "submenu":
                target_id = number(item.payload)
                target = await storage.section(target_id) if target_id else None
                if not target or target.mirror_id != mirror_id:
                    continue
            rows.append([InlineKeyboardButton(text=label, callback_data=f"pub:button:{item.id}:{section.id}", style=style)])
        if len(rows) >= 99:
            break
    if back:
        rows.append([key("Назад", f"pub:back:{section.id}")])
    # Overflow is sent separately; navigation stays on the final chunk.
    text = clipped(section.title, 64) + "\n\n" + section.body
    return await say(message, text, rows, edit)


class MirrorLifecycle:
    async def start(self, mirror: Mirror) -> None: ...
    async def stop(self, mirror_id: int) -> None: ...
