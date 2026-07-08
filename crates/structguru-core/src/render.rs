//! Native render pipeline: assemble the event map, redact, and serialize.
//!
//! This reproduces the output contract of the Python structlog processor chain
//! for the common (non-exception) case: key-based redaction, canonical level,
//! syslog severity, and the `event -> message` rename, rendered as compact JSON.

use crate::value::Value;
use crate::{normalize_level, syslog_severity};

const REDACTED: &str = "[REDACTED]";

/// Default sensitive keys, mirroring `structguru.redaction.DEFAULT_SENSITIVE_KEYS`.
pub const DEFAULT_SENSITIVE_KEYS: &[&str] = &[
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session_id",
    "credit_card",
    "ssn",
    "private_key",
];

fn is_sensitive(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    DEFAULT_SENSITIVE_KEYS.iter().any(|candidate| *candidate == lower)
}

/// Recursively redact sensitive keys in place (key-based, matches the default
/// `RedactingProcessor`: no value-pattern matching).
fn redact(value: &mut Value) {
    match value {
        Value::Map(entries) => {
            for (key, child) in entries.iter_mut() {
                if is_sensitive(key) {
                    *child = Value::String(REDACTED.to_owned());
                } else {
                    redact(child);
                }
            }
        }
        Value::List(items) => {
            for item in items.iter_mut() {
                redact(item);
            }
        }
        Value::Null | Value::Bool(_) | Value::Int(_) | Value::Float(_) | Value::String(_) => {}
    }
}

/// Render a single log line as compact JSON.
///
/// `fields` are the user/bound/contextvars fields (already converted to
/// [`Value`]); they are redacted, then the standard fields are appended in the
/// order the Python pipeline emits: `logger`, `level`, `severity`, `timestamp`,
/// `service`, `message`.
pub fn render_line(
    fields: Vec<(String, Value)>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    timestamp: &str,
) -> Result<String, serde_json::Error> {
    let mut root = Value::Map(fields);
    redact(&mut root);
    let Value::Map(mut entries) = root else {
        unreachable!("root is constructed as a map");
    };

    let canonical = normalize_level(level);
    let severity = syslog_severity(&canonical);

    entries.push(("logger".to_owned(), Value::String(logger.to_owned())));
    entries.push(("level".to_owned(), Value::String(canonical)));
    entries.push(("severity".to_owned(), Value::Int(i64::from(severity))));
    entries.push(("timestamp".to_owned(), Value::String(timestamp.to_owned())));
    entries.push(("service".to_owned(), Value::String(service.to_owned())));
    entries.push(("message".to_owned(), Value::String(message.to_owned())));

    Value::Map(entries).to_json_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_fields_then_standard_keys_in_order() {
        let fields = vec![("request_id".to_owned(), Value::String("req-1".to_owned()))];
        let line = render_line(fields, "svc.mod", "warning", "checkout", "hello", "TS").unwrap();

        assert_eq!(
            line,
            r#"{"request_id":"req-1","logger":"svc.mod","level":"WARN","severity":4,"timestamp":"TS","service":"checkout","message":"hello"}"#,
        );
    }

    #[test]
    fn redacts_sensitive_keys_including_nested_and_case_insensitive() {
        let fields = vec![
            ("Password".to_owned(), Value::String("hunter2".to_owned())),
            (
                "ctx".to_owned(),
                Value::Map(vec![("api_key".to_owned(), Value::String("abc".to_owned()))]),
            ),
            ("qty".to_owned(), Value::Int(2)),
        ];
        let line = render_line(fields, "l", "info", "svc", "m", "TS").unwrap();

        assert!(line.contains(r#""Password":"[REDACTED]""#));
        assert!(line.contains(r#""api_key":"[REDACTED]""#));
        assert!(line.contains(r#""qty":2"#));
    }
}
