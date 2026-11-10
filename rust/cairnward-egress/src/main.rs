//! `cairnward-egress-rs`
//!
//! - `serve --listen 127.0.0.1:PORT --decisions SOCKET [--connect-timeout S] [--idle-timeout S]
//!   [--max-connections N]`: the proxy (ADR-0020, stage 2). Prints `listening PORT` once bound.
//! - `check-address`: IP addresses on stdin, one per line; prints `<ip>\t<reason or ->` (stage 1,
//!   compared with the Python checks by `tests/test_egress_rust.py`).

use std::io::{self, BufRead, Write};
use std::net::IpAddr;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use cairnward_egress::addresses::special_reason;
use cairnward_egress::proxy::{Limits, Proxy};

const USAGE: &str = "usage: cairnward-egress-rs serve --listen ADDR --decisions SOCKET \
[--connect-timeout S] [--idle-timeout S] [--max-connections N]\n       cairnward-egress-rs check-address";

fn check_addresses() -> io::Result<()> {
    let stdin = io::stdin();
    let mut out = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        let verdict = match text.parse::<IpAddr>() {
            Ok(ip) => special_reason(ip).unwrap_or("-"),
            Err(_) => "invalid",
        };
        writeln!(out, "{text}\t{verdict}")?;
    }
    out.flush()
}

struct ServeArgs {
    listen: String,
    decisions: PathBuf,
    limits: Limits,
}

fn parse_serve(args: &[String]) -> Result<ServeArgs, String> {
    let mut listen = None;
    let mut decisions = None;
    let mut limits = Limits {
        connect_timeout: Duration::from_secs(15),
        idle_timeout: Duration::from_secs(300),
        max_connections: 256,
    };
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("{flag} needs a value"))?;
        let secs = || {
            value
                .parse::<f64>()
                .ok()
                .filter(|s| *s > 0.0 && s.is_finite())
                .map(Duration::from_secs_f64)
                .ok_or_else(|| format!("bad {flag}"))
        };
        match flag.as_str() {
            "--listen" => listen = Some(value.clone()),
            "--decisions" => decisions = Some(PathBuf::from(value)),
            "--connect-timeout" => limits.connect_timeout = secs()?,
            "--idle-timeout" => limits.idle_timeout = secs()?,
            "--max-connections" => {
                limits.max_connections = value
                    .parse()
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or("bad --max-connections")?;
            }
            _ => return Err(format!("unknown option {flag}")),
        }
    }
    Ok(ServeArgs {
        listen: listen.ok_or("--listen is required")?,
        decisions: decisions.ok_or("--decisions is required")?,
        limits,
    })
}

fn serve(args: ServeArgs) -> io::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        let listener = tokio::net::TcpListener::bind(&args.listen).await?;
        println!("listening {}", listener.local_addr()?.port());
        io::stdout().flush()?;
        let proxy = Arc::new(Proxy {
            decisions: args.decisions,
            limits: args.limits,
        });
        tokio::select! {
            r = proxy.serve(listener) => r,
            _ = tokio::signal::ctrl_c() => Ok(()),
        }
    })
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.split_first() {
        Some((cmd, rest)) if cmd == "check-address" && rest.is_empty() => check_addresses(),
        Some((cmd, rest)) if cmd == "serve" => match parse_serve(rest) {
            Ok(a) => serve(a),
            Err(e) => {
                eprintln!("cairnward-egress-rs: {e}\n{USAGE}");
                return ExitCode::from(2);
            }
        },
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("cairnward-egress-rs: {err}");
            ExitCode::FAILURE
        }
    }
}
