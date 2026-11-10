//! `cairnward-egress-rs`: stage 1 (ADR-0020) offers only `check-address`, which the Python tests
//! use to compare this port with `cairnward/egress/addresses.py`.

use std::io::{self, BufRead, Write};
use std::net::IpAddr;
use std::process::ExitCode;

use cairnward_egress::addresses::special_reason;

const USAGE: &str = "usage: cairnward-egress-rs check-address  (IP addresses on stdin, one per \
line; prints \"<ip>\\t<reason or ->\" for each, or \"<ip>\\tinvalid\")";

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

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [cmd] if cmd == "check-address" => match check_addresses() {
            Ok(()) => ExitCode::SUCCESS,
            Err(err) => {
                eprintln!("cairnward-egress-rs: {err}");
                ExitCode::FAILURE
            }
        },
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}
