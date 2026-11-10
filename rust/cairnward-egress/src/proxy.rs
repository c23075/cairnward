//! The proxy: each connection handled as `EgressProxy._handle_one` handles it, with policy,
//! approvals and audit records from the decision service (ADR-0020, stage 2).

use std::fmt::Write as _;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;
use tokio::time::timeout;

use crate::addresses::special_reason;
use crate::decisions::Decisions;
use crate::http::{self, Error, Framing};

#[derive(Clone, Debug)]
pub struct Limits {
    pub connect_timeout: Duration,
    pub idle_timeout: Duration,
    pub max_connections: usize,
}

pub struct Proxy {
    pub decisions: PathBuf,
    pub limits: Limits,
    pub tls: crate::inspect::Tls,
}

const HOP: [&str; 9] = [
    "proxy-authorization",
    "proxy-connection",
    "connection",
    "keep-alive",
    "host",
    "expect",
    "te",
    "trailer",
    "upgrade",
];

async fn reply<W: AsyncWrite + Unpin>(
    w: &mut W,
    status: u16,
    reason: &str,
    body: &str,
    extra: &[(&str, &str)],
) {
    let mut lines = vec![
        format!("HTTP/1.1 {status} {reason}"),
        format!("Content-Length: {}", body.len()),
        "Content-Type: text/plain; charset=utf-8".to_string(),
        "Connection: close".to_string(),
    ];
    lines.extend(extra.iter().map(|(k, v)| format!("{k}: {v}")));
    let mut data = (lines.join("\r\n") + "\r\n\r\n").into_bytes();
    data.extend_from_slice(body.as_bytes());
    let _ = w.write_all(&data).await;
    let _ = w.flush().await;
}

async fn deny_reply<W: AsyncWrite + Unpin>(w: &mut W, target: &str, reason: &str) {
    let body = format!("cairnward-egress: {target} denied: {reason}\n");
    reply(
        w,
        403,
        "Forbidden by cairnward",
        &body,
        &[("X-Cairnward-Decision", "deny")],
    )
    .await;
}

fn error_name(e: &std::io::Error) -> &'static str {
    match e.kind() {
        std::io::ErrorKind::ConnectionRefused => "ConnectionRefusedError",
        std::io::ErrorKind::ConnectionReset => "ConnectionResetError",
        std::io::ErrorKind::TimedOut => "TimeoutError",
        _ => "OSError",
    }
}

/// What the request asks for: a tunnel, or one plain-HTTP request.
struct Target {
    host: String,
    port: u16,
    /// path and query, for plain HTTP; None for CONNECT
    path: Option<String>,
    host_header: String,
}

fn target_of(head: &http::Head) -> Result<Target, Error> {
    if head.method.eq_ignore_ascii_case("CONNECT") {
        let (host, port) = http::split_hostport(&head.target, 443)?;
        return Ok(Target {
            host,
            port,
            path: None,
            host_header: String::new(),
        });
    }
    // urlsplit's view of an absolute http:// URL
    let only = || Error::Bad("only CONNECT and absolute http:// URLs are proxied".to_string());
    let (scheme, rest) = head.target.split_once("://").ok_or_else(only)?;
    if !scheme.eq_ignore_ascii_case("http") {
        return Err(only());
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, tail) = rest.split_at(end);
    if netloc.is_empty() {
        return Err(only());
    }
    let tail = tail.split('#').next().unwrap_or("");
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    let mut path = if path.is_empty() {
        "/".to_string()
    } else {
        path.to_string()
    };
    if !query.is_empty() {
        path = format!("{path}?{query}");
    }
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let malformed = || {
        Error::Bad(format!(
            "malformed URL {}",
            http::py_repr(&head.target.chars().take(100).collect::<String>())
        ))
    };
    // urlsplit: brackets must pair, and hold a valid IPv6 address
    if hostport.contains('[') || hostport.contains(']') {
        let inner = hostport
            .strip_prefix('[')
            .and_then(|r| r.split_once(']'))
            .map(|(h, _)| h);
        if !inner.is_some_and(|h| h.parse::<std::net::Ipv6Addr>().is_ok()) {
            return Err(malformed());
        }
    }
    let (host, port) = http::split_hostport(hostport, 80).map_err(|_| {
        Error::Bad(format!(
            "malformed URL {}",
            http::py_repr(&head.target.chars().take(100).collect::<String>())
        ))
    })?;
    let name = if host.contains(':') {
        format!("[{host}]")
    } else {
        host.clone()
    };
    let host_header = if port == 80 {
        name
    } else {
        format!("{name}:{port}")
    };
    Ok(Target {
        host,
        port,
        path: Some(path),
        host_header,
    })
}

async fn resolve(host: &str, port: u16) -> Result<Vec<IpAddr>, String> {
    if let Ok(ip) = host
        .trim_matches(|c| c == '[' || c == ']')
        .parse::<IpAddr>()
    {
        return Ok(vec![ip]);
    }
    let found = tokio::net::lookup_host((host, port))
        .await
        .map_err(|e| format!("DNS lookup failed: {e}"))?;
    let mut out: Vec<IpAddr> = Vec::new();
    for addr in found {
        if !out.contains(&addr.ip()) {
            out.push(addr.ip());
        }
    }
    Ok(out)
}

impl Proxy {
    pub async fn serve(self: Arc<Self>, listener: TcpListener) -> std::io::Result<()> {
        let slots = Arc::new(Semaphore::new(self.limits.max_connections));
        loop {
            let (stream, _) = listener.accept().await?;
            let permit = Arc::clone(&slots)
                .acquire_owned()
                .await
                .map_err(std::io::Error::other)?;
            let me = Arc::clone(&self);
            tokio::spawn(async move {
                me.handle(stream).await;
                drop(permit);
            });
        }
    }

    async fn handle(&self, stream: TcpStream) {
        let (r, mut w) = stream.into_split();
        let mut client = BufReader::new(r);
        let result = self.handle_one(&mut client, &mut w).await;
        if let Err(Error::Bad(msg)) = result {
            reply(
                &mut w,
                400,
                "Bad Request",
                &format!("cairnward-egress: {msg}\n"),
                &[],
            )
            .await;
        }
        let _ = w.shutdown().await;
    }

    #[allow(clippy::too_many_lines)] // one connection's whole story, in the Python order
    async fn handle_one(
        &self,
        client: &mut BufReader<tokio::net::tcp::OwnedReadHalf>,
        w: &mut tokio::net::tcp::OwnedWriteHalf,
    ) -> Result<(), Error> {
        let raw = http::read_head(client, Duration::from_secs(30)).await?;
        let head = http::parse_head(&raw)?;
        let mut dec = Decisions::open(&self.decisions)
            .await
            .map_err(|_| Error::Closed)?;
        // the token first, as Python does: a client without one learns nothing more
        let auth = head.get("proxy-authorization").map(str::to_string);
        if !dec
            .session_valid(auth.as_deref())
            .await
            .map_err(|_| Error::Closed)?
        {
            reply(
                w,
                407,
                "Proxy Authentication Required",
                "cairnward-egress: missing or unknown session token\n",
                &[("Proxy-Authenticate", "Basic realm=\"cairnward\"")],
            )
            .await;
            return Ok(());
        }
        let target = target_of(&head)?;
        let display = if target.host.contains(':') {
            format!("[{}]:{}", target.host, target.port)
        } else {
            format!("{}:{}", target.host, target.port)
        };
        let c = dec
            .connect(
                auth.as_deref(),
                &head.method.to_uppercase(),
                &target.host,
                target.port,
            )
            .await
            .map_err(|_| Error::Closed)?;
        match c.status {
            407 => {
                reply(
                    w,
                    407,
                    "Proxy Authentication Required",
                    "cairnward-egress: missing or unknown session token\n",
                    &[("Proxy-Authenticate", "Basic realm=\"cairnward\"")],
                )
                .await;
                return Ok(());
            }
            400 => return Err(Error::Bad(c.reason)),
            _ if !c.allow => {
                deny_reply(w, &display, &c.reason).await;
                return Ok(());
            }
            _ => {}
        }
        if c.inspect {
            if let Some(path) = &target.path {
                // plain HTTP: the request is already readable, so the same rules apply to it
                if let Some(why) = crate::inspect::check_plain(&mut dec, &head.method, path)
                    .await
                    .map_err(|_| Error::Closed)?
                {
                    let _ = dec.drop_ticket().await;
                    let body = format!("cairnward-egress: {} denied: {why}\n", head.method);
                    reply(
                        w,
                        403,
                        "Forbidden by cairnward",
                        &body,
                        &[("X-Cairnward-Decision", "deny")],
                    )
                    .await;
                    return Ok(());
                }
            }
        }
        // resolve once, refuse special addresses, dial what was checked
        let addresses = match resolve(&target.host, target.port).await {
            Ok(a) => a,
            Err(why) => {
                let _ = dec.deny(&why).await;
                deny_reply(w, &display, &why).await;
                return Ok(());
            }
        };
        let mut chosen = None;
        let mut refused: Vec<&str> = Vec::new();
        for ip in &addresses {
            match special_reason(*ip) {
                None => {
                    chosen = Some(*ip);
                    break;
                }
                Some(why) => {
                    if dec
                        .allow_special(&ip.to_string())
                        .await
                        .map_err(|_| Error::Closed)?
                    {
                        chosen = Some(*ip);
                        break;
                    }
                    refused.push(why);
                }
            }
        }
        let Some(ip) = chosen else {
            let why = format!(
                "{} resolves to {}",
                target.host,
                refused.first().copied().unwrap_or("nothing")
            );
            let _ = dec.deny(&why).await;
            deny_reply(w, &display, &why).await;
            return Ok(());
        };
        dec.opened().await.map_err(|_| Error::Closed)?;
        let started = Instant::now();
        let (mut up, mut down, mut status, mut error) = (0u64, 0u64, None::<u16>, None::<String>);
        let mut bad_request = None;
        match timeout(
            self.limits.connect_timeout,
            TcpStream::connect((ip, target.port)),
        )
        .await
        {
            Err(_) => error = Some("connect failed: TimeoutError".to_string()),
            Ok(Err(e)) => error = Some(format!("connect failed: {}", error_name(&e))),
            Ok(Ok(upstream)) => {
                let outcome = if let Some(path) = &target.path {
                    self.forward_once(&head, path, &target.host_header, client, w, upstream)
                        .await
                } else if c.inspect {
                    let _ = w
                        .write_all(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        .await;
                    let o = crate::inspect::tunnel(
                        &self.tls,
                        &mut dec,
                        &target.host,
                        client,
                        w,
                        upstream,
                        self.limits.connect_timeout,
                        self.limits.idle_timeout,
                    )
                    .await;
                    if let Some(e) = o.error {
                        error = Some(e);
                    }
                    Ok((o.bytes_up, o.bytes_down, None))
                } else {
                    let _ = w
                        .write_all(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        .await;
                    self.tunnel(client, w, upstream).await
                };
                match outcome {
                    Ok((u, d, s)) => (up, down, status) = (u, d, s),
                    Err((u, d, Error::Bad(msg))) => {
                        (up, down) = (u, d);
                        let msg = format!("malformed request: {msg}");
                        error = Some(msg.chars().take(200).collect());
                        bad_request = Some(msg);
                    }
                    Err((u, d, Error::Closed)) => {
                        (up, down) = (u, d);
                        error = Some("ConnectionError".to_string());
                    }
                }
            }
        }
        if let Some(e) = &error {
            if e.starts_with("connect failed") {
                reply(
                    w,
                    502,
                    "Bad Gateway",
                    &format!("cairnward-egress: {e}\n"),
                    &[],
                )
                .await;
            }
        }
        if let Some(msg) = bad_request {
            reply(
                w,
                400,
                "Bad Request",
                &format!("cairnward-egress: {msg}\n"),
                &[],
            )
            .await;
        }
        let mut closed = json!({
            "outcome": if error.is_none() { "ok" } else { "error" },
            "bytes_up": up, "bytes_down": down,
            "duration_s": started.elapsed().as_secs_f64(),
            "address": ip.to_string(),
            "error": error,
        });
        if let Some(s) = status {
            closed["status"] = json!(s);
        }
        let _ = dec.closed(closed).await;
        Ok(())
    }

    async fn tunnel(
        &self,
        client: &mut BufReader<tokio::net::tcp::OwnedReadHalf>,
        w: &mut tokio::net::tcp::OwnedWriteHalf,
        upstream: TcpStream,
    ) -> Result<(u64, u64, Option<u16>), (u64, u64, Error)> {
        let idle = self.limits.idle_timeout;
        let (mut ur, mut uw) = upstream.into_split();
        let to_up = async {
            let mut n = 0u64;
            let mut buf = vec![0u8; 65536];
            loop {
                match timeout(idle, client.read(&mut buf)).await {
                    Ok(Ok(k)) if k > 0 => {
                        if uw.write_all(&buf[..k]).await.is_err() {
                            break;
                        }
                        n += k as u64;
                    }
                    _ => break,
                }
            }
            let _ = uw.shutdown().await;
            n
        };
        let to_down = async {
            let mut n = 0u64;
            let mut buf = vec![0u8; 65536];
            loop {
                match timeout(idle, ur.read(&mut buf)).await {
                    Ok(Ok(k)) if k > 0 => {
                        if w.write_all(&buf[..k]).await.is_err() {
                            break;
                        }
                        n += k as u64;
                    }
                    _ => break,
                }
            }
            let _ = w.shutdown().await;
            n
        };
        let (up, down) = tokio::join!(to_up, to_down);
        Ok((up, down, None))
    }

    /// One plain-HTTP request and its response, then the end (I28); `Host` from the checked URL.
    async fn forward_once(
        &self,
        head: &http::Head,
        path: &str,
        host_header: &str,
        client: &mut BufReader<tokio::net::tcp::OwnedReadHalf>,
        w: &mut tokio::net::tcp::OwnedWriteHalf,
        upstream: TcpStream,
    ) -> Result<(u64, u64, Option<u16>), (u64, u64, Error)> {
        let idle = self.limits.idle_timeout;
        let hosts = head.all("host");
        if hosts.len() > 1 || (hosts.is_empty() && head.version == "HTTP/1.1") {
            return Err((
                0,
                0,
                Error::Bad(if hosts.is_empty() {
                    "Missing mandatory Host: header".into()
                } else {
                    "Found multiple Host: headers".into()
                }),
            ));
        }
        let framing = http::request_framing(head).map_err(|e| (0, 0, e))?;
        if head
            .get("expect")
            .is_some_and(|v| v.eq_ignore_ascii_case("100-continue"))
        {
            let _ = w.write_all(b"HTTP/1.1 100 \r\n\r\n").await;
        }
        let (ur, mut uw) = upstream.into_split();
        let mut out = format!(
            "{} {} HTTP/1.1\r\nhost: {}\r\n",
            head.method, path, host_header
        );
        for (k, v) in &head.headers {
            if !HOP.contains(&k.to_ascii_lowercase().as_str()) {
                let _ = write!(out, "{}: {}\r\n", k.to_ascii_lowercase(), v);
            }
        }
        out.push_str("connection: close\r\n\r\n");
        uw.write_all(out.as_bytes())
            .await
            .map_err(|_| (0, 0, Error::Closed))?;
        let head_len = out.len() as u64;
        let body = http::copy_body(client, &mut uw, framing, idle)
            .await
            .map_err(|e| (head_len, 0, e))?;
        let up = head_len + body;
        let mut upr = BufReader::new(ur);
        loop {
            let raw = http::read_head(&mut upr, idle)
                .await
                .map_err(|_| (up, 0, Error::Closed))?;
            let text: String = raw.iter().map(|&b| char::from(b)).collect();
            let mut lines = text.trim_end_matches("\r\n").split("\r\n");
            let status_line = lines.next().unwrap_or("");
            let mut parts = status_line.splitn(3, ' ');
            let (_, code, reason) = (
                parts.next(),
                parts.next().unwrap_or(""),
                parts.next().unwrap_or(""),
            );
            let status: u16 = code.parse().map_err(|_| (up, 0, Error::Closed))?;
            let headers: Vec<(String, String)> = lines
                .filter_map(|l| {
                    l.split_once(':')
                        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
                })
                .collect();
            if (100..200).contains(&status) {
                continue; // 100 was answered here; nothing else asked to switch protocols
            }
            let mut resp = format!("HTTP/1.1 {status} {reason}\r\n");
            for (k, v) in &headers {
                if !["connection", "keep-alive", "proxy-connection"]
                    .contains(&k.to_ascii_lowercase().as_str())
                {
                    let _ = write!(resp, "{}: {}\r\n", k.to_ascii_lowercase(), v);
                }
            }
            resp.push_str("connection: close\r\n\r\n");
            w.write_all(resp.as_bytes())
                .await
                .map_err(|_| (up, 0, Error::Closed))?;
            let framing: Framing = http::response_framing(&head.method, status, &headers)
                .map_err(|_| (up, 0, Error::Closed))?;
            let down = http::copy_body(&mut upr, w, framing, idle)
                .await
                .map_err(|_| (up, 0, Error::Closed))?;
            let _ = w.flush().await;
            return Ok((up, down, Some(status)));
        }
    }
}
