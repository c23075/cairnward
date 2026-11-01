"""Destination address checks (protection against DNS tricks, spec §3.2).

An allowed host name can resolve to anything its DNS owner chooses, including this
machine (the gateway, Ollama), link-local addresses or cloud metadata services. Those are
refused unless the exact IP is explicitly allowed. Private LAN ranges follow the normal
network policy.
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Cloud metadata / platform endpoints outside the link-local range.
_METADATA = {
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    ipaddress.ip_address("168.63.129.16"),  # Azure
    ipaddress.ip_address("192.0.0.192"),  # Oracle
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6
}


def _unwrap(ip: IPAddress) -> IPAddress:
    """IPv4 addresses hidden inside IPv6 forms are judged as IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 well-known prefix
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def special_reason(address: str) -> str | None:
    """Why this address may not be dialled by name, or None if it's an ordinary address."""
    ip = _unwrap(ipaddress.ip_address(address))
    if ip.is_loopback:
        return "a loopback address (this machine)"
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_link_local:
        return "a link-local address (includes cloud metadata services)"
    if ip.is_multicast:
        return "a multicast address"
    if ip in _METADATA:
        return "a cloud metadata address"
    if isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("255.255.255.255"):
        return "the broadcast address"
    return None


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def literal_address(host: str) -> str | None:
    """The address a host string denotes without DNS: IP literals, inet_aton shorthand
    such as 127.1 or 2130706433, and localhost names; None for ordinary names."""
    import socket

    host = host.lower().rstrip(".")
    if is_ip_literal(host):
        return host.strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return "127.0.0.1"
    if host.replace(".", "").replace("x", "").isalnum() and host[:1].isdigit():
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return None
    return None
