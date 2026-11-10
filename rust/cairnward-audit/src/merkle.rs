//! RFC 9162 Merkle trees over audit record hashes: a port of `cairnward/audit/merkle.py`.
//! Leaves are `SHA-256(0x00 || record hash)`, nodes `SHA-256(0x01 || left || right)`.

use sha2::{Digest, Sha256};

type Hash = [u8; 32];

pub fn parse_hash(hex: &str) -> Result<Hash, String> {
    if hex.len() != 64 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!(
            "not a hash: {:?}",
            hex.chars().take(20).collect::<String>()
        ));
    }
    let mut out = [0u8; 32];
    for (i, chunk) in hex.as_bytes().chunks(2).enumerate() {
        let s = std::str::from_utf8(chunk).map_err(|e| e.to_string())?;
        out[i] = u8::from_str_radix(s, 16).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

pub fn to_hex(h: &[u8]) -> String {
    use std::fmt::Write as _;
    h.iter()
        .fold(String::with_capacity(h.len() * 2), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

fn sha(parts: &[&[u8]]) -> Hash {
    let mut d = Sha256::new();
    for p in parts {
        d.update(p);
    }
    d.finalize().into()
}

pub fn leaf(record_hash: &Hash) -> Hash {
    sha(&[&[0u8], record_hash])
}

fn node(l: &Hash, r: &Hash) -> Hash {
    sha(&[&[1u8], l, r])
}

fn split(n: usize) -> usize {
    let mut k = 1;
    while k * 2 < n {
        k *= 2;
    }
    k
}

fn root_of(leaves: &[Hash]) -> Hash {
    match leaves.len() {
        0 => sha(&[]),
        1 => leaves[0],
        n => {
            let k = split(n);
            node(&root_of(&leaves[..k]), &root_of(&leaves[k..]))
        }
    }
}

/// The root over these record hashes, in order (RFC 9162 MTH).
pub fn root(record_hashes: &[Hash]) -> Hash {
    root_of(&record_hashes.iter().map(leaf).collect::<Vec<_>>())
}

/// RFC 9162 §2.1.3.2: does `record_hash` sit at leaf `index` of the tree of `size` with `root`?
pub fn verify_inclusion(
    record_hash: &Hash,
    index: u64,
    size: u64,
    proof: &[Hash],
    expected: &Hash,
) -> bool {
    if index >= size {
        return false;
    }
    let (mut fn_, mut sn) = (index, size - 1);
    let mut r = leaf(record_hash);
    for p in proof {
        if sn == 0 {
            return false;
        }
        if fn_ % 2 == 1 || fn_ == sn {
            r = node(p, &r);
            if fn_ % 2 == 0 {
                while fn_ % 2 == 0 && fn_ != 0 {
                    fn_ >>= 1;
                    sn >>= 1;
                }
            }
        } else {
            r = node(&r, p);
        }
        fn_ >>= 1;
        sn >>= 1;
    }
    sn == 0 && &r == expected
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc6962_reference_roots() {
        let data: [&[u8]; 8] = [
            b"",
            b"\x00",
            b"\x10",
            b"\x20\x21",
            b"\x30\x31",
            b"\x40\x41\x42\x43",
            b"\x50\x51\x52\x53\x54\x55\x56\x57",
            b"\x60\x61\x62\x63\x64\x65\x66\x67\x68\x69\x6a\x6b\x6c\x6d\x6e\x6f",
        ];
        let leaves: Vec<Hash> = data.iter().map(|d| sha(&[&[0u8], d])).collect();
        assert_eq!(
            to_hex(&root_of(&leaves[..1])),
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
        );
        assert_eq!(
            to_hex(&root_of(&leaves[..4])),
            "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7"
        );
        assert_eq!(
            to_hex(&root_of(&leaves)),
            "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"
        );
    }
}
