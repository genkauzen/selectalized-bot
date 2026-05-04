import html
from datetime import datetime


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def pre(text: str) -> str:
    return f"<pre>{esc(text)}</pre>"


SEP = "┈" * 22


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def short_time() -> str:
    return datetime.now().strftime("%H:%M:%S")
