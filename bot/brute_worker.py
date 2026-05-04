"""
Main brute-force worker.

Per-iteration logic:
  1. Load all enabled accounts that have full credentials.
  2. Filter out accounts whose balance is below the configured minimum.
  3. Pick up to SELECTEL_ATMOMENT_ACC accounts and run them in parallel.
  4. For each account × each configured region:
       - Allocate a floating IP.
       - Check IP against the whitelist subnets.
       - If match  → success: log both topics, store to DB.
       - If no match → delete the IP immediately, continue.
  5. On any API error → wait ERROR_RETRY_SEC seconds, then retry the whole loop.
  6. On 429 / 503 → wait RATE_LIMIT_RETRY_SEC seconds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from . import db, notify
from .config import config
from .ip_pool import get_matching_subnet, ip_in_whitelist
from .selectel_client import SelectelAccount, SelectelApiError
from .selectel_constants import ERROR_RETRY_SEC, RATE_LIMIT_RETRY_SEC
from .tg_format import SEP, bold, code, esc, now_str, short_time

logger = logging.getLogger(__name__)

_worker_task: Optional[asyncio.Task] = None


# ─────────────────────────────────────────── per-account coroutine

async def _try_account(acc_data: Dict, regions: List[str]) -> None:
    acc = SelectelAccount(
        name=acc_data["name"],
        sa_login=acc_data["sa_login"],
        sa_pass=acc_data["sa_pass"],
        project_id=acc_data["project_id"],
        acc_login=acc_data["acc_login"],
        api_key=acc_data["api_key"],
    )

    if not acc.has_full_creds:
        await notify.logs(f"⚠️ {bold(acc.name)}: неполные учётные данные, пропускаю")
        return

    # Balance guard
    if config.selectel_minimum_rubles > 0:
        balance = await acc.get_balance()
        if balance < config.selectel_minimum_rubles:
            await notify.live(
                f"💰 {bold(acc.name)} — баланс {code(f'{balance:.2f} ₽')} "
                f"< {code(f'{config.selectel_minimum_rubles:.2f} ₽')}, пропускаю"
            )
            return

    for region in regions:
        if not await db.is_running():
            return

        ip_addr: Optional[str] = None
        floatip_id: Optional[str] = None

        try:
            await notify.live(
                f"🔄 [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — выделяю floating IP…"
            )

            ip_addr, floatip_id = await acc.create_floatingip(region)

            if ip_in_whitelist(ip_addr):
                subnet = get_matching_subnet(ip_addr) or "?"
                await notify.live(
                    f"✅ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}]\n"
                    f"IP: {code(ip_addr)} → {code(subnet)}"
                )
                await notify.logs(
                    f"🎯 {bold('НАЙДЕН IP В WHITELIST!')}\n"
                    f"{SEP}\n"
                    f"Аккаунт : {code(acc.name)}\n"
                    f"Регион  : {code(region)}\n"
                    f"IP      : {code(ip_addr)}\n"
                    f"Подсеть : {code(subnet)}\n"
                    f"Время   : {code(now_str())}"
                )
                await db.add_found_ip(acc.name, region, ip_addr, floatip_id, subnet)
                # Keep the IP — do not delete it
                return
            else:
                await acc.delete_floatingip(region, floatip_id)
                await notify.live(
                    f"🗑 [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] {code(ip_addr)} — не в whitelist, удалён"
                )

        except SelectelApiError as exc:
            # Try to clean up leaked IP
            if floatip_id:
                try:
                    await acc.delete_floatingip(region, floatip_id)
                except Exception:
                    pass

            if exc.is_rate_limit:
                wait = RATE_LIMIT_RETRY_SEC
                await notify.live(
                    f"⏳ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — rate limit, пауза "
                    f"{code(f'{wait // 60} мин')}"
                )
            else:
                wait = ERROR_RETRY_SEC
                await notify.live(
                    f"❌ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — HTTP {exc.status}, "
                    f"жду {code(f'{wait} с')}"
                )
            await asyncio.sleep(wait)

        except asyncio.CancelledError:
            if floatip_id:
                try:
                    await acc.delete_floatingip(region, floatip_id)
                except Exception:
                    pass
            raise

        except Exception as exc:
            if floatip_id:
                try:
                    await acc.delete_floatingip(region, floatip_id)
                except Exception:
                    pass
            await notify.live(
                f"❌ [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — {esc(str(exc)[:120])}, "
                f"жду {code(f'{ERROR_RETRY_SEC} с')}"
            )
            await asyncio.sleep(ERROR_RETRY_SEC)


# ─────────────────────────────────────────── main loop

async def _worker_loop() -> None:
    await notify.logs(
        f"🚀 {bold('Перебор запущен')}\n"
        f"{SEP}\n"
        f"Регионы      : {code(', '.join(config.selectel_vm_region))}\n"
        f"Одновременно : {code(str(config.selectel_atmoment_acc))} акк.\n"
        f"Мин. баланс  : {code(f'{config.selectel_minimum_rubles:.2f} ₽')}"
    )

    while True:
        try:
            if not await db.is_running():
                await asyncio.sleep(2)
                continue

            accounts = await db.get_enabled_accounts()
            if not accounts:
                await notify.live(
                    f"⚠️ [{code(short_time())}] Нет аккаунтов с полными данными — "
                    f"добавьте через {code('/selecteladd')}"
                )
                await asyncio.sleep(30)
                continue

            batch = accounts[: config.selectel_atmoment_acc]
            tasks = [
                asyncio.create_task(_try_account(acc, config.selectel_vm_region))
                for acc in batch
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Worker loop unhandled error")
            await notify.logs(
                f"❌ {bold('Критическая ошибка воркера')}: {esc(str(exc)[:200])}\n"
                f"Перезапуск через {ERROR_RETRY_SEC} с…"
            )
            await asyncio.sleep(ERROR_RETRY_SEC)

    await notify.logs(f"⏹ {bold('Перебор остановлен')}")


# ─────────────────────────────────────────── public API

async def start_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    await db.set_state("running", "1")
    _worker_task = asyncio.create_task(_worker_loop())


async def stop_worker() -> None:
    global _worker_task
    await db.set_state("running", "0")
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_worker_task), timeout=6.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _worker_task = None


def is_worker_running() -> bool:
    return _worker_task is not None and not _worker_task.done()
