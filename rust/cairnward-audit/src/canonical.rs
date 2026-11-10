//! Python's `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
//! byte for byte, because the audit log hashes that text (`cairnward/audit/log.py`).
//!
//! The parts that differ from `serde_json`'s own output:
//! - **numbers:** a float is written as Python's `repr` writes it (`1e-05`, `1e+16`, `100.0`),
//!   integers as written;
//! - **strings:** non-ASCII stays as it is; `"`, `\` and control characters below 0x20 are
//!   escaped as Python escapes them (`\n`, `\r`, `\t`, `\b`, `\f`, else `\u00XX`);
//! - **keys:** sorted by code point (UTF-8 byte order is the same).

use std::fmt::Write as _;

use serde_json::{Number, Value};

pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => write_number(n, out),
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(k, out);
                out.push(':');
                write_value(&map[*k], out);
            }
            out.push('}');
        }
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_number(n: &Number, out: &mut String) {
    if n.is_i64() || n.is_u64() {
        out.push_str(&n.to_string());
    } else if let Some(f) = n.as_f64() {
        out.push_str(&py_float_repr(f));
    }
}

/// `repr(float)` in Python: the shortest digits that round-trip, written in fixed notation when
/// the decimal exponent is in -4..16, otherwise as `d.ddde±XX` (at least two exponent digits).
pub fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        };
    }
    if f == 0.0 {
        return if f.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    // Rust's `{:e}` gives the shortest round-trip digits too, as "d.ddde<exp>"
    let sci = format!("{f:e}");
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let point = exp + 1; // digits before the decimal point
        let body = if point <= 0 {
            format!(
                "0.{}{}",
                "0".repeat(usize::try_from(-point).unwrap_or(0)),
                digits
            )
        } else {
            let p = usize::try_from(point).unwrap_or(0);
            if digits.len() <= p {
                format!("{}{}.0", digits, "0".repeat(p - digits.len()))
            } else {
                format!("{}.{}", &digits[..p], &digits[p..])
            }
        };
        format!("{sign}{body}")
    } else {
        let (first, rest) = digits.split_at(1);
        let frac = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{first}{frac}e{esign}{:02}", exp.abs())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_as_python_writes_them() {
        for (f, py) in [
            (0.1, "0.1"),
            (100.0, "100.0"),
            (1e-5, "1e-05"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (123_456_789_012_345.6, "123456789012345.6"),
            (0.000_1, "0.0001"),
            (-2.5, "-2.5"),
            (1e22, "1e+22"),
            (5e-324, "5e-324"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0 / 3.0, "0.3333333333333333"),
        ] {
            assert_eq!(py_float_repr(f), py, "{f}");
        }
    }

    #[test]
    fn strings_and_keys_as_python_writes_them() {
        let v: Value = serde_json::from_str(r#"{"b":"é\n\u0001\"\\\u007f","a":[1,2.0,null,true]}"#)
            .unwrap_or(Value::Null);
        assert_eq!(
            canonical(&v),
            "{\"a\":[1,2.0,null,true],\"b\":\"é\\n\\u0001\\\"\\\\\u{7f}\"}"
        );
    }
}
