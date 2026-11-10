//! `cairnward-audit-rs`: an independent check of Cairnward's audit log (ADR-0020, stage 4),
//! written apart from the Python writer so a mistake in one shows up as a disagreement.
//!
//! - `verify FILE` (JSON lines, as `cairnward audit log --json` or `audit.jsonl` write them):
//!   every record's hash recomputed from its fields, the chain from the genesis hash, and
//!   sequence numbers 1, 2, 3... Prints `OK <records> <head seq>:<head hash>` or the problems.
//! - `root FILE SIZE`: the RFC 9162 Merkle root over records 1..SIZE.
//! - `check-proof FILE`: a proof from `cairnward audit prove`: the record's hash and its
//!   inclusion in the checkpoint's root. The checkpoint's signature is checked separately, with
//!   `ssh-keygen -Y verify` and the signers file you trust (printed as a reminder).

mod canonical;
mod merkle;

use std::io::{BufRead, BufReader};
use std::process::ExitCode;

use serde_json::Value;
use sha2::{Digest, Sha256};

const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
const USAGE: &str = "usage: cairnward-audit-rs verify FILE | root FILE SIZE | check-proof FILE";

/// SHA-256 of the canonical JSON of the record without its own `hash`.
fn record_hash(record: &Value) -> Option<String> {
    let mut fields = record.as_object()?.clone();
    fields.remove("hash");
    let text = canonical::canonical(&Value::Object(fields));
    Some(merkle::to_hex(&Sha256::digest(text.as_bytes())))
}

fn read_records(path: &str) -> Result<Vec<Value>, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("{path}: {e}"))?;
    let mut out = Vec::new();
    for (i, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|e| e.to_string())?;
        if line.trim().is_empty() {
            continue;
        }
        out.push(
            serde_json::from_str(&line).map_err(|e| format!("line {}: not JSON ({e})", i + 1))?,
        );
    }
    Ok(out)
}

fn verify(path: &str) -> Result<bool, String> {
    let records = read_records(path)?;
    let mut problems = Vec::new();
    let (mut prev, mut expected) = (GENESIS.to_string(), 1u64);
    for r in &records {
        let seq = r.get("seq").and_then(Value::as_u64).unwrap_or(0);
        let hash = r.get("hash").and_then(Value::as_str).unwrap_or("");
        if seq != expected {
            problems.push(format!(
                "record {expected}: missing (next present is {seq})"
            ));
            expected = seq;
        }
        if r.get("prev_hash").and_then(Value::as_str) != Some(prev.as_str()) {
            problems.push(format!(
                "record {seq}: does not link to the previous record"
            ));
        }
        if record_hash(r).as_deref() != Some(hash) {
            problems.push(format!(
                "record {seq}: contents changed after it was written"
            ));
        }
        prev = hash.to_string();
        expected += 1;
    }
    if problems.is_empty() {
        let head = records
            .last()
            .and_then(|r| r.get("seq"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        println!("OK {} {head}:{prev}", records.len());
        return Ok(true);
    }
    for p in problems.iter().take(20) {
        println!("PROBLEM {p}");
    }
    Ok(false)
}

fn root(path: &str, size: &str) -> Result<bool, String> {
    let size: usize = size
        .parse()
        .map_err(|_| "SIZE must be a number".to_string())?;
    let records = read_records(path)?;
    if records.len() < size {
        return Err(format!(
            "the file has {} records, fewer than {size}",
            records.len()
        ));
    }
    let hashes = records[..size]
        .iter()
        .map(|r| merkle::parse_hash(r.get("hash").and_then(Value::as_str).unwrap_or("")))
        .collect::<Result<Vec<_>, _>>()?;
    println!("{}", merkle::to_hex(&merkle::root(&hashes)));
    Ok(true)
}

fn check_proof(path: &str) -> Result<bool, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
    let b: Value = serde_json::from_str(&text).map_err(|e| format!("not JSON: {e}"))?;
    if b.get("format").and_then(Value::as_str) != Some("cairnward-inclusion-proof-1") {
        return Err("not a Cairnward inclusion proof".into());
    }
    let record = b.get("record").ok_or("no record")?;
    let payload = b.pointer("/checkpoint/payload").ok_or("no checkpoint")?;
    let hash = record.get("hash").and_then(Value::as_str).unwrap_or("");
    let mut ok = true;
    if record_hash(record).as_deref() != Some(hash) {
        println!("PROBLEM the record's fields don't match its hash: it was edited");
        ok = false;
    }
    let index = b.get("index").and_then(Value::as_u64).ok_or("no index")?;
    let size = b.get("size").and_then(Value::as_u64).ok_or("no size")?;
    if record.get("seq").and_then(Value::as_u64) != Some(index + 1)
        || payload.get("seq").and_then(Value::as_u64) != Some(size)
    {
        println!("PROBLEM the proof's position or size doesn't match the record and checkpoint");
        ok = false;
    }
    let proof = b
        .get("proof")
        .and_then(Value::as_array)
        .ok_or("no proof")?
        .iter()
        .map(|h| merkle::parse_hash(h.as_str().unwrap_or("")))
        .collect::<Result<Vec<_>, _>>()?;
    let root = merkle::parse_hash(payload.get("root").and_then(Value::as_str).unwrap_or(""))?;
    if !merkle::verify_inclusion(&merkle::parse_hash(hash)?, index, size, &proof, &root) {
        println!("PROBLEM the record isn't in the checkpoint's Merkle tree");
        ok = false;
    }
    if ok {
        println!("OK record {} is in the checkpoint at seq {size}", index + 1);
        println!("note: check the checkpoint's signature with ssh-keygen -Y verify and the signers file you trust");
    }
    Ok(ok)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["verify", file] => verify(file),
        ["root", file, size] => root(file, size),
        ["check-proof", file] => check_proof(file),
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    match result {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(e) => {
            eprintln!("cairnward-audit-rs: {e}");
            ExitCode::from(2)
        }
    }
}
