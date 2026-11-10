//! A client for `cairnward/egress/decisions.py`: one Unix-socket connection per proxied
//! connection, one JSON line each way per question (ADR-0020).

use std::path::Path;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

pub struct Decisions {
    reader: BufReader<tokio::net::unix::OwnedReadHalf>,
    writer: tokio::net::unix::OwnedWriteHalf,
    pub ticket: Option<String>,
}

#[derive(Debug)]
pub struct Connect {
    pub allow: bool,
    pub status: u16,
    pub reason: String,
    pub inspect: bool,
}

impl Decisions {
    pub async fn open(path: &Path) -> std::io::Result<Self> {
        let (r, w) = UnixStream::connect(path).await?.into_split();
        Ok(Self {
            reader: BufReader::new(r),
            writer: w,
            ticket: None,
        })
    }

    async fn ask(&mut self, req: Value) -> std::io::Result<Value> {
        let mut line = serde_json::to_vec(&req)?;
        line.push(b'\n');
        self.writer.write_all(&line).await?;
        let mut reply = String::new();
        if self.reader.read_line(&mut reply).await? == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "decision service closed",
            ));
        }
        let value: Value = serde_json::from_str(&reply)?;
        if value.get("ok") != Some(&Value::Bool(true)) {
            let why = value
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("no reason");
            return Err(std::io::Error::other(format!("decision service: {why}")));
        }
        Ok(value)
    }

    pub async fn session_valid(&mut self, authorization: Option<&str>) -> std::io::Result<bool> {
        let r = self
            .ask(json!({"op": "session", "authorization": authorization}))
            .await?;
        Ok(r.get("valid").and_then(Value::as_bool).unwrap_or(false))
    }

    pub async fn connect(
        &mut self,
        authorization: Option<&str>,
        method: &str,
        host: &str,
        port: u16,
    ) -> std::io::Result<Connect> {
        let r = self.ask(json!({"op": "connect", "authorization": authorization, "method": method, "host": host, "port": port})).await?;
        self.ticket = r.get("ticket").and_then(Value::as_str).map(str::to_string);
        Ok(Connect {
            allow: r.get("allow").and_then(Value::as_bool).unwrap_or(false),
            status: r
                .get("status")
                .and_then(Value::as_u64)
                .and_then(|s| u16::try_from(s).ok())
                .unwrap_or(403),
            reason: r
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            inspect: r.get("inspect").and_then(Value::as_bool).unwrap_or(false),
        })
    }

    fn ticket(&self) -> Value {
        self.ticket.clone().map_or(Value::Null, Value::String)
    }

    pub async fn allow_special(&mut self, address: &str) -> std::io::Result<bool> {
        let r = self
            .ask(json!({"op": "allow_special", "ticket": self.ticket(), "address": address}))
            .await?;
        Ok(r.get("allow").and_then(Value::as_bool).unwrap_or(false))
    }

    pub async fn deny(&mut self, reason: &str) -> std::io::Result<()> {
        self.ask(json!({"op": "deny", "ticket": self.ticket(), "reason": reason}))
            .await
            .map(|_| ())
    }

    pub async fn opened(&mut self) -> std::io::Result<()> {
        self.ask(json!({"op": "opened", "ticket": self.ticket()}))
            .await
            .map(|_| ())
    }

    pub async fn closed(&mut self, closed: Value) -> std::io::Result<()> {
        let mut req = closed;
        req["op"] = json!("closed");
        req["ticket"] = self.ticket();
        self.ask(req).await.map(|_| ())
    }
}
