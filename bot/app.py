import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import Settings
from bot.handlers import MirrorLifecycle, controller_router, mirror_router
from bot.storage import Mirror, Storage


class MirrorManager(MirrorLifecycle):
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.tasks: dict[int, asyncio.Task[None]] = {}

    async def start(self, mirror: Mirror) -> None:
        if mirror.id in self.tasks:
            return
        bot = Bot(mirror.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(mirror_router(self.storage, mirror))

        async def poll() -> None:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Mirror @%s stopped because of an error", mirror.username)
            finally:
                await bot.session.close()
                self.tasks.pop(mirror.id, None)

        self.tasks[mirror.id] = asyncio.create_task(poll(), name=f"mirror-{mirror.id}")

    async def stop(self, mirror_id: int) -> None:
        task = self.tasks.pop(mirror_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(mirror_id) for mirror_id in list(self.tasks)), return_exceptions=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    storage = Storage(settings.database_path)
    await storage.initialize()

    manager = MirrorManager(storage)
    for mirror in await storage.list_mirrors(enabled_only=True):
        await manager.start(mirror)

    controller_bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    controller = Dispatcher(storage=MemoryStorage())
    controller.include_router(controller_router(storage, settings.admin_id, manager))
    try:
        await controller_bot.delete_webhook(drop_pending_updates=False)
        await controller.start_polling(controller_bot, allowed_updates=controller.resolve_used_update_types())
    finally:
        await manager.stop_all()
        await controller_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
