import ipaddress
from typing import Dict, Iterator, List, Optional

# Whitelist subnets — floating IPs in these ranges are considered "good"
WHITELIST_CIDRS: List[str] = [
    "109.71.12.0/24",
    "109.71.13.0/24",
    "146.185.192.0/24",
    "164.138.102.0/24",
    "185.189.195.0/24",
    "185.91.52.0/24",
    "185.91.53.0/24",
    "185.91.54.0/24",
    "185.91.55.0/24",
    "188.68.218.0/24",
    "188.68.219.0/24",
    "31.129.42.0/24",
    "31.131.251.0/24",
    "31.184.215.0/24",
    "37.9.4.0/24",
    "5.178.85.0/24",
    "5.188.112.0/24",
    "5.188.113.0/24",
    "5.188.114.0/24",
    "5.188.115.0/24",
    "81.163.22.0/24",
    "81.163.23.0/24",
    "82.202.220.0/24",
    "82.202.252.0/24",
    "87.228.101.0/24",
]

_NETWORKS = [ipaddress.ip_network(cidr, strict=False) for cidr in WHITELIST_CIDRS]


def ip_in_whitelist(ip_str: str) -> bool:
    """Return True if the given IPv4 address belongs to any whitelist subnet."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _NETWORKS)
    except ValueError:
        return False


def whitelist_ips_in_pools(pools: List[Dict]) -> Iterator[str]:
    """
    Yield IP address strings that fall inside both the given allocation
    pools and the whitelist.  pools is the Neutron subnet allocation_pools
    list: [{"start": "x.x.x.x", "end": "x.x.x.x"}, ...].
    """
    for pool in pools:
        try:
            p_start = int(ipaddress.ip_address(pool["start"]))
            p_end = int(ipaddress.ip_address(pool["end"]))
        except (KeyError, ValueError):
            continue
        for wnet in _NETWORKS:
            w_start = int(wnet.network_address)
            w_end = int(wnet.broadcast_address)
            i_start = max(p_start, w_start)
            i_end = min(p_end, w_end)
            for ip_int in range(i_start, i_end + 1):
                yield str(ipaddress.ip_address(ip_int))


def get_matching_subnet(ip_str: str) -> Optional[str]:
    """Return the CIDR string of the first whitelist subnet that contains the IP."""
    try:
        addr = ipaddress.ip_address(ip_str)
        for net in _NETWORKS:
            if addr in net:
                return str(net)
    except ValueError:
        pass
    return None
