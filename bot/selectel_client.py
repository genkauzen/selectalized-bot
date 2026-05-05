"""
Selectel API client.

Authentication flow:
  1. POST /identity/v3/auth/tokens  → IAM token (X-Subject-Token header)
  2. Use IAM token as X-Auth-Token for Neutron (floating IP) calls.
  3. Use static api_key as X-Token for billing balance check.

Floating IP lifecycle:
  create  → POST  /v2.0/floatingips
  delete  → DELETE /v2.0/floatingips/{id}
  list    → GET   /v2.0/floatingips
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

from .config import config
from .selectel_constants import (
    BILLING_BASE,
    IAM_BASE,
    TOKEN_MAX_AGE,
    neutron_url,
)

logger = logging.getLogger(__name__)


class SelectelApiError(Exception):
    def __init__(self, status: int, body: str, is_rate_limit: bool = False) -> None:
        self.status = status
        self.body = body
        self.is_rate_limit = is_rate_limit
        self.is_permanent = status in (401, 403, 404)
        super().__init__(f"HTTP {status}: {body[:300]}")


def _make_session() -> aiohttp.ClientSession:
    connector: Optional[aiohttp.TCPConnector] = None
    if config.selectel_proxy_use and config.selectel_proxy_url:
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore

            connector = ProxyConnector.from_url(config.selectel_proxy_url)
        except ImportError:
            logger.warning("aiohttp_socks not installed — proxy ignored")
    if connector:
        return aiohttp.ClientSession(connector=connector)
    return aiohttp.ClientSession()


async def _raise_for(resp: aiohttp.ClientResponse) -> None:
    if resp.status in (200, 201, 202, 204):
        return
    body = await resp.text()
    raise SelectelApiError(
        resp.status,
        body,
        is_rate_limit=resp.status in (429, 503),
    )


class SelectelAccount:
    """
    Wraps one Selectel cloud account.

    Credentials:
        sa_login   — service-account username (IAM user)
        sa_pass    — service-account password
        project_id — OpenStack project UUID
        acc_login  — Selectel account ID (used as Keystone domain name)
        api_key    — static token for billing API (X-Token)
    """

    def __init__(
        self,
        name: str,
        sa_login: str,
        sa_pass: str,
        project_id: str,
        acc_login: str,
        api_key: str,
    ) -> None:
        self.name = name
        self.sa_login = sa_login
        self.sa_pass = sa_pass
        self.project_id = project_id
        self.acc_login = acc_login
        self.api_key = api_key

        self._token: Optional[str] = None
        self._token_ts: float = 0.0
        self._ext_nets: Dict[str, str] = {}   # region → external-network UUID
        self._neutron_endpoints: Dict[str, str] = {}  # region → base URL from catalog

    @property
    def has_full_creds(self) -> bool:
        return bool(self.sa_login and self.sa_pass and self.project_id and self.acc_login)

    # ------------------------------------------------------------------ auth

    async def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and (now - self._token_ts) < TOKEN_MAX_AGE:
            return self._token

        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.sa_login,
                            "domain": {"name": self.acc_login},
                            "password": self.sa_pass,
                        }
                    },
                },
                "scope": {
                    "project": {"id": self.project_id}
                },
            }
        }

        async with _make_session() as session:
            async with session.post(
                f"{IAM_BASE}/auth/tokens",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                await _raise_for(resp)
                token = resp.headers.get("X-Subject-Token", "")
                if not token:
                    raise SelectelApiError(0, "X-Subject-Token missing in auth response")
                self._token = token
                self._token_ts = now

                # Parse service catalog to discover Neutron endpoints per region
                try:
                    body = await resp.json()
                    catalog = body.get("token", {}).get("catalog", [])
                    for svc in catalog:
                        if svc.get("type") != "network":
                            continue
                        for ep in svc.get("endpoints", []):
                            if ep.get("interface") == "public":
                                region = ep.get("region_id") or ep.get("region", "")
                                url = ep.get("url", "").rstrip("/")
                                if region and url:
                                    self._neutron_endpoints[region] = url
                    if self._neutron_endpoints:
                        logger.info(
                            "Account %s: discovered neutron endpoints: %s",
                            self.name,
                            list(self._neutron_endpoints.keys()),
                        )
                except Exception as exc:
                    logger.warning("Failed to parse service catalog: %s", exc)

                return token

    # ------------------------------------------------------------------ billing

    async def get_balance(self) -> float:
        """Return RUB balance using the static API key."""
        if not self.api_key:
            return 0.0
        try:
            async with _make_session() as session:
                async with session.get(
                    f"{BILLING_BASE}/v3/balances",
                    headers={"X-Token": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        return 0.0
                    data = await resp.json()
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if isinstance(item, dict) and item.get("currency") == "RUB":
                            try:
                                return float(item.get("money", 0))
                            except (ValueError, TypeError):
                                pass
            return 0.0
        except Exception as exc:
            logger.debug("get_balance error for %s: %s", self.name, exc)
            return 0.0

    # ------------------------------------------------------------------ endpoints

    def _neutron_base(self, region: str) -> str:
        """Return the Neutron base URL for the region (from catalog or fallback)."""
        if region in self._neutron_endpoints:
            return self._neutron_endpoints[region]
        return neutron_url(region)

    def available_regions(self) -> List[str]:
        """Return regions discovered from the Keystone service catalog."""
        return list(self._neutron_endpoints.keys())

    # ------------------------------------------------------------------ networks

    async def _external_network(self, region: str) -> str:
        if region in self._ext_nets:
            return self._ext_nets[region]

        token = await self._get_token()
        base = self._neutron_base(region)

        async with _make_session() as session:
            async with session.get(
                f"{base}/v2.0/networks",
                params={"router:external": "true"},
                headers={"X-Auth-Token": token},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                await _raise_for(resp)
                data = await resp.json()
                nets: List[Dict] = data.get("networks", [])
                if not nets:
                    raise SelectelApiError(0, f"No external networks in region {region}")
                self._ext_nets[region] = nets[0]["id"]
                return self._ext_nets[region]

    # ------------------------------------------------------------------ floating IPs

    async def list_subnets(self, region: str) -> List[Dict]:
        """List subnets of the external network in the given region."""
        token = await self._get_token()
        ext_net = await self._external_network(region)
        base = self._neutron_base(region)

        async with _make_session() as session:
            async with session.get(
                f"{base}/v2.0/subnets",
                params={"network_id": ext_net},
                headers={"X-Auth-Token": token},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                return body.get("subnets", [])

    async def create_floatingip(
        self, region: str, ip_address: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Allocate a floating IPv4 in the given region.
        If ip_address is given, request that specific IP.

        Returns:
            (ip_address, floatip_id)
        """
        token = await self._get_token()
        ext_net = await self._external_network(region)
        base = self._neutron_base(region)

        fip_body: Dict = {"floating_network_id": ext_net}
        if ip_address:
            fip_body["floating_ip_address"] = ip_address

        async with _make_session() as session:
            async with session.post(
                f"{base}/v2.0/floatingips",
                json={"floatingip": fip_body},
                headers={"X-Auth-Token": token},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                fip = body["floatingip"]
                return fip["floating_ip_address"], fip["id"]

    async def delete_floatingip(self, region: str, floatip_id: str) -> None:
        """Release a floating IP by its ID."""
        token = await self._get_token()
        base = self._neutron_base(region)

        async with _make_session() as session:
            async with session.delete(
                f"{base}/v2.0/floatingips/{floatip_id}",
                headers={"X-Auth-Token": token},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 404:
                    return  # already gone, fine
                await _raise_for(resp)

    async def list_floatingips(self, region: str) -> List[Dict]:
        """Return all floating IPs allocated in the given region."""
        token = await self._get_token()
        base = self._neutron_base(region)

        async with _make_session() as session:
            async with session.get(
                f"{base}/v2.0/floatingips",
                headers={"X-Auth-Token": token},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                await _raise_for(resp)
                body = await resp.json()
                return body.get("floatingips", [])
