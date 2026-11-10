//! TLS inspection in the Rust data path (ADR-0020, stage 3), following `egress/inspect.py`'s
//! `Inspector`: end the tool's TLS with a certificate from Cairnward's local CA, open a verified
//! TLS connection to the host, and pass HTTP/1.1 through one request at a time. Every request
//! is judged by the decision service (method and path rules, a tunnel inside the tunnel, a
//! request naming another site) and recorded there, by digest. A refused request gets 403 and
//! the connection ends. After `101 Switching Protocols` the rest is piped unread.

use std::collections::HashMap;
use std::net::IpAddr;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};
use tokio::io::{
    AsyncBufRead, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader, ReadBuf,
};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tokio_rustls::{TlsAcceptor, TlsConnector};

use crate::decisions::Decisions;
use crate::http::{self, Error, Framing};

/// The client connection's two halves as one stream, so TLS can run over it. Reads go through
/// the `BufReader`, so nothing it already holds is lost.
pub struct Joined<'a, R, W> {
    pub r: &'a mut R,
    pub w: &'a mut W,
}

impl<R: AsyncRead + Unpin, W: Unpin> AsyncRead for Joined<'_, R, W> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.r).poll_read(cx, buf)
    }
}

impl<R: Unpin, W: AsyncWrite + Unpin> AsyncWrite for Joined<'_, R, W> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut *self.w).poll_write(cx, buf)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.w).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut *self.w).poll_shutdown(cx)
    }
}

/// Server configurations per host (one certificate each from the decision service), and the
/// client configuration for the real sites: Mozilla's roots (as certifi), TLS 1.2 or later,
/// ALPN http/1.1 only.
pub struct Tls {
    servers: Mutex<HashMap<String, (Arc<rustls::ServerConfig>, Instant)>>,
    client: Arc<rustls::ClientConfig>,
}

const CACHE_FOR: Duration = Duration::from_secs(24 * 3600);
const MAX_CACHED: usize = 256;

fn client_config(roots: rustls::RootCertStore) -> Result<Arc<rustls::ClientConfig>, String> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut client = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots)
        .with_no_client_auth();
    client.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(Arc::new(client))
}

impl Tls {
    /// The public roots (Mozilla's, as certifi).
    pub fn new() -> Result<Self, String> {
        let roots = rustls::RootCertStore {
            roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
        };
        Ok(Self {
            servers: Mutex::new(HashMap::new()),
            client: client_config(roots)?,
        })
    }

    /// For tests: trust these roots (PEM) instead of the public ones.
    pub fn with_roots(pem: &[u8]) -> Result<Self, String> {
        let mut roots = rustls::RootCertStore::empty();
        for cert in CertificateDer::pem_slice_iter(pem) {
            roots
                .add(cert.map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
        }
        Ok(Self {
            servers: Mutex::new(HashMap::new()),
            client: client_config(roots)?,
        })
    }

    async fn server_for(
        &self,
        host: &str,
        dec: &mut Decisions,
    ) -> Result<Arc<rustls::ServerConfig>, String> {
        if let Ok(cache) = self.servers.lock() {
            if let Some((cfg, made)) = cache.get(host) {
                if made.elapsed() < CACHE_FOR {
                    return Ok(Arc::clone(cfg));
                }
            }
        }
        let (chain, key) = dec.certificate().await.map_err(|e| e.to_string())?;
        let certs: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(chain.as_bytes())
            .collect::<Result<_, _>>()
            .map_err(|e| e.to_string())?;
        let key = PrivateKeyDer::from_pem_slice(key.as_bytes()).map_err(|e| e.to_string())?;
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut cfg = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|e| e.to_string())?
            .with_no_client_auth()
            .with_single_cert(certs, key)
            .map_err(|e| e.to_string())?;
        cfg.alpn_protocols = vec![b"http/1.1".to_vec()]; // one request at a time; no HTTP/2
        let cfg = Arc::new(cfg);
        if let Ok(mut cache) = self.servers.lock() {
            if cache.len() >= MAX_CACHED {
                cache.clear();
            }
            cache.insert(host.to_string(), (Arc::clone(&cfg), Instant::now()));
        }
        Ok(cfg)
    }
}

/// What happened, for the connection's `closed` record.
pub struct Outcome {
    pub bytes_up: u64,
    pub bytes_down: u64,
    pub error: Option<String>,
}

fn server_name(host: &str) -> Result<ServerName<'static>, String> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(ServerName::IpAddress(ip.into()));
    }
    ServerName::try_from(host.to_string()).map_err(|e| e.to_string())
}

fn verify_message(err: &std::io::Error) -> Option<String> {
    let inner = err.get_ref()?.downcast_ref::<rustls::Error>()?;
    match inner {
        rustls::Error::InvalidCertificate(why) => Some(format!("{why:?}")),
        _ => None,
    }
}

/// The tunnel after `200 Connection established`, inspected.
#[allow(clippy::too_many_arguments)]
pub async fn tunnel<R, W>(
    tls: &Tls,
    dec: &mut Decisions,
    host: &str,
    client_r: &mut R,
    client_w: &mut W,
    upstream: TcpStream,
    handshake: Duration,
    idle: Duration,
) -> Outcome
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let fail = |e: String| Outcome {
        bytes_up: 0,
        bytes_down: 0,
        error: Some(e),
    };
    let server = match tls.server_for(host, dec).await {
        Ok(s) => s,
        Err(e) => return fail(format!("inspection unavailable: {e}")),
    };
    let joined = Joined {
        r: client_r,
        w: client_w,
    };
    let Ok(Ok(mut client)) = timeout(handshake, TlsAcceptor::from(server).accept(joined)).await
    else {
        return fail("the tool refused Cairnward's certificate (TLSError): it doesn't trust the local CA, or pins certificates (add it to skip_hosts)".into());
    };
    let name = match server_name(host) {
        Ok(n) => n,
        Err(e) => return fail(format!("upstream TLS failed: {e}")),
    };
    let up = match timeout(
        handshake,
        TlsConnector::from(Arc::clone(&tls.client)).connect(name, upstream),
    )
    .await
    {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => {
            let (body, error) = match verify_message(&e) {
                Some(m) => (
                    format!("cairnward-egress: {host}'s certificate failed verification: {m}\n"),
                    format!("upstream certificate failed verification: {m}"),
                ),
                None => (
                    format!("cairnward-egress: TLS to {host} failed\n"),
                    "upstream TLS failed: TLSError".to_string(),
                ),
            };
            let reply = format!("HTTP/1.1 502 Bad Gateway\r\nContent-Length: {}\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n{body}", body.len());
            let _ = client.write_all(reply.as_bytes()).await;
            let _ = client.shutdown().await;
            return fail(error);
        }
        Err(_) => return fail("upstream TLS failed: TimeoutError".into()),
    };
    let (cr, mut cw) = tokio::io::split(client);
    let (ur, mut uw) = tokio::io::split(up);
    let mut cr = BufReader::new(cr);
    let mut ur = BufReader::new(ur);
    let mut out = Outcome {
        bytes_up: 0,
        bytes_down: 0,
        error: None,
    };
    exchanges(
        dec, &mut cr, &mut cw, &mut ur, &mut uw, idle, &mut out, true,
    )
    .await;
    let _ = cw.shutdown().await;
    out
}

fn refusal(reason: &str, method: &str) -> Vec<u8> {
    let body = format!("cairnward-egress: {method} denied: {reason}\n");
    format!("HTTP/1.1 403 \r\ncontent-type: text/plain; charset=utf-8\r\ncontent-length: {}\r\nx-cairnward-decision: deny\r\nconnection: close\r\n\r\n{body}", body.len()).into_bytes()
}

fn closes(headers: &[(String, String)]) -> bool {
    headers.iter().any(|(k, v)| {
        k.eq_ignore_ascii_case("connection")
            && v.split(',').any(|t| t.trim().eq_ignore_ascii_case("close"))
    })
}

/// Requests and responses, one at a time, until either side closes or a request is refused.
#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
async fn exchanges<CR, CW, UR, UW>(
    dec: &mut Decisions,
    cr: &mut CR,
    cw: &mut CW,
    ur: &mut UR,
    uw: &mut UW,
    idle: Duration,
    out: &mut Outcome,
    fronting: bool,
) where
    CR: AsyncBufRead + Unpin,
    CW: AsyncWrite + Unpin,
    UR: AsyncBufRead + Unpin,
    UW: AsyncWrite + Unpin,
{
    loop {
        let Ok(raw) = http::read_head(cr, idle).await else {
            return;
        };
        let Ok(head) = http::parse_head(&raw) else {
            return;
        }; // as h11: a protocol error ends it
        let hosts = if fronting {
            head.all("host")
        } else {
            Vec::new()
        };
        let Ok((allow, reason, rid)) = dec.request(&head.method, &head.target, &hosts).await else {
            return;
        };
        if !allow {
            if let Ok(f) = http::request_framing(&head) {
                let _ = http::copy_body(cr, &mut tokio::io::sink(), f, idle).await;
            }
            let _ = cw.write_all(&refusal(&reason, &head.method)).await;
            let _ = cw.flush().await;
            let _ = dec.request_done(&rid, Some(403), 0, 0, None).await;
            return; // we said Connection: close
        }
        let (mut up, mut down, mut status, mut switched) = (0u64, 0u64, None::<u16>, false);
        let result: Result<(), Error> = async {
            let framing = http::request_framing(&head)?;
            if head
                .get("expect")
                .is_some_and(|v| v.eq_ignore_ascii_case("100-continue"))
            {
                cw.write_all(b"HTTP/1.1 100 \r\n\r\n").await?; // answered here; Expect not passed on
            }
            let mut req = format!("{} {} {}\r\n", head.method, head.target, head.version);
            for (k, v) in &head.headers {
                if !k.eq_ignore_ascii_case("expect") {
                    req.push_str(k);
                    req.push_str(": ");
                    req.push_str(v);
                    req.push_str("\r\n");
                }
            }
            req.push_str("\r\n");
            uw.write_all(req.as_bytes()).await?;
            up = http::copy_body(cr, uw, framing, idle).await?;
            uw.flush().await?;
            loop {
                let raw = http::read_head(ur, idle).await?;
                let text: String = raw.iter().map(|&b| char::from(b)).collect();
                let mut lines = text.trim_end_matches("\r\n").split("\r\n");
                let code: u16 = lines
                    .next()
                    .unwrap_or("")
                    .split(' ')
                    .nth(1)
                    .and_then(|c| c.parse().ok())
                    .ok_or(Error::Closed)?;
                let headers: Vec<(String, String)> = lines
                    .filter_map(|l| {
                        l.split_once(':')
                            .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
                    })
                    .collect();
                if code == 100 {
                    continue; // we sent our own
                }
                cw.write_all(&raw).await?;
                if code == 101 {
                    status = Some(101);
                    switched = true;
                    return Ok(());
                }
                if (100..200).contains(&code) {
                    continue;
                }
                status = Some(code);
                let f = http::response_framing(&head.method, code, &headers)?;
                down = http::copy_body(ur, cw, f, idle).await?;
                cw.flush().await?;
                let ends = closes(&head.headers)
                    || closes(&headers)
                    || f == Framing::UntilClose
                    || head.version == "HTTP/1.0";
                if ends {
                    return Err(Error::Bad(String::new())); // a clean end, not an error
                }
                return Ok(());
            }
        }
        .await;
        out.bytes_up += up;
        out.bytes_down += down;
        let (error, end) = match &result {
            Ok(()) => (None, false),
            Err(Error::Bad(m)) if m.is_empty() => (None, true),
            Err(Error::Bad(_)) => (Some("RemoteProtocolError"), true),
            Err(Error::Closed) => (Some("ConnectionError"), true),
        };
        let _ = dec.request_done(&rid, status, up, down, error).await;
        if error.is_some() {
            out.error = error.map(str::to_string);
        }
        if switched {
            pipe(cr, cw, ur, uw, idle, out).await;
            return;
        }
        if end {
            return;
        }
    }
}

/// After a 101: what's left, unread, both ways (buffered bytes first).
async fn pipe<CR, CW, UR, UW>(
    cr: &mut CR,
    cw: &mut CW,
    ur: &mut UR,
    uw: &mut UW,
    idle: Duration,
    out: &mut Outcome,
) where
    CR: AsyncRead + Unpin,
    CW: AsyncWrite + Unpin,
    UR: AsyncRead + Unpin,
    UW: AsyncWrite + Unpin,
{
    async fn copy<A: AsyncRead + Unpin, B: AsyncWrite + Unpin>(
        a: &mut A,
        b: &mut B,
        idle: Duration,
    ) -> u64 {
        let mut n = 0u64;
        let mut buf = vec![0u8; 65536];
        while let Ok(Ok(k)) = timeout(idle, a.read(&mut buf)).await {
            if k == 0 || b.write_all(&buf[..k]).await.is_err() || b.flush().await.is_err() {
                break;
            }
            n += k as u64;
        }
        let _ = b.shutdown().await;
        n
    }
    let (u, d) = tokio::join!(copy(cr, uw, idle), copy(ur, cw, idle));
    out.bytes_up += u;
    out.bytes_down += d;
}

/// Plain HTTP in an inspecting environment: the same rules, before anything is sent
/// (`EgressProxy._handle_one`). Returns the refusal reason, or None to go ahead.
pub async fn check_plain(
    dec: &mut Decisions,
    method: &str,
    path: &str,
) -> Result<Option<String>, std::io::Error> {
    let (allow, reason, rid) = dec.request(method, path, &[]).await?;
    dec.request_done(&rid, if allow { None } else { Some(403) }, 0, 0, None)
        .await?;
    Ok(if allow { None } else { Some(reason) })
}
