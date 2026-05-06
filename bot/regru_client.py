"""
Reg.cloud (reg.ru) API client.

Authentication: API key is used directly as X-Auth-Token.
Region: Moscow only (msk1).

Floating IP lifecycle:
  list   → GET    /v2.0/floatingips
  create → POST   /v2.0/floatingips
  delete → DELETE /v2.0/floatingips/{id}

Note: floating IP allocation in reg.cloud can take significantly longer
than other providers (up to 60 seconds per request).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import aiohttp

from .regru_constants import REGRU_NETWORK_BASE, REGRU_REGION

logger = logging.getLogger(__name__)

_CREATE_TIMEOUT = 90  # reg.cloud IP allocation is slow


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

    Only the API key is required — it is sent as X-Auth-Token.
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

    async def create_floatingip(self) -> Tuple[str, str]:
        """
        Allocate a floating IPv4 in Moscow.
        Note: reg.cloud IP allocation is slow — timeout is set to 90 s.

        Returns:
            (ip_address, floatip_id)
        """
        ext_net = await self._external_network()

        async with _make_session() as session:
            async with session.post(
                f"{REGRU_NETWORK_BASE}/v2.0/floatingips",
                json={"floatingip": {"floating_network_id": ext_net}},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=_CREATE_TIMEOUT),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                fip = body["floatingip"]
                return fip["floating_ip_address"], fip["id"]

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
