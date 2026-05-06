"""
Main brute-force worker (Selectel + Reg.cloud).

Per-iteration logic (both providers):
  1. Clean up stale floating IPs from previous runs.
  2. Allocate a new floating IP.
  3. Check if it belongs to the target whitelist subnets.
  4. If yes → keep it, notify all topics + main chat, store to DB.
  5. If no  → delete it immediately, try again next iteration.

Reg.cloud note: IP allocation takes significantly longer than Selectel
(up to 60–90 seconds per request). This is logged accordingly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from . import db, notify
from .config import config
from .ip_pool import (
    get_matching_subnet,
    ip_in_whitelist,
    regru_get_matching_subnet,
    regru_ip_in_whitelist,
)
from .regru_client import RegRuAccount, RegRuApiError
from .regru_constants import (
    ERROR_RETRY_SEC as REGRU_ERROR_RETRY_SEC,
    RATE_LIMIT_RETRY_SEC as REGRU_RATE_LIMIT_RETRY_SEC,
    REGRU_REGION,
)
from .selectel_client import SelectelAccount, SelectelApiError
from .selectel_constants import ERROR_RETRY_SEC, RATE_LIMIT_RETRY_SEC
from .tg_format import SEP, bold, code, esc, now_str, short_time

logger = logging.getLogger(__name__)

_worker_task: Optional[asyncio.Task] = None
_regru_worker_task: Optional[asyncio.Task] = None

# Throttle error notify.live spam: one message per key per interval
_notify_ts: Dict[str, float] = {}
ERROR_THROTTLE = 60.0   # seconds between error logs per region


def _can_notify(key: str, interval: float) -> bool:
    now = time.monotonic()
    if now - _notify_ts.get(key, 0.0) >= interval:
        _notify_ts[key] = now
        return True
    return False


# ─────────────────────────────────────────── per-region coroutine

async def _try_region(acc: SelectelAccount, region: str) -> None:
    """One iteration for a single account × region pair."""
    if not await db.is_running():
        return

    # Зачищаем зависшие IP. При 400 (квота) — чистим и сразу пробуем снова.
    floatip_id: Optional[str] = None
    try:
        existing = await acc.list_floatingips(region)
        found_ids = {row["floatip_id"] for row in await db.get_found_ips(limit=10000)}
        stale = [f for f in existing if f["id"] not in found_ids]
        if stale:
            await notify.live(
                f"🧹 [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — удаляю {code(str(len(stale)))} зависших IP"
            )
            for fip in stale:
                try:
                    await acc.delete_floatingip(region, fip["id"])
                except Exception:
                    pass
    except Exception:
        pass

    try:
        ip_addr, floatip_id = await acc.create_floatingip(region)

        if ip_in_whitelist(ip_addr):
            subnet = get_matching_subnet(ip_addr) or "?"
            found_msg = (
                f"🎯 {bold('НАЙДЕН IP В WHITELIST!')} [Selectel]\n"
                f"{SEP}\n"
                f"Аккаунт : {code(acc.name)}\n"
                f"Регион  : {code(region)}\n"
                f"IP      : {code(ip_addr)}\n"
                f"Подсеть : {code(subnet)}\n"
                f"Время   : {code(now_str())}"
            )
            await notify.live(
                f"✅ [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — {code(ip_addr)} → {code(subnet)}"
            )
            await notify.logs(found_msg)
            await notify.alert(found_msg)
            await db.add_found_ip(acc.name, region, ip_addr, floatip_id, subnet)
        else:
            await acc.delete_floatingip(region, floatip_id)
            floatip_id = None
            await notify.live(
                f"🔄 [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — {code(ip_addr)} мимо"
            )

    except SelectelApiError as exc:
        if exc.is_rate_limit:
            if _can_notify(f"{acc.name}:{region}:ratelimit", RATE_LIMIT_RETRY_SEC):
                await notify.live(
                    f"⏳ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — rate limit (HTTP {exc.status}), пауза {code(f'{RATE_LIMIT_RETRY_SEC} с')}"
                )
            await asyncio.sleep(RATE_LIMIT_RETRY_SEC)
        elif exc.status == 400:
            # Quota exceeded — stale cleanup runs at next iteration start; no sleep needed
            if _can_notify(f"{acc.name}:{region}:quota", ERROR_THROTTLE):
                await notify.live(
                    f"⚠️ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — квота (HTTP 400), зачищаю и пробую снова"
                )
        elif exc.is_permanent:
            if _can_notify(f"{acc.name}:{region}:perm", ERROR_THROTTLE):
                await notify.live(
                    f"⛔ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — HTTP {exc.status}, пропускаю регион"
                )
        else:
            if _can_notify(f"{acc.name}:{region}:err", ERROR_THROTTLE):
                await notify.live(
                    f"❌ [{code(short_time())}] {bold(acc.name)} "
                    f"[{code(region)}] — HTTP {exc.status}, пропускаю"
                )

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
        err_text = str(exc) or type(exc).__name__
        if _can_notify(f"{acc.name}:{region}:exc", ERROR_THROTTLE):
            await notify.live(
                f"❌ [{code(short_time())}] {bold(acc.name)} "
                f"[{code(region)}] — {esc(err_text[:120])}, пропускаю"
            )


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

    # Pre-authenticate to discover available regions from catalog
    try:
        await acc._get_token()
    except SelectelApiError as exc:
        await notify.live(
            f"⛔ [{code(short_time())}] {bold(acc.name)} — "
            f"ошибка авторизации (HTTP {exc.status}), пропускаю"
        )
        return

    catalog_regions = acc.available_regions()
    if catalog_regions:
        skipped = [r for r in regions if r not in catalog_regions]
        if skipped:
            await notify.live(
                f"ℹ️ [{code(short_time())}] {bold(acc.name)} — "
                f"регионы {code(', '.join(skipped))} отсутствуют в каталоге, пропускаю. "
                f"Доступны: {code(', '.join(catalog_regions))}"
            )
        regions = [r for r in regions if r in catalog_regions]
        if not regions:
            return

    # Все регионы — параллельно
    region_tasks = [
        asyncio.create_task(_try_region(acc, region))
        for region in regions
    ]
    await asyncio.gather(*region_tasks, return_exceptions=True)


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


# ─────────────────────────────────────────── Reg.cloud worker

# Pause between iterations after a miss (delete was fast, don't spam API)
REGRU_ITER_PAUSE = 5  # seconds


async def _regru_cleanup(acc: RegRuAccount) -> None:
    """Delete all floating IPs that aren't in our found list."""
    try:
        existing = await acc.list_floatingips()
        found_ids = {row["floatip_id"] for row in await db.get_regru_found_ips(limit=10000)}
        stale = [f for f in existing if f["id"] not in found_ids]
        if stale:
            await notify.live(
                f"🧹 [{code(short_time())}] [RegRu] {bold(acc.name)} "
                f"— удаляю {code(str(len(stale)))} зависших IP"
            )
            for fip in stale:
                try:
                    await acc.delete_floatingip(fip["id"])
                except Exception:
                    pass
    except Exception:
        pass


_NET_ERROR_RETRY_SEC = 60  # longer backoff on DNS/connection failures


async def _regru_one_iteration(acc: RegRuAccount) -> bool:
    """
    One create→wait→check→delete cycle for a reg.cloud account.

    Returns True if a matching IP was found (loop should pause longer),
    False on miss or error.
    """
    floatip_id: Optional[str] = None
    try:
        await notify.live(
            f"⏳ [{code(short_time())}] [RegRu] {bold(acc.name)} "
            f"[{code(REGRU_REGION)}] — создаю IP…"
        )
        floatip_id, ip_addr = await acc.post_floatingip()
        if not ip_addr:
            await notify.live(
                f"⏳ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                f"[{code(REGRU_REGION)}] — жду назначения адреса…"
            )
            ip_addr = await acc.poll_for_ip(floatip_id)

        if regru_ip_in_whitelist(ip_addr):
            subnet = regru_get_matching_subnet(ip_addr) or "?"
            found_msg = (
                f"🎯 {bold('НАЙДЕН IP В WHITELIST!')} [Reg.cloud]\n"
                f"{SEP}\n"
                f"Аккаунт : {code(acc.name)}\n"
                f"Регион  : {code(REGRU_REGION)} (Москва)\n"
                f"IP      : {code(ip_addr)}\n"
                f"Подсеть : {code(subnet)}\n"
                f"Время   : {code(now_str())}"
            )
            await notify.live(
                f"✅ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                f"[{code(REGRU_REGION)}] — {code(ip_addr)} → {code(subnet)}"
            )
            await notify.logs(found_msg)
            await notify.alert(found_msg)
            await db.add_regru_found_ip(acc.name, REGRU_REGION, ip_addr, floatip_id, subnet)
            return True
        else:
            await acc.delete_floatingip(floatip_id)
            floatip_id = None
            await notify.live(
                f"🔄 [{code(short_time())}] [RegRu] {bold(acc.name)} "
                f"[{code(REGRU_REGION)}] — {code(ip_addr)} мимо"
            )
            return False

    except RegRuApiError as exc:
        if floatip_id:
            try:
                await acc.delete_floatingip(floatip_id)
            except Exception:
                pass
        if exc.is_rate_limit:
            if _can_notify(f"regru:{acc.name}:ratelimit", REGRU_RATE_LIMIT_RETRY_SEC):
                await notify.live(
                    f"⏳ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                    f"— rate limit (HTTP {exc.status}), пауза {code(f'{REGRU_RATE_LIMIT_RETRY_SEC} с')}"
                )
            await asyncio.sleep(REGRU_RATE_LIMIT_RETRY_SEC)
        elif exc.status == 400:
            if _can_notify(f"regru:{acc.name}:quota", ERROR_THROTTLE):
                await notify.live(
                    f"⚠️ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                    f"— квота (HTTP 400), зачищаю зависшие IP"
                )
            await _regru_cleanup(acc)
        elif exc.is_permanent:
            if _can_notify(f"regru:{acc.name}:perm", ERROR_THROTTLE):
                await notify.live(
                    f"⛔ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                    f"— HTTP {exc.status}, пропускаю"
                )
            await asyncio.sleep(REGRU_ERROR_RETRY_SEC)
        else:
            if _can_notify(f"regru:{acc.name}:err", ERROR_THROTTLE):
                await notify.live(
                    f"❌ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                    f"— HTTP {exc.status}: {esc(exc.body[:80])}"
                )
        return False

    except asyncio.CancelledError:
        if floatip_id:
            try:
                await acc.delete_floatingip(floatip_id)
            except Exception:
                pass
        raise

    except Exception as exc:
        if floatip_id:
            try:
                await acc.delete_floatingip(floatip_id)
            except Exception:
                pass
        err_text = str(exc) or type(exc).__name__
        # Network/DNS errors: show more often and wait longer before retry
        is_net_err = isinstance(exc, (aiohttp.ClientError, OSError))
        throttle_key = f"regru:{acc.name}:{'net' if is_net_err else 'exc'}"
        throttle_sec = 30.0 if is_net_err else ERROR_THROTTLE
        if _can_notify(throttle_key, throttle_sec):
            await notify.live(
                f"❌ [{code(short_time())}] [RegRu] {bold(acc.name)} "
                f"— {esc(err_text[:120])}"
            )
        if is_net_err:
            await asyncio.sleep(_NET_ERROR_RETRY_SEC)
        return False


async def _regru_account_loop(name: str, api_key: str) -> None:
    """
    Persistent per-account loop for reg.cloud.

    Runs: cleanup → create → wait for IP → compare → delete if miss → pause → repeat.
    Non-aggressive: natural pacing comes from IP allocation time (30–180s each).
    An extra short pause after each miss prevents hammering the API on fast errors.
    """
    acc = RegRuAccount(name=name, api_key=api_key)
    await _regru_cleanup(acc)

    while True:
        if not await db.is_regru_running():
            await asyncio.sleep(3)
            continue

        hit = await _regru_one_iteration(acc)
        if not hit:
            # Short pause after a miss or error so we don't hammer the API
            await asyncio.sleep(REGRU_ITER_PAUSE)


async def _regru_worker_loop() -> None:
    await notify.logs(
        f"🚀 {bold('Reg.cloud перебор запущен')}\n"
        f"{SEP}\n"
        f"Регион       : {code(REGRU_REGION)} (Москва)\n"
        f"Одновременно : {code(str(config.regru_atmoment_acc))} акк.\n"
        f"Режим        : неагрессивный (IP создаются долго, ждём назначения)"
    )

    # account_name → running Task
    account_tasks: Dict[str, asyncio.Task] = {}

    try:
        while True:
            if not await db.is_regru_running():
                await asyncio.sleep(3)
                continue

            accounts = await db.get_enabled_regru_accounts()
            if not accounts:
                if not account_tasks:
                    await notify.live(
                        f"⚠️ [{code(short_time())}] [RegRu] Нет аккаунтов — "
                        f"добавьте через {code('/regrkadd')}"
                    )
                await asyncio.sleep(30)
                continue

            active = {a["name"]: a for a in accounts[: config.regru_atmoment_acc]}

            # Cancel tasks for accounts no longer in the active set
            for acc_name in list(account_tasks):
                if acc_name not in active or account_tasks[acc_name].done():
                    if not account_tasks[acc_name].done():
                        account_tasks[acc_name].cancel()
                    del account_tasks[acc_name]

            # Spawn tasks for new accounts
            for acc_name, acc_data in active.items():
                if acc_name not in account_tasks or account_tasks[acc_name].done():
                    account_tasks[acc_name] = asyncio.create_task(
                        _regru_account_loop(acc_data["name"], acc_data["api_key"])
                    )

            # Refresh account list every 15 s
            await asyncio.sleep(15)

    except asyncio.CancelledError:
        for task in account_tasks.values():
            task.cancel()
        await asyncio.gather(*account_tasks.values(), return_exceptions=True)
    except Exception as exc:
        logger.exception("RegRu worker loop unhandled error")
        await notify.logs(
            f"❌ {bold('[RegRu] Критическая ошибка воркера')}: {esc(str(exc)[:200])}"
        )
    finally:
        await notify.logs(f"⏹ {bold('[Reg.cloud] Перебор остановлен')}")


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


async def start_regru_worker() -> None:
    global _regru_worker_task
    if _regru_worker_task and not _regru_worker_task.done():
        return
    await db.set_state("regru_running", "1")
    _regru_worker_task = asyncio.create_task(_regru_worker_loop())


async def stop_regru_worker() -> None:
    global _regru_worker_task
    await db.set_state("regru_running", "0")
    if _regru_worker_task and not _regru_worker_task.done():
        _regru_worker_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_regru_worker_task), timeout=6.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _regru_worker_task = None


def is_regru_worker_running() -> bool:
    return _regru_worker_task is not None and not _regru_worker_task.done()
