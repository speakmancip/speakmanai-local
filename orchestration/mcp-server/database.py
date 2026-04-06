import os
import logging

log = logging.getLogger(__name__)

USE_SQLITE = os.environ.get("USE_SQLITE", "false").lower() in ("true", "1", "yes")

if USE_SQLITE:
    # ── SQLite mode ──────────────────────────────────────────────────────────
    from database_sqlite import get_sqlite_db, close_sqlite

    def get_db(name: str = None):
        return get_sqlite_db()

    def get_user_db():
        return get_sqlite_db()

    async def close():
        await close_sqlite()

else:
    # ── MongoDB mode ─────────────────────────────────────────────────────────
    from motor.motor_asyncio import AsyncIOMotorClient

    _client: AsyncIOMotorClient = None

    def get_client() -> AsyncIOMotorClient:
        global _client
        if _client is None:
            uri = os.environ["MONGO_ATLAS_URI"]
            _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        return _client

    def get_db(name: str = None):
        db_name = name or os.environ.get("OPERATIONAL_DB_NAME", "speakmanai_db")
        return get_client()[db_name]

    def get_user_db():
        db_name = os.environ.get("USER_DB_NAME", "app_db")
        return get_client()[db_name]

    async def close():
        global _client
        if _client:
            _client.close()
            _client = None
