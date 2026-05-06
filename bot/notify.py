from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import config

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


async def _send(topic_id: int, text: str) -> None:
    if _bot is None:
        return
    try:
        await _bot.send_message(
            chat_id=config.group_id,
            message_thread_id=topic_id if topic_id > 0 else None,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        logger.warning("Telegram send error: %s", exc)
    except Exception as exc:
        logger.error("Unexpected notify error: %s", exc)


async def logs(text: str) -> None:
    """Send to the general logs topic."""
    await _send(config.topic_id_logs, text)


async def live(text: str) -> None:
    """Send to the live-process topic."""
    await _send(config.topic_id_live, text)


async def alert(text: str) -> None:
    """Send to the main group chat (no topic thread) — used for found IPs."""
    await _send(0, text)
