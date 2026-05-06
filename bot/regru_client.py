"""
Reg.cloud (reg.ru) API client.

Authentication: API key is used directly as X-Auth-Token.
Region: Moscow only (msk1).

Floating IP lifecycle:
  create → POST   /v2.0/floatingips  (response may have floating_ip_address=null)
  poll   → GET    /v2.0/floatingips/{id}  until floating_ip_address is set
  delete → DELETE /v2.0/floatingips/{id}
  list   → GET    /v2.0/floatingips

Note: reg.cloud allocates IPs asynchronously — the POST may return before
the address is assigned. _poll_for_ip handles waiting for it to appear.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import aiohttp

from .regru_constants import REGRU_NETWORK_BASE, REGRU_REGION

logger = logging.getLogger(__name__)

_POST_TIMEOUT = 30       # POST itself; address assignment happens async
_POLL_INTERVAL = 5       # seconds between poll attempts
_POLL_TIMEOUT = 180      # max seconds to wait for address to appear


class RegRuApiError(Exception):
    def __init__(self, status: int, body: str, is_rate_limit: bool = False) -> None:
        self.status = status
        self.body = body
        self.is_rate_limit = is_rate_limit
        self.is_permanent = status in (401, 403, 404)
        super().__init__(f"HTTP {status}: {body[:300]}")


def _make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession()


async def _raise_for(resp: aiohttp.ClientResponse) -> None:
    if resp.status in (200, 201, 202, 204):
        return
    body = await resp.text()
    raise RegRuApiError(
        resp.status,
        body,
        is_rate_limit=resp.status in (429, 503, 529),
    )


class RegRuAccount:
    """
    Wraps one reg.cloud account.
    Only API key required — sent as X-Auth-Token.
    """

    def __init__(self, name: str, api_key: str) -> None:
        self.name = name
        self.api_key = api_key
        self._ext_net_id: Optional[str] = None

    @property
    def region(self) -> str:
        return REGRU_REGION

    def _headers(self) -> Dict[str, str]:
        return {"X-Auth-Token": self.api_key}

    # ------------------------------------------------------------------ balance

    async def get_balance(self) -> Optional[float]:
        """
        Try to retrieve account balance. Returns None if the endpoint
        is unavailable or the response is unexpected.
        """
        try:
            async with _make_session() as session:
                async with session.get(
                    f"{REGRU_NETWORK_BASE.replace('/network', '')}/billing/v1/balance",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    # Try common field names
                    for key in ("balance", "amount", "money", "value"):
                        if key in data:
                            return float(data[key])
                    return None
        except Exception:
            return None

    # ------------------------------------------------------------------ networks

    async def _external_network(self) -> str:
        if self._ext_net_id:
            return self._ext_net_id

        async with _make_session() as session:
            async with session.get(
                f"{REGRU_NETWORK_BASE}/v2.0/networks",
                params={"router:external": "true"},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                await _raise_for(resp)
                data = await resp.json()
                nets: List[Dict] = data.get("networks", [])
                if not nets:
                    raise RegRuApiError(0, f"No external networks in region {self.region}")
                self._ext_net_id = nets[0]["id"]
                return self._ext_net_id

    # ------------------------------------------------------------------ floating IPs

    async def list_floatingips(self) -> List[Dict]:
        async with _make_session() as session:
            async with session.get(
                f"{REGRU_NETWORK_BASE}/v2.0/floatingips",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                return body.get("floatingips", [])

    async def _poll_for_ip(self, fip_id: str) -> str:
        """
        Poll GET /floatingips/{id} until floating_ip_address is populated.
        reg.cloud allocates the address asynchronously after the POST returns.
        Raises RegRuApiError on timeout or if the resource disappears.
        """
        deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise RegRuApiError(
                    0,
                    f"Timeout: floating IP {fip_id} never got an address in {_POLL_TIMEOUT}s",
                )
            await asyncio.sleep(_POLL_INTERVAL)

            async with _make_session() as session:
                async with session.get(
                    f"{REGRU_NETWORK_BASE}/v2.0/floatingips/{fip_id}",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 404:
                        raise RegRuApiError(404, f"Floating IP {fip_id} disappeared during polling")
                    await _raise_for(resp)
                    body = await resp.json()
                    ip = body.get("floatingip", {}).get("floating_ip_address")
                    if ip:
                        elapsed = _POLL_TIMEOUT - remaining
                        logger.debug("RegRu: fip %s got IP %s after ~%.0fs", fip_id, ip, elapsed)
                        return ip

    async def post_floatingip(self) -> Tuple[str, Optional[str]]:
        """
        POST the create request only.

        Returns:
            (floatip_id, ip_address_or_None)
        """
        ext_net = await self._external_network()

        async with _make_session() as session:
            async with session.post(
                f"{REGRU_NETWORK_BASE}/v2.0/floatingips",
                json={"floatingip": {"floating_network_id": ext_net}},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=_POST_TIMEOUT),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                fip = body["floatingip"]
                return fip["id"], fip.get("floating_ip_address")

    async def poll_for_ip(self, fip_id: str) -> str:
        return await self._poll_for_ip(fip_id)

    async def create_floatingip(self) -> Tuple[str, str]:
        """POST + poll until address. Returns (ip_address, floatip_id)."""
        fip_id, ip = await self.post_floatingip()
        if not ip:
            ip = await self._poll_for_ip(fip_id)
        return ip, fip_id

    async def delete_floatingip(self, floatip_id: str) -> None:
        async with _make_session() as session:
            async with session.delete(
                f"{REGRU_NETWORK_BASE}/v2.0/floatingips/{floatip_id}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return
                await _raise_for(resp)
