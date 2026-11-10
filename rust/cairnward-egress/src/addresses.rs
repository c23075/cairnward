//! Destination address checks: a port of `cairnward/egress/addresses.py` (spec §3.2).
//!
//! An allowed host name can resolve to anything its DNS owner chooses, including this machine,
//! link-local addresses or cloud metadata services. Those are refused unless the exact IP is
//! explicitly allowed. Private LAN ranges follow the normal network policy.
//!
//! The reasons are the same strings as the Python version's, and
//! `tests/test_egress_rust.py` compares the two over generated addresses (ADR-0020, stage 1).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// Cloud metadata and platform endpoints outside the link-local range.
const METADATA_V4: [Ipv4Addr; 3] = [
    Ipv4Addr::new(100, 100, 100, 200), // Alibaba Cloud
    Ipv4Addr::new(168, 63, 129, 16),   // Azure
    Ipv4Addr::new(192, 0, 0, 192),     // Oracle
];
const METADATA_V6: [Ipv6Addr; 1] = [
    Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254), // AWS IPv6
];

fn low32(s: [u16; 8]) -> Ipv4Addr {
    Ipv4Addr::from((u32::from(s[6]) << 16) | u32::from(s[7]))
}

/// IPv4 addresses hidden inside IPv6 forms are judged as IPv4: IPv4-mapped (`::ffff:a.b.c.d`),
/// 6to4 (`2002::/16`) and the NAT64 well-known prefix (`64:ff9b::/96`).
fn unwrap(ip: IpAddr) -> IpAddr {
    let IpAddr::V6(v6) = ip else { return ip };
    let s = v6.segments();
    if s[..5] == [0, 0, 0, 0, 0] && s[5] == 0xffff {
        return IpAddr::V4(low32(s));
    }
    if s[0] == 0x2002 {
        let embedded = (u32::from(s[1]) << 16) | u32::from(s[2]);
        return IpAddr::V4(Ipv4Addr::from(embedded));
    }
    if s[..6] == [0x0064, 0xff9b, 0, 0, 0, 0] {
        return IpAddr::V4(low32(s));
    }
    ip
}

/// Why this address may not be dialled by name, or `None` if it's an ordinary address.
#[must_use]
pub fn special_reason(ip: IpAddr) -> Option<&'static str> {
    match unwrap(ip) {
        IpAddr::V4(v4) => {
            if v4.is_loopback() {
                Some("a loopback address (this machine)")
            } else if v4.is_unspecified() {
                Some("an unspecified address")
            } else if v4.is_link_local() {
                Some("a link-local address (includes cloud metadata services)")
            } else if v4.is_multicast() {
                Some("a multicast address")
            } else if METADATA_V4.contains(&v4) {
                Some("a cloud metadata address")
            } else if v4.is_broadcast() {
                Some("the broadcast address")
            } else {
                None
            }
        }
        IpAddr::V6(v6) => {
            if v6.is_loopback() {
                Some("a loopback address (this machine)")
            } else if v6.is_unspecified() {
                Some("an unspecified address")
            } else if (v6.segments()[0] & 0xffc0) == 0xfe80 {
                Some("a link-local address (includes cloud metadata services)")
            } else if v6.is_multicast() {
                Some("a multicast address")
            } else if METADATA_V6.contains(&v6) {
                Some("a cloud metadata address")
            } else {
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::special_reason;
    use std::net::IpAddr;

    fn reason(text: &str) -> Option<&'static str> {
        special_reason(
            text.parse::<IpAddr>()
                .unwrap_or_else(|_| panic!("bad test IP {text}")),
        )
    }

    #[test]
    fn same_cases_as_the_python_tests() {
        for (ip, special) in [
            ("8.8.8.8", false),
            ("10.0.0.5", false),
            ("192.168.1.2", false),
            ("127.0.0.53", true),
            ("::1", true),
            ("0.0.0.0", true),
            ("169.254.169.254", true),
            ("fe80::1", true),
            ("224.0.0.1", true),
            ("::ffff:169.254.169.254", true),
            ("64:ff9b::7f00:1", true),
            ("2002:7f00:1::", true),
            ("168.63.129.16", true),
        ] {
            assert_eq!(reason(ip).is_some(), special, "{ip}");
        }
    }

    #[test]
    fn reasons_match_the_python_wording() {
        assert_eq!(
            reason("127.0.0.1"),
            Some("a loopback address (this machine)")
        );
        assert_eq!(reason("255.255.255.255"), Some("the broadcast address"));
        assert_eq!(reason("fd00:ec2::254"), Some("a cloud metadata address"));
        assert_eq!(reason("2001:db8::1"), None);
    }
}
