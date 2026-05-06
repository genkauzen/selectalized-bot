import aiosqlite
from pathlib import Path
from typing import Dict, List, Optional
from .config import config

_DB_PATH: Optional[Path] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    UNIQUE NOT NULL,
    sa_login   TEXT    NOT NULL DEFAULT '',
    sa_pass    TEXT    NOT NULL DEFAULT '',
    project_id TEXT    NOT NULL DEFAULT '',
    acc_login  TEXT    NOT NULL DEFAULT '',
    api_key    TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS found_ips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    region       TEXT NOT NULL,
    ip           TEXT NOT NULL,
    floatip_id   TEXT NOT NULL DEFAULT '',
    subnet       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS regru_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    UNIQUE NOT NULL,
    api_key    TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS regru_found_ips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    region       TEXT NOT NULL DEFAULT 'msk1',
    ip           TEXT NOT NULL,
    floatip_id   TEXT NOT NULL DEFAULT '',
    subnet       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO bot_state (key, value) VALUES ('running', '0');
INSERT OR IGNORE INTO bot_state (key, value) VALUES ('regru_running', '0');
"""


def _db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = Path(config.data_dir) / "selectalized.db"
    return _DB_PATH


async def init_db() -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def upsert_account(
    name: str,
    sa_login: str,
    sa_pass: str,
    project_id: str,
    acc_login: str,
    api_key: str,
) -> bool:
    try:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO accounts (name, sa_login, sa_pass, project_id, acc_login, api_key)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    sa_login   = excluded.sa_login,
                    sa_pass    = excluded.sa_pass,
                    project_id = excluded.project_id,
                    acc_login  = excluded.acc_login,
                    api_key    = excluded.api_key
                """,
                (name, sa_login, sa_pass, project_id, acc_login, api_key),
            )
            await db.commit()
        return True
    except Exception:
        return False


async def get_enabled_accounts() -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM accounts WHERE enabled = 1 AND sa_login != '' ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_accounts() -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM accounts ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_account_enabled(name: str, enabled: bool) -> bool:
    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute(
            "UPDATE accounts SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_account(name: str) -> bool:
    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute("DELETE FROM accounts WHERE name = ?", (name,))
        await db.commit()
        return cur.rowcount > 0


async def get_state(key: str, default: str = "0") -> str:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_state(key: str, value: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def is_running() -> bool:
    return await get_state("running") == "1"


async def add_found_ip(
    account_name: str,
    region: str,
    ip: str,
    floatip_id: str,
    subnet: str,
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO found_ips (account_name, region, ip, floatip_id, subnet)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_name, region, ip, floatip_id, subnet),
        )
        await db.commit()


async def get_found_ips(limit: int = 20) -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM found_ips ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_found_ips() -> int:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT COUNT(*) FROM found_ips") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ──────────────────────────────────────── Reg.cloud accounts

async def upsert_regru_account(name: str, api_key: str) -> bool:
    try:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO regru_accounts (name, api_key)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET api_key = excluded.api_key
                """,
                (name, api_key),
            )
            await db.commit()
        return True
    except Exception:
        return False


async def get_enabled_regru_accounts() -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM regru_accounts WHERE enabled = 1 AND api_key != '' ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_regru_accounts() -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM regru_accounts ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def is_regru_running() -> bool:
    return await get_state("regru_running") == "1"


# ──────────────────────────────────────── Reg.cloud found IPs

async def add_regru_found_ip(
    account_name: str,
    region: str,
    ip: str,
    floatip_id: str,
    subnet: str,
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO regru_found_ips (account_name, region, ip, floatip_id, subnet)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_name, region, ip, floatip_id, subnet),
        )
        await db.commit()


async def get_regru_found_ips(limit: int = 20) -> List[Dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM regru_found_ips ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_regru_found_ips() -> int:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT COUNT(*) FROM regru_found_ips") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
