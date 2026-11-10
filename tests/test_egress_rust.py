"""ADR-0020 stage 1: the Rust port of the address checks agrees with egress/addresses.py.

Runs when the Rust binary is built (`cargo build --manifest-path rust/Cargo.toml`), or found
through CAIRNWARD_EGRESS_RS; skips otherwise. CI builds it.
"""

import ipaddress
import os
import random
import subprocess
from pathlib import Path

import pytest

from cairnward.egress import special_reason

ROOT = Path(__file__).resolve().parent.parent


def _binary() -> Path | None:
    if found := os.environ.get("CAIRNWARD_EGRESS_RS"):
        return Path(found)
    for profile in ("release", "debug"):
        exe = ROOT / "rust" / "target" / profile / "cairnward-egress-rs"
        for candidate in (exe, exe.with_suffix(".exe")):
            if candidate.is_file():
                return candidate
    return None


BINARY = _binary()
pytestmark = pytest.mark.skipif(BINARY is None, reason="Rust egress binary not built")


def _rust(addresses: list[str]) -> dict[str, str]:
    assert BINARY is not None
    out = subprocess.run([str(BINARY), "check-address"], input="\n".join(addresses) + "\n",
                         capture_output=True, text=True, check=True, timeout=60).stdout  # fmt: skip
    pairs = (line.split("\t", 1) for line in out.splitlines())
    return {ip: verdict for ip, verdict in pairs}


def _python(address: str) -> str:
    return special_reason(address) or "-"


def _corpus(n: int, seed: int) -> list[str]:
    """Ordinary and special addresses, and special IPv4 hidden in each IPv6 form."""
    rnd = random.Random(seed)  # noqa: S311 - reproducible test data, not secrets
    v4_special = ["127.0.0.0/8", "0.0.0.0/32", "169.254.0.0/16", "224.0.0.0/4",
                  "255.255.255.255/32", "100.100.100.200/32", "168.63.129.16/32",
                  "192.0.0.192/32", "10.0.0.0/8", "192.168.0.0/16"]  # fmt: skip
    v6_special = ["::1/128", "::/128", "fe80::/10", "ff00::/8", "fd00:ec2::254/128",
                  "2001:db8::/32", "fc00::/7"]  # fmt: skip

    def pick(net: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        network = ipaddress.ip_network(net)
        return network[rnd.randrange(network.num_addresses)]

    out: set[str] = set()
    while len(out) < n:
        kind = rnd.randrange(7)
        if kind == 0:
            out.add(str(ipaddress.IPv4Address(rnd.getrandbits(32))))
        elif kind == 1:
            out.add(str(ipaddress.IPv6Address(rnd.getrandbits(128))))
        elif kind == 2:
            out.add(str(pick(rnd.choice(v4_special))))
        elif kind == 3:
            out.add(str(pick(rnd.choice(v6_special))))
        else:
            v4 = int(pick(rnd.choice(v4_special)))
            if kind == 4:  # IPv4-mapped
                out.add(str(ipaddress.IPv6Address((0xFFFF << 32) | v4)))
            elif kind == 5:  # 6to4
                out.add(str(ipaddress.IPv6Address((0x2002 << 112) | (v4 << 80))))
            else:  # NAT64 well-known prefix
                out.add(str(ipaddress.IPv6Address((0x64FF9B << 96) | v4)))
    return sorted(out)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_rust_and_python_agree_on_every_address(seed: int) -> None:
    addresses = _corpus(4000, seed)
    rust = _rust(addresses)
    differ = [(a, _python(a), rust.get(a)) for a in addresses if rust.get(a) != _python(a)]
    assert not differ, differ[:10]


def test_rust_rejects_what_isnt_an_address() -> None:
    assert _rust(["not-an-ip", "300.1.1.1", "127.1"]) == {
        "not-an-ip": "invalid",
        "300.1.1.1": "invalid",
        "127.1": "invalid",
    }
