"""
Telegram command handlers.

All commands are restricted to the configured GROUP_ID.
Commands:
    /selecteladd  — add / update a Selectel account
    /regrkadd     — add / update a Reg.cloud account (API key only)
    /start        — start Selectel brute-force worker
    /stop         — stop Selectel brute-force worker
    /regrstart    — start Reg.cloud brute-force worker
    /regrstop     — stop Reg.cloud brute-force worker
    /status       — current status overview (both providers)
    /accounts     — list all stored Selectel accounts
    /regraccs     — list all stored Reg.cloud accounts
    /whitelist    — show Selectel whitelist subnets
    /regrwhitelist — show Reg.cloud target subnets
    /found        — found Selectel IPs
    /regrfound    — found Reg.cloud IPs
    /help         — command reference
"""
from __future__ import annotations

import re
from typing import List, Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from . import brute_worker, db, notify
from .config import config
from .ip_pool import WHITELIST_CIDRS
from .regru_constants import REGRU_REGION, REGRU_WHITELIST_CIDRS
from .tg_format import SEP, bold, code, esc, italic, now_str, short_time

router = Router()


# ─────────────────────────────────────────── helpers

def _allowed(msg: Message) -> bool:
    """Return True if the message comes from the configured group."""
    if config.group_id == 0:
        return True
    return msg.chat.id == config.group_id


def _split(raw: str) -> List[str]:
    """Split credential string by pipe or colon, strip whitespace."""
    return [p.strip() for p in re.split(r"\s*[|]\s*", raw)]


# ─────────────────────────────────────────── /selecteladd

_HELP_ADD = (
    f"{bold('Форматы добавления аккаунта:')}\n\n"
    f"<b>Полный</b> (рекомендуется):\n"
    f"{code('/selecteladd name | sa_login | sa_pass | project_id | acc_login | api_key')}\n\n"
    f"<b>Только billing-ключ</b> (перебор недоступен):\n"
    f"{code('/selecteladd name | api_key')}\n\n"
    f"{bold('Пример полного:')}\n"
    f"{code('/selecteladd main | user@example.com | P@ssw0rd | 50276c8b32584450b8bed77d24c223b4 | 573082 | XCTAfU6nEkDYaxIjx2tB7OCKEn_573082')}"
)


@router.message(Command("selecteladd"))
async def cmd_selecteladd(msg: Message) -> None:
    if not _allowed(msg):
        return

    raw = re.sub(r"^/selecteladd\s*", "", msg.text or "", flags=re.IGNORECASE).strip()
    if not raw:
        await msg.reply(_HELP_ADD, parse_mode="HTML")
        return

    parts = _split(raw)

    if len(parts) == 2:
        name, api_key = parts
        sa_login = sa_pass = project_id = acc_login = ""
        mode = "billing"
    elif len(parts) == 6:
        name, sa_login, sa_pass, project_id, acc_login, api_key = parts
        mode = "full"
    else:
        await msg.reply(
            f"❌ Неверный формат — ожидается <b>2</b> или <b>6</b> полей через <code>|</code>\n\n"
            f"{_HELP_ADD}",
            parse_mode="HTML",
        )
        return

    if not name:
        await msg.reply("❌ Название аккаунта не может быть пустым", parse_mode="HTML")
        return

    ok = await db.upsert_account(name, sa_login, sa_pass, project_id, acc_login, api_key)
    if not ok:
        await msg.reply("❌ Ошибка записи в базу данных", parse_mode="HTML")
        return

    warn = (
        f"\n⚠️ {italic('Режим billing-only: для перебора добавьте полные учётные данные')}"
        if mode == "billing"
        else ""
    )

    await msg.reply(
        f"✅ {bold('Аккаунт сохранён')}\n"
        f"{SEP}\n"
        f"Имя    : {code(name)}\n"
        f"Режим  : {code('полный' if mode == 'full' else 'billing only')}"
        f"{warn}",
        parse_mode="HTML",
    )
    await notify.logs(
        f"➕ {bold('Добавлен аккаунт')}: {code(name)} "
        f"[{code('полный' if mode == 'full' else 'billing')}]"
    )


# ─────────────────────────────────────────── /regrkadd

_HELP_REGRU_ADD = (
    f"{bold('Добавление Reg.cloud аккаунта:')}\n\n"
    f"{code('/regrkadd name | api_key')}\n\n"
    f"{bold('Пример:')}\n"
    f"{code('/regrkadd main | abc123def456')}"
)


@router.message(Command("regrkadd"))
async def cmd_regrkadd(msg: Message) -> None:
    if not _allowed(msg):
        return

    raw = re.sub(r"^/regrkadd\s*", "", msg.text or "", flags=re.IGNORECASE).strip()
    if not raw:
        await msg.reply(_HELP_REGRU_ADD, parse_mode="HTML")
        return

    parts = _split(raw)
    if len(parts) != 2:
        await msg.reply(
            f"❌ Неверный формат — ожидается <b>2</b> поля через <code>|</code>\n\n"
            f"{_HELP_REGRU_ADD}",
            parse_mode="HTML",
        )
        return

    name, api_key = parts
    if not name:
        await msg.reply("❌ Название аккаунта не может быть пустым", parse_mode="HTML")
        return
    if not api_key:
        await msg.reply("❌ API ключ не может быть пустым", parse_mode="HTML")
        return

    ok = await db.upsert_regru_account(name, api_key)
    if not ok:
        await msg.reply("❌ Ошибка записи в базу данных", parse_mode="HTML")
        return

    await msg.reply(
        f"✅ {bold('Reg.cloud аккаунт сохранён')}\n"
        f"{SEP}\n"
        f"Имя    : {code(name)}\n"
        f"Регион : {code(REGRU_REGION)} (Москва)",
        parse_mode="HTML",
    )
    await notify.logs(
        f"➕ {bold('[RegRu] Добавлен аккаунт')}: {code(name)}"
    )


# ─────────────────────────────────────────── /start

@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not _allowed(msg):
        return

    if brute_worker.is_worker_running():
        await msg.reply(
            f"▶️ Перебор уже {bold('запущен')}",
            parse_mode="HTML",
        )
        return

    accounts = await db.get_enabled_accounts()
    if not accounts:
        await msg.reply(
            f"❌ Нет аккаунтов с полными данными\n"
            f"Добавьте через {code('/selecteladd')}",
            parse_mode="HTML",
        )
        return

    await brute_worker.start_worker()
    await msg.reply(
        f"▶️ {bold('Перебор запущен')}\n"
        f"{SEP}\n"
        f"Регионы      : {code(', '.join(config.selectel_vm_region))}\n"
        f"Одновременно : {code(str(config.selectel_atmoment_acc))} акк.\n"
        f"Аккаунтов    : {code(str(len(accounts)))}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /stop

@router.message(Command("stop"))
async def cmd_stop(msg: Message) -> None:
    if not _allowed(msg):
        return

    if not brute_worker.is_worker_running():
        await msg.reply(
            f"⏹ Перебор уже {bold('остановлен')}",
            parse_mode="HTML",
        )
        return

    await brute_worker.stop_worker()
    await msg.reply(f"⏹ {bold('Перебор остановлен')}", parse_mode="HTML")


# ─────────────────────────────────────────── /regrstart

@router.message(Command("regrstart"))
async def cmd_regrstart(msg: Message) -> None:
    if not _allowed(msg):
        return

    if brute_worker.is_regru_worker_running():
        await msg.reply(
            f"▶️ [Reg.cloud] Перебор уже {bold('запущен')}",
            parse_mode="HTML",
        )
        return

    accounts = await db.get_enabled_regru_accounts()
    if not accounts:
        await msg.reply(
            f"❌ Нет Reg.cloud аккаунтов\nДобавьте через {code('/regrkadd')}",
            parse_mode="HTML",
        )
        return

    await brute_worker.start_regru_worker()
    await msg.reply(
        f"▶️ {bold('[Reg.cloud] Перебор запущен')}\n"
        f"{SEP}\n"
        f"Регион       : {code(REGRU_REGION)} (Москва)\n"
        f"Одновременно : {code(str(config.regru_atmoment_acc))} акк.\n"
        f"Аккаунтов    : {code(str(len(accounts)))}\n"
        f"⚠️ IP создаются медленно — это нормально",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /regrstop

@router.message(Command("regrstop"))
async def cmd_regrstop(msg: Message) -> None:
    if not _allowed(msg):
        return

    if not brute_worker.is_regru_worker_running():
        await msg.reply(
            f"⏹ [Reg.cloud] Перебор уже {bold('остановлен')}",
            parse_mode="HTML",
        )
        return

    await brute_worker.stop_regru_worker()
    await msg.reply(f"⏹ {bold('[Reg.cloud] Перебор остановлен')}", parse_mode="HTML")


# ─────────────────────────────────────────── /status

@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    if not _allowed(msg):
        return

    sel_running = brute_worker.is_worker_running()
    rgr_running = brute_worker.is_regru_worker_running()
    all_accs = await db.get_all_accounts()
    full_accs = [a for a in all_accs if a["sa_login"]]
    found_sel = await db.count_found_ips()
    all_rgr_accs = await db.get_all_regru_accounts()
    found_rgr = await db.count_regru_found_ips()

    sel_status = f"▶️ {bold('запущен')}" if sel_running else f"⏹ {bold('остановлен')}"
    rgr_status = f"▶️ {bold('запущен')}" if rgr_running else f"⏹ {bold('остановлен')}"

    await msg.reply(
        f"🤖 {bold('Selectalized Bot')}\n"
        f"{SEP}\n"
        f"🔷 {bold('Selectel')}\n"
        f"  Статус       : {sel_status}\n"
        f"  Регионы      : {code(', '.join(config.selectel_vm_region))}\n"
        f"  Одновременно : {code(str(config.selectel_atmoment_acc))} акк.\n"
        f"  Мин. баланс  : {code(f'{config.selectel_minimum_rubles:.2f} ₽')}\n"
        f"  Аккаунтов    : {code(str(len(all_accs)))} (полных: {code(str(len(full_accs)))})\n"
        f"  IP найдено   : {code(str(found_sel))}\n"
        f"{SEP}\n"
        f"🔶 {bold('Reg.cloud')}\n"
        f"  Статус       : {rgr_status}\n"
        f"  Регион       : {code(REGRU_REGION)} (Москва)\n"
        f"  Одновременно : {code(str(config.regru_atmoment_acc))} акк.\n"
        f"  Аккаунтов    : {code(str(len(all_rgr_accs)))}\n"
        f"  IP найдено   : {code(str(found_rgr))}\n"
        f"{SEP}\n"
        f"Время          : {code(now_str())}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /accounts

@router.message(Command("accounts"))
async def cmd_accounts(msg: Message) -> None:
    if not _allowed(msg):
        return

    all_accs = await db.get_all_accounts()
    if not all_accs:
        await msg.reply(
            f"📭 Аккаунтов нет\nДобавьте через {code('/selecteladd')}",
            parse_mode="HTML",
        )
        return

    lines = []
    for acc in all_accs[:30]:
        enabled_icon = "✅" if acc["enabled"] else "🔴"
        creds_icon = "🔑" if acc["sa_login"] else "🔒"
        lines.append(f"{enabled_icon}{creds_icon} {code(acc['name'])}")

    suffix = f"\n…и ещё {len(all_accs) - 30}" if len(all_accs) > 30 else ""

    await msg.reply(
        f"📋 {bold(f'Аккаунты ({len(all_accs)})')}\n"
        f"{SEP}\n"
        + "\n".join(lines)
        + suffix
        + f"\n{SEP}\n"
        f"✅🔑 — активен, полные данные\n"
        f"✅🔒 — активен, только billing\n"
        f"🔴   — отключён",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /whitelist

@router.message(Command("whitelist"))
async def cmd_whitelist(msg: Message) -> None:
    if not _allowed(msg):
        return

    lines = "\n".join(f"  • {code(cidr)}" for cidr in WHITELIST_CIDRS)
    await msg.reply(
        f"📋 {bold(f'Whitelist подсети ({len(WHITELIST_CIDRS)})')}\n"
        f"{SEP}\n"
        f"{lines}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /regraccs

@router.message(Command("regraccs"))
async def cmd_regraccs(msg: Message) -> None:
    if not _allowed(msg):
        return

    all_accs = await db.get_all_regru_accounts()
    if not all_accs:
        await msg.reply(
            f"📭 Reg.cloud аккаунтов нет\nДобавьте через {code('/regrkadd')}",
            parse_mode="HTML",
        )
        return

    lines = []
    for acc in all_accs[:30]:
        enabled_icon = "✅" if acc["enabled"] else "🔴"
        lines.append(f"{enabled_icon} {code(acc['name'])}")

    suffix = f"\n…и ещё {len(all_accs) - 30}" if len(all_accs) > 30 else ""

    await msg.reply(
        f"📋 {bold(f'Reg.cloud аккаунты ({len(all_accs)})')}\n"
        f"{SEP}\n"
        + "\n".join(lines)
        + suffix,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /regrwhitelist

@router.message(Command("regrwhitelist"))
async def cmd_regrwhitelist(msg: Message) -> None:
    if not _allowed(msg):
        return

    lines = "\n".join(f"  • {code(cidr)}" for cidr in REGRU_WHITELIST_CIDRS)
    await msg.reply(
        f"📋 {bold(f'Reg.cloud целевые подсети ({len(REGRU_WHITELIST_CIDRS)})')}\n"
        f"{SEP}\n"
        f"{lines}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /found

@router.message(Command("found"))
async def cmd_found(msg: Message) -> None:
    if not _allowed(msg):
        return

    ips = await db.get_found_ips(limit=20)
    if not ips:
        await msg.reply("📭 Найденных IP пока нет", parse_mode="HTML")
        return

    lines = []
    for row in ips:
        lines.append(
            f"• {code(row['ip'])} [{code(row['region'])}] "
            f"— {code(row['account_name'])} | {row['created_at']}"
        )

    await msg.reply(
        f"🎯 {bold(f'Найденные IP (последние {len(ips)})')}\n"
        f"{SEP}\n"
        + "\n".join(lines),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /regrfound

@router.message(Command("regrfound"))
async def cmd_regrfound(msg: Message) -> None:
    if not _allowed(msg):
        return

    ips = await db.get_regru_found_ips(limit=20)
    if not ips:
        await msg.reply("📭 Найденных Reg.cloud IP пока нет", parse_mode="HTML")
        return

    lines = []
    for row in ips:
        lines.append(
            f"• {code(row['ip'])} [{code(row['region'])}] "
            f"— {code(row['account_name'])} | {row['created_at']}"
        )

    await msg.reply(
        f"🎯 {bold(f'Reg.cloud найденные IP (последние {len(ips)})')}\n"
        f"{SEP}\n"
        + "\n".join(lines),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────── /help

@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    if not _allowed(msg):
        return

    await msg.reply(
        f"🤖 {bold('Selectalized Bot — справка')}\n"
        f"{SEP}\n"
        f"🔷 {bold('Selectel')}\n"
        f"{code('/selecteladd')}   — добавить Selectel аккаунт\n"
        f"{code('/start')}         — запустить перебор Selectel\n"
        f"{code('/stop')}          — остановить перебор Selectel\n"
        f"{code('/accounts')}      — список Selectel аккаунтов\n"
        f"{code('/whitelist')}     — целевые подсети Selectel\n"
        f"{code('/found')}         — найденные IP Selectel\n"
        f"{SEP}\n"
        f"🔶 {bold('Reg.cloud')}\n"
        f"{code('/regrkadd')}      — добавить Reg.cloud аккаунт\n"
        f"{code('/regrstart')}     — запустить перебор Reg.cloud\n"
        f"{code('/regrstop')}      — остановить перебор Reg.cloud\n"
        f"{code('/regraccs')}      — список Reg.cloud аккаунтов\n"
        f"{code('/regrwhitelist')} — целевые подсети Reg.cloud\n"
        f"{code('/regrfound')}     — найденные IP Reg.cloud\n"
        f"{SEP}\n"
        f"📊 {bold('Общее')}\n"
        f"{code('/status')}        — статус обоих провайдеров\n"
        f"{code('/help')}          — эта справка",
        parse_mode="HTML",
    )
