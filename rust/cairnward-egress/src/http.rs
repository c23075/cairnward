//! The parts of HTTP/1.1 the data path needs, written to match `egress/proxy.py` and
//! `egress/inspect.py` (which uses h11): request heads read and checked by hand, and message
//! bodies framed exactly (Content-Length or chunked) so nothing after a request's body is ever
//! forwarded (I28).

use std::fmt;
use std::time::Duration;

use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::time::timeout;

pub const MAX_HEAD: usize = 64 * 1024;

#[derive(Debug)]
pub enum Error {
    /// A 400 for the client, with the same wording as the Python data path.
    Bad(String),
    /// The other side went away or timed out.
    Closed,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Bad(msg) => write!(f, "{msg}"),
            Error::Closed => write!(f, "connection closed"),
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Error::Closed
    }
}

fn bad(msg: impl Into<String>) -> Error {
    Error::Bad(msg.into())
}

/// Read up to and including the blank line that ends a head (at most `MAX_HEAD` bytes).
pub async fn read_head<R: AsyncBufRead + Unpin>(
    r: &mut R,
    wait: Duration,
) -> Result<Vec<u8>, Error> {
    let mut head = Vec::new();
    let read = async {
        loop {
            let before = head.len();
            let n = r.read_until(b'\n', &mut head).await?;
            if n == 0 {
                return Err(Error::Closed);
            }
            if head.len() > MAX_HEAD {
                return Err(bad("request header too large"));
            }
            if head[before..].ends_with(b"\r\n") && head.ends_with(b"\r\n\r\n") {
                return Ok(());
            }
        }
    };
    match timeout(wait, read).await {
        Ok(Ok(())) => Ok(head),
        Ok(Err(e)) => Err(e),
        Err(_) => Err(Error::Closed),
    }
}

/// A request head, checked as `EgressProxy._handle_one` checks it.
#[derive(Debug)]
pub struct Head {
    pub method: String,
    pub target: String,
    pub version: String,
    pub headers: Vec<(String, String)>,
}

impl Head {
    pub fn get(&self, name: &str) -> Option<&str> {
        // the last one wins, like the Python dict built from the header list
        self.headers
            .iter()
            .rev()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    pub fn all(&self, name: &str) -> Vec<&str> {
        self.headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
            .collect()
    }
}

fn is_control(b: u8) -> bool {
    // [\x00-\x08\x0a-\x1f\x7f]: tab is allowed, a bare LF or CR inside a line is not
    matches!(b, 0x00..=0x08 | 0x0a..=0x1f | 0x7f)
}

pub fn parse_head(raw: &[u8]) -> Result<Head, Error> {
    // latin-1: every byte is one character, as in the Python version
    let text: String = raw.iter().map(|&b| char::from(b)).collect();
    let lines: Vec<&str> = text
        .strip_suffix("\r\n\r\n")
        .unwrap_or(&text)
        .split("\r\n")
        .collect();
    if lines.iter().any(|l| l.bytes().any(is_control)) {
        return Err(bad("control characters in the request header"));
    }
    let mut parts = lines[0].splitn(3, ' ');
    let (Some(method), Some(target), Some(version)) = (parts.next(), parts.next(), parts.next())
    else {
        return Err(bad("malformed request line"));
    };
    if method.is_empty() || version.contains(' ') || !version.starts_with("HTTP/") {
        return Err(bad("malformed request line"));
    }
    let mut headers = Vec::new();
    for line in &lines[1..] {
        if line.is_empty() {
            continue;
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err(bad("malformed header"));
        };
        if name.trim().is_empty() || name != name.trim() {
            return Err(bad("malformed header"));
        }
        headers.push((name.trim().to_string(), value.trim().to_string()));
    }
    Ok(Head {
        method: method.to_string(),
        target: target.to_string(),
        version: version.to_string(),
        headers,
    })
}

/// `_split_hostport`: "host:port", "[v6]:port" or a bare host; host lowercased.
pub fn split_hostport(value: &str, default_port: u16) -> Result<(String, u16), Error> {
    let (host, port) = if let Some(rest) = value.strip_prefix('[') {
        let (host, rest) = rest.split_once(']').unwrap_or((rest, ""));
        (host, rest.strip_prefix(':').unwrap_or(""))
    } else if let Some((host, port)) = value.rsplit_once(':') {
        (host, port)
    } else {
        (value, "")
    };
    if host.is_empty() {
        return Err(bad("missing host"));
    }
    let port = if port.is_empty() {
        default_port
    } else {
        match port.parse::<u32>() {
            Ok(p) if (1..65536).contains(&p) => {
                u16::try_from(p).map_err(|_| bad(format!("bad port in {value:?}")))?
            }
            _ => return Err(bad(format!("bad port in {}", py_repr(value)))),
        }
    };
    Ok((host.to_lowercase(), port))
}

/// Python's `repr()` of a str, for messages that quote the request (ASCII only; enough here).
pub fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// How a message's body is delimited.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Framing {
    None,
    Length(u64),
    Chunked,
    UntilClose,
}

fn content_length(values: &[&str]) -> Result<Option<u64>, Error> {
    let mut found: Option<u64> = None;
    for v in values {
        for part in v.split(',') {
            let part = part.trim();
            if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
                return Err(bad("bad Content-Length"));
            }
            let n: u64 = part.parse().map_err(|_| bad("bad Content-Length"))?;
            if found.is_some_and(|f| f != n) {
                return Err(bad("conflicting Content-Length values"));
            }
            found = Some(n);
        }
    }
    Ok(found)
}

/// A request's framing (RFC 9112 §6.3), refusing what h11 refuses.
pub fn request_framing(head: &Head) -> Result<Framing, Error> {
    let te = head.all("transfer-encoding");
    let cl = content_length(&head.all("content-length"))?;
    if !te.is_empty() {
        if cl.is_some() {
            return Err(bad("both Transfer-Encoding and Content-Length"));
        }
        if te.len() != 1 || !te[0].trim().eq_ignore_ascii_case("chunked") {
            return Err(bad("only Transfer-Encoding: chunked is supported"));
        }
        return Ok(Framing::Chunked);
    }
    Ok(cl.map_or(Framing::None, Framing::Length))
}

/// A response's framing: no body for HEAD, 1xx, 204 and 304.
pub fn response_framing(
    request_method: &str,
    status: u16,
    headers: &[(String, String)],
) -> Result<Framing, Error> {
    if request_method.eq_ignore_ascii_case("HEAD")
        || (100..200).contains(&status)
        || status == 204
        || status == 304
    {
        return Ok(Framing::None);
    }
    let get = |n: &str| {
        headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case(n))
            .map(|(_, v)| v.as_str())
            .collect::<Vec<_>>()
    };
    let te = get("transfer-encoding");
    if te.last().is_some_and(|v| {
        v.split(',')
            .next_back()
            .is_some_and(|x| x.trim().eq_ignore_ascii_case("chunked"))
    }) {
        return Ok(Framing::Chunked);
    }
    match content_length(&get("content-length"))? {
        Some(n) => Ok(Framing::Length(n)),
        None => Ok(Framing::UntilClose),
    }
}

/// Copy exactly one body from `r` to `w`, framing included; returns the body bytes copied.
pub async fn copy_body<R, W>(
    r: &mut R,
    w: &mut W,
    framing: Framing,
    idle: Duration,
) -> Result<u64, Error>
where
    R: AsyncBufRead + Unpin,
    W: AsyncWrite + Unpin,
{
    match framing {
        Framing::None => Ok(0),
        Framing::Length(n) => copy_exact(r, w, n, idle).await,
        Framing::UntilClose => {
            let mut total = 0u64;
            let mut buf = vec![0u8; 65536];
            loop {
                let n = timeout(idle, r.read(&mut buf))
                    .await
                    .map_err(|_| Error::Closed)??;
                if n == 0 {
                    return Ok(total);
                }
                w.write_all(&buf[..n]).await?;
                total += n as u64;
            }
        }
        Framing::Chunked => {
            let mut total = 0u64;
            loop {
                let line = read_line(r, idle).await?;
                w.write_all(&line).await?;
                let text = String::from_utf8_lossy(&line);
                let size_text = text.trim_end().split(';').next().unwrap_or("").trim();
                let size = u64::from_str_radix(size_text, 16).map_err(|_| bad("bad chunk size"))?;
                if size == 0 {
                    // trailers, then the blank line
                    loop {
                        let t = read_line(r, idle).await?;
                        w.write_all(&t).await?;
                        if t == b"\r\n" || t == b"\n" {
                            return Ok(total);
                        }
                    }
                }
                total += copy_exact(r, w, size, idle).await?;
                let end = read_line(r, idle).await?;
                if end != b"\r\n" && end != b"\n" {
                    return Err(bad("chunk not followed by CRLF"));
                }
                w.write_all(&end).await?;
            }
        }
    }
}

async fn read_line<R: AsyncBufRead + Unpin>(r: &mut R, idle: Duration) -> Result<Vec<u8>, Error> {
    let mut line = Vec::new();
    let n = timeout(idle, r.read_until(b'\n', &mut line))
        .await
        .map_err(|_| Error::Closed)??;
    if n == 0 || line.len() > MAX_HEAD {
        return Err(Error::Closed);
    }
    Ok(line)
}

async fn copy_exact<R, W>(r: &mut R, w: &mut W, n: u64, idle: Duration) -> Result<u64, Error>
where
    R: AsyncBufRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let mut left = n;
    let mut buf = vec![0u8; 65536];
    while left > 0 {
        let want = usize::try_from(left.min(buf.len() as u64)).unwrap_or(buf.len());
        let got = timeout(idle, r.read(&mut buf[..want]))
            .await
            .map_err(|_| Error::Closed)??;
        if got == 0 {
            return Err(Error::Closed);
        }
        w.write_all(&buf[..got]).await?;
        left -= got as u64;
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heads_are_checked_like_the_python_path() {
        assert!(
            matches!(parse_head(b"GET / HTTP/1.1\r\nX: a\nb\r\n\r\n"), Err(Error::Bad(m)) if m.contains("control"))
        );
        assert!(
            matches!(parse_head(b"NONSENSE\r\n\r\n"), Err(Error::Bad(m)) if m == "malformed request line")
        );
        assert!(
            matches!(parse_head(b"GET / HTTP/1.1\r\n x: y\r\n\r\n"), Err(Error::Bad(m)) if m == "malformed header")
        );
        let h = parse_head(b"CONNECT a:443 HTTP/1.1\r\nHost: a\r\nhost: b\r\n\r\n")
            .unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(h.all("host"), vec!["a", "b"]);
    }

    #[test]
    fn host_and_port() {
        let ok = |v: &str, d| split_hostport(v, d).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(ok("example.org:443", 80), ("example.org".into(), 443));
        assert_eq!(ok("Example.org", 80), ("example.org".into(), 80));
        assert_eq!(ok("[::1]:8080", 80), ("::1".into(), 8080));
        assert_eq!(ok("[2001:db8::1]", 80), ("2001:db8::1".into(), 80));
        for v in [":443", "host:99999", "host:abc"] {
            assert!(split_hostport(v, 80).is_err(), "{v}");
        }
    }

    #[test]
    fn framing_refuses_what_h11_refuses() {
        let head = |extra: &str| {
            parse_head(format!("POST /x HTTP/1.1\r\nHost: h\r\n{extra}\r\n").as_bytes())
                .unwrap_or_else(|e| panic!("{e}"))
        };
        assert_eq!(
            request_framing(&head("Content-Length: 4\r\n")).ok(),
            Some(Framing::Length(4))
        );
        assert_eq!(
            request_framing(&head("Transfer-Encoding: chunked\r\n")).ok(),
            Some(Framing::Chunked)
        );
        assert!(
            request_framing(&head("Transfer-Encoding: chunked\r\nContent-Length: 4\r\n")).is_err()
        );
        assert!(request_framing(&head("Content-Length: 4\r\nContent-Length: 5\r\n")).is_err());
        assert!(request_framing(&head("Transfer-Encoding: gzip\r\n")).is_err());
        assert!(request_framing(&head("Content-Length: -1\r\n")).is_err());
    }

    #[tokio::test]
    async fn a_chunked_body_is_copied_exactly_and_nothing_after_it() {
        let input = b"3\r\nabc\r\n0\r\n\r\nGET /next HTTP/1.1\r\n\r\n".to_vec();
        let mut r = tokio::io::BufReader::new(&input[..]);
        let mut out = Vec::new();
        let n = copy_body(&mut r, &mut out, Framing::Chunked, Duration::from_secs(1))
            .await
            .unwrap_or_else(|e| panic!("{e}"));
        assert_eq!((n, out.as_slice()), (3, &b"3\r\nabc\r\n0\r\n\r\n"[..]));
    }
}
