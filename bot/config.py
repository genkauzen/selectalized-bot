import os
from dataclasses import dataclass, field
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "").strip().lower()
    return v in ("true", "1", "yes") if v else default


def _int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, ""))
    except (ValueError, TypeError):
        return default


def _float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, ""))
    except (ValueError, TypeError):
        return default


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, "").strip() or default


def _regions(key: str) -> List[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return ["ru-2", "ru-3"]
    parts = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts if parts else ["ru-2", "ru-3"]


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str = field(default_factory=lambda: _str("BOT_TOKEN"))
    tg_proxy_use: bool = field(default_factory=lambda: _bool("TG_PROXY_USE"))
    tg_proxy_url: Optional[str] = field(
        default_factory=lambda: os.getenv("TG_PROXY_URL") or None
    )
    group_id: int = field(default_factory=lambda: _int("GROUP_ID"))
    topic_id_logs: int = field(default_factory=lambda: _int("TOPIC_ID_LOGS"))
    topic_id_live: int = field(default_factory=lambda: _int("TOPIC_ID_LIVE"))

    # Selectel
    selectel_proxy_use: bool = field(default_factory=lambda: _bool("SELECTEL_PROXY_USE"))
    selectel_proxy_url: Optional[str] = field(
        default_factory=lambda: os.getenv("SELECTEL_PROXY_URL") or None
    )
    selectel_vm_name: str = field(
        default_factory=lambda: _str("SELECTEL_VM_NAME", "selectalized-vm")
    )
    selectel_vm_region: List[str] = field(
        default_factory=lambda: _regions("SELECTEL_VM_REGION")
    )
    selectel_atmoment_acc: int = field(
        default_factory=lambda: max(1, _int("SELECTEL_ATMOMENT_ACC", 2))
    )
    selectel_minimum_rubles: float = field(
        default_factory=lambda: _float("SELECTEL_MINIMUM_RUBLES", 0.0)
    )

    # Paths
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/app/data"))


config = Config()
