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

fn is_sensitive(key: &str, keys: &[impl AsRef<str>]) -> bool {
    // Case-insensitive compare without allocating a lowercase copy per key.
    keys.iter()
        .any(|candidate| candidate.as_ref().eq_ignore_ascii_case(key))
}

/// Recursively redact sensitive keys in place (key-based, matches the default
/// `RedactingProcessor`: no value-pattern matching).
fn redact(value: &mut Value, keys: &[impl AsRef<str>]) {
    match value {
        Value::Map(entries) => {
            for (key, child) in entries.iter_mut() {
                if is_sensitive(key, keys) {
                    *child = Value::String(REDACTED.to_owned());
                } else {
                    redact(child, keys);
                }
            }
        }
        Value::List(items) => {
            for item in items.iter_mut() {
                redact(item, keys);
            }
        }
        _ => {}
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
    sensitive_keys: Option<Vec<String>>,
) -> Result<String, serde_json::Error> {
    // Redact against the caller's keys, or the static defaults with zero
    // per-record allocation (comparison is case-insensitive in `is_sensitive`).
    let mut root = Value::Map(fields);
    match &sensitive_keys {
        Some(custom) => redact(&mut root, custom),
        None => redact(&mut root, DEFAULT_SENSITIVE_KEYS),
    }
    let Value::Map(mut entries) = root else {
        unreachable!("root is constructed as a map");
    };

    let canonical = normalize_level(level);
    let severity = syslog_severity(&canonical);

    // Guard against duplicate JSON keys when a user field collides with a
    // standard key. logger/level/severity/timestamp/message are authoritative
    // (the structlog processors overwrite them), so drop any user field of the
    // same name. "service" uses setdefault semantics — a user-provided
    // "service" wins — matching `add_service`.
    const OVERRIDE_KEYS: [&str; 5] = ["logger", "level", "severity", "timestamp", "message"];
    let has_service = entries.iter().any(|(key, _)| key == "service");
    entries.retain(|(key, _)| !OVERRIDE_KEYS.contains(&key.as_str()));

    entries.push(("logger".to_owned(), Value::String(logger.to_owned())));
    entries.push(("level".to_owned(), Value::String(canonical)));
    entries.push(("severity".to_owned(), Value::Int(i64::from(severity))));
    entries.push(("timestamp".to_owned(), Value::String(timestamp.to_owned())));
    if !has_service {
        entries.push(("service".to_owned(), Value::String(service.to_owned())));
    }
    entries.push(("message".to_owned(), Value::String(message.to_owned())));

    Value::Map(entries).to_json_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_fields_then_standard_keys_in_order() {
        let fields = vec![("request_id".to_owned(), Value::String("req-1".to_owned()))];
        let line = render_line(
            fields, "svc.mod", "warning", "checkout", "hello", "TS", None,
        )
        .unwrap();

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
                Value::Map(vec![(
                    "api_key".to_owned(),
                    Value::String("abc".to_owned()),
                )]),
            ),
            ("qty".to_owned(), Value::Int(2)),
        ];
        let line = render_line(fields, "l", "info", "svc", "m", "TS", None).unwrap();

        assert!(line.contains(r#""Password":"[REDACTED]""#));
        assert!(line.contains(r#""api_key":"[REDACTED]""#));
        assert!(line.contains(r#""qty":2"#));
    }

    #[test]
    fn standard_keys_override_user_fields_except_service() {
        let fields = vec![
            ("level".to_owned(), Value::String("bogus".to_owned())),
            ("service".to_owned(), Value::String("user-svc".to_owned())),
            ("keep".to_owned(), Value::Int(1)),
        ];
        let line = render_line(fields, "l", "warning", "cfg-svc", "m", "TS", None).unwrap();

        // canonical level overrides the user field; user "service" wins (setdefault)
        assert!(line.contains(r#""level":"WARN""#));
        assert!(!line.contains("bogus"));
        assert!(line.contains(r#""service":"user-svc""#));
        assert!(!line.contains("cfg-svc"));
        assert!(line.contains(r#""keep":1"#));
        // each standard key appears exactly once (no duplicate JSON keys)
        assert_eq!(line.matches(r#""level":"#).count(), 1);
        assert_eq!(line.matches(r#""service":"#).count(), 1);
    }

    #[test]
    fn custom_sensitive_keys_replace_the_defaults() {
        let fields = vec![
            ("ssn".to_owned(), Value::String("123".to_owned())),
            ("secret_sauce".to_owned(), Value::String("x".to_owned())),
        ];
        let keys = Some(vec!["secret_sauce".to_owned()]);
        let line = render_line(fields, "l", "info", "svc", "m", "TS", keys).unwrap();

        // "ssn" is a default key but NOT in the custom set → not redacted.
        assert!(line.contains(r#""ssn":"123""#));
        assert!(line.contains(r#""secret_sauce":"[REDACTED]""#));
    }
}
