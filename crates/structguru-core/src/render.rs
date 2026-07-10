//! Native render pipeline: assemble the event map, redact, and serialize.
//!
//! This reproduces the output contract of the Python structlog processor chain
//! for the common (non-exception) case: key-based redaction, canonical level,
//! syslog severity, and the `event -> message` rename, rendered as compact JSON.

use crate::value::Value;
use crate::{normalize_level, syslog_severity};
use regex::Regex;

const REDACTED: &str = "[REDACTED]";

/// Internal marker set by `RedactingProcessor`, mirrored from
/// `structguru.redaction.REDACTED_MARKER_KEY`. `strip_redaction_marker`
/// removes it before rendering on the standard path; drop it here too so a
/// user field of the same name never reaches the output.
const REDACTED_MARKER_KEY: &str = "_structguru_redacted";

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

/// Recursively apply regex patterns to every string value in place.
///
/// Mirrors `RedactingProcessor(patterns=...)`: each compiled pattern is run via
/// `replace_all` against every `Value::String` leaf (in declaration order, so
/// later patterns see the output of earlier ones). Non-string leaves and
/// `Value::Raw` are not pattern-matched.
///
/// `Value::Raw` is deliberately left untouched: it holds pre-serialized JSON
/// for exotic Python leaves (datetime/UUID/Enum/...). Descending into it would
/// require re-parsing the JSON and would break the verbatim-output contract.
/// Key-based redaction still applies to a `Raw` value's *key* in its parent map.
fn redact_patterns(value: &mut Value, patterns: &[Regex]) {
    match value {
        Value::Map(entries) => {
            for (_, child) in entries.iter_mut() {
                redact_patterns(child, patterns);
            }
        }
        Value::List(items) => {
            for item in items.iter_mut() {
                redact_patterns(item, patterns);
            }
        }
        Value::String(s) if !patterns.is_empty() => {
            let mut current = std::mem::take(s);
            for re in patterns {
                current = re.replace_all(&current, REDACTED).into_owned();
            }
            *s = current;
        }
        _ => {}
    }
}

/// Replace the value of an existing `key` in place, or append it.
fn upsert(entries: &mut Vec<(String, Value)>, key: &str, value: Value) {
    match entries.iter_mut().find(|(k, _)| k == key) {
        Some((_, existing)) => *existing = value,
        None => entries.push((key.to_owned(), value)),
    }
}

/// Render a single log line as compact JSON.
///
/// `fields` are the user/bound/contextvars fields (already converted to
/// [`Value`]); they are redacted, then the standard fields are appended in the
/// order the Python pipeline emits: `logger`, `level`, `severity`, `timestamp`,
/// `service`, `message`.
#[allow(clippy::too_many_arguments)]
pub fn render_line(
    fields: Vec<(String, Value)>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    timestamp: &str,
    stack: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<&[Regex]>,
) -> Result<String, serde_json::Error> {
    // Redact against the caller's keys, or the static defaults with zero
    // per-record allocation (comparison is case-insensitive in `is_sensitive`).
    let mut root = Value::Map(fields);
    match &sensitive_keys {
        Some(custom) => redact(&mut root, custom),
        None => redact(&mut root, DEFAULT_SENSITIVE_KEYS),
    }
    // Then apply value-pattern redaction to any string leaves.
    if let Some(patterns) = sensitive_patterns {
        redact_patterns(&mut root, patterns);
    }
    let Value::Map(mut entries) = root else {
        unreachable!("root is constructed as a map");
    };

    // strip_redaction_marker semantics: the marker key never reaches the renderer.
    entries.retain(|(key, _)| key != REDACTED_MARKER_KEY);

    let canonical = normalize_level(level);
    let severity = syslog_severity(&canonical);

    // Standard keys mirror the structlog processors exactly. logger/level/
    // severity/timestamp/message are authoritative overrides; the processors
    // *assign* into the event dict, which replaces a colliding user field's
    // value at its original position — so upsert in place rather than
    // drop-and-append. "service" uses setdefault semantics — a user-provided
    // "service" wins — matching `add_service`.
    upsert(&mut entries, "logger", Value::String(logger.to_owned()));
    upsert(&mut entries, "level", Value::String(canonical));
    upsert(&mut entries, "severity", Value::Int(i64::from(severity)));
    upsert(&mut entries, "timestamp", Value::String(timestamp.to_owned()));
    if !entries.iter().any(|(key, _)| key == "service") {
        entries.push(("service".to_owned(), Value::String(service.to_owned())));
    }
    // "stack" sits between "service" and "message": StackInfoRenderer runs
    // after add_service and before EventRenamer in the shared chain.
    if let Some(stack) = stack {
        upsert(&mut entries, "stack", Value::String(stack.to_owned()));
    }
    upsert(&mut entries, "message", Value::String(message.to_owned()));

    Value::Map(entries).to_json_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_fields_then_standard_keys_in_order() {
        let fields = vec![("request_id".to_owned(), Value::String("req-1".to_owned()))];
        let line = render_line(
            fields, "svc.mod", "warning", "checkout", "hello", "TS", None, None, None,
        )
        .unwrap();

        assert_eq!(
            line,
            r#"{"request_id":"req-1","logger":"svc.mod","level":"WARN","severity":4,"timestamp":"TS","service":"checkout","message":"hello"}"#,
        );
    }

    #[test]
    fn redaction_marker_key_is_stripped() {
        let fields = vec![
            ("_structguru_redacted".to_owned(), Value::Bool(true)),
            ("keep".to_owned(), Value::Int(1)),
        ];
        let line = render_line(fields, "l", "info", "svc", "m", "TS", None, None, None).unwrap();

        assert!(!line.contains("_structguru_redacted"));
        assert!(line.contains(r#""keep":1"#));
    }

    #[test]
    fn stack_renders_between_service_and_message() {
        let fields = vec![("id".to_owned(), Value::Int(1))];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            Some("Stack (most recent call last):\n  frame"),
            None,
            None,
        )
        .unwrap();

        assert_eq!(
            line,
            r#"{"id":1,"logger":"l","level":"INFO","severity":6,"timestamp":"TS","service":"svc","stack":"Stack (most recent call last):\n  frame","message":"m"}"#,
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
        let line = render_line(fields, "l", "info", "svc", "m", "TS", None, None, None).unwrap();

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
        let line = render_line(fields, "l", "warning", "cfg-svc", "m", "TS", None, None, None).unwrap();

        // canonical level overrides the user field; user "service" wins (setdefault)
        assert!(line.contains(r#""level":"WARN""#));
        assert!(!line.contains("bogus"));
        assert!(line.contains(r#""service":"user-svc""#));
        assert!(!line.contains("cfg-svc"));
        assert!(line.contains(r#""keep":1"#));
        // each standard key appears exactly once (no duplicate JSON keys)
        assert_eq!(line.matches(r#""level":"#).count(), 1);
        assert_eq!(line.matches(r#""service":"#).count(), 1);
        // the override replaces the user field *in place* (structlog assigns
        // into the event dict, preserving the colliding key's position)
        assert!(line.starts_with(r#"{"level":"WARN","service":"user-svc","keep":1,"logger":"l""#));
    }

    #[test]
    fn custom_sensitive_keys_replace_the_defaults() {
        let fields = vec![
            ("ssn".to_owned(), Value::String("123".to_owned())),
            ("secret_sauce".to_owned(), Value::String("x".to_owned())),
        ];
        let keys = Some(vec!["secret_sauce".to_owned()]);
        let line = render_line(fields, "l", "info", "svc", "m", "TS", None, keys, None).unwrap();

        // "ssn" is a default key but NOT in the custom set → not redacted.
        assert!(line.contains(r#""ssn":"123""#));
        assert!(line.contains(r#""secret_sauce":"[REDACTED]""#));
    }

    #[test]
    fn pattern_redacts_matching_substring_in_string_value() {
        let email = Regex::new(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}").unwrap();
        let patterns = vec![email];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("Contact user@example.com for details".to_owned()),
        )];
        let line =
            render_line(fields, "l", "info", "svc", "m", "TS", None, None, Some(&patterns)).unwrap();

        assert!(line.contains(r#""msg":"Contact [REDACTED] for details""#));
        assert!(!line.contains("user@example.com"));
    }

    #[test]
    fn pattern_redaction_descends_into_nested_maps_and_lists() {
        let ssn = Regex::new(r"\b\d{3}-\d{2}-\d{4}\b").unwrap();
        let patterns = vec![ssn];
        let fields = vec![
            (
                "ctx".to_owned(),
                Value::Map(vec![(
                    "note".to_owned(),
                    Value::String("ssn is 123-45-6789".to_owned()),
                )]),
            ),
            (
                "tags".to_owned(),
                Value::List(vec![
                    Value::String("ok 111-22-3333".to_owned()),
                    Value::Int(7),
                ]),
            ),
        ];
        let line =
            render_line(fields, "l", "info", "svc", "m", "TS", None, None, Some(&patterns)).unwrap();

        assert!(line.contains(r#""note":"ssn is [REDACTED]""#));
        assert!(line.contains(r#""ok [REDACTED]""#));
        assert!(!line.contains("123-45-6789"));
        assert!(!line.contains("111-22-3333"));
    }

    #[test]
    fn pattern_redaction_skips_non_string_leaves_and_raw() {
        let any = Regex::new(r".+").unwrap();
        let patterns = vec![any];
        let fields = vec![
            ("count".to_owned(), Value::Int(42)),
            ("flag".to_owned(), Value::Bool(true)),
            ("ratio".to_owned(), Value::Float(1.5)),
            ("nothing".to_owned(), Value::Null),
            // Raw holds pre-serialized JSON; patterns must not descend into it.
            ("raw".to_owned(), Value::Raw(r#""escaped""#.to_owned())),
        ];
        let line =
            render_line(fields, "l", "info", "svc", "m", "TS", None, None, Some(&patterns)).unwrap();

        assert!(line.contains(r#""count":42"#));
        assert!(line.contains(r#""flag":true"#));
        assert!(line.contains(r#""ratio":1.5"#));
        assert!(line.contains(r#""nothing":null"#));
        assert!(line.contains(r#""raw":"escaped""#));
    }

    #[test]
    fn multiple_patterns_apply_in_order() {
        // Two patterns: first replaces emails, second replaces the word "secret".
        let patterns = vec![
            Regex::new(r"a@b\.com").unwrap(),
            Regex::new(r"secret").unwrap(),
        ];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("secret email a@b.com here".to_owned()),
        )];
        let line =
            render_line(fields, "l", "info", "svc", "m", "TS", None, None, Some(&patterns)).unwrap();

        assert!(line.contains(r#""msg":"[REDACTED] email [REDACTED] here""#));
    }

    #[test]
    fn key_redaction_and_pattern_redaction_combine() {
        // Key-based redaction masks "token"; pattern-based masks the email in "msg".
        let patterns = vec![Regex::new(r"leak@x\.io").unwrap()];
        let fields = vec![
            ("token".to_owned(), Value::String("abc".to_owned())),
            (
                "msg".to_owned(),
                Value::String("ping leak@x.io now".to_owned()),
            ),
        ];
        let line =
            render_line(fields, "l", "info", "svc", "m", "TS", None, None, Some(&patterns)).unwrap();

        assert!(line.contains(r#""token":"[REDACTED]""#));
        assert!(line.contains(r#""msg":"ping [REDACTED] now""#));
        assert!(!line.contains("leak@x.io"));
    }
}
