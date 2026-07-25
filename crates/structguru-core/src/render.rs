//! Native render pipeline: assemble the event map, redact, and serialize.
//!
//! This reproduces the output contract of the Python structlog processor chain
//! for the common (non-exception) case: key-based redaction, canonical level,
//! syslog severity, and the `event -> message` rename, rendered as compact JSON.

use crate::value::Value;
use crate::{normalize_level, syslog_severity};
use regex::Regex;

const REDACTED: &str = "[REDACTED]";

/// Maximum backtracking steps for an opt-in backtracking pattern before the
/// engine gives up on the current string. Exceeding it fails closed (see
/// [`RedactionPattern::replace_all`]), so a pathological value cannot hang the
/// render path. Mirrors fancy-regex's own default.
const BACKTRACK_LIMIT: usize = 1_000_000;

/// A compiled value-redaction pattern.
///
/// `Linear` wraps the default `regex` engine, which guarantees linear-time
/// matching (no ReDoS) but rejects look-around and backreferences.
/// `Backtracking` wraps `fancy-regex` for those constructs; it is only
/// constructed when the caller explicitly opts in
/// (`allow_backtracking_patterns=True` in Python) and carries
/// [`BACKTRACK_LIMIT`] so evaluation is bounded.
pub enum RedactionPattern {
    Linear(Regex),
    Backtracking(Box<fancy_regex::Regex>),
}

impl RedactionPattern {
    /// Compile with the linear-time engine only.
    pub fn linear(pattern: &str) -> Result<Self, String> {
        Regex::new(pattern)
            .map(Self::Linear)
            .map_err(|err| err.to_string())
    }

    /// Compile with the linear-time engine, falling back to the backtracking
    /// engine (look-around, backreferences) when `allow_backtracking` is set.
    pub fn compile(pattern: &str, allow_backtracking: bool) -> Result<Self, String> {
        match Regex::new(pattern) {
            Ok(re) => Ok(Self::Linear(re)),
            Err(_) if allow_backtracking => Self::backtracking_with_limit(pattern, BACKTRACK_LIMIT),
            Err(err) => Err(err.to_string()),
        }
    }

    fn backtracking_with_limit(pattern: &str, limit: usize) -> Result<Self, String> {
        fancy_regex::RegexBuilder::new(pattern)
            .backtrack_limit(limit)
            .build()
            .map(|re| Self::Backtracking(Box::new(re)))
            .map_err(|err| err.to_string())
    }

    /// `replace_all` with the regex crate's group expansion in `replacement`
    /// (`$1`, `${name}`; `$$` for a literal `$`).
    ///
    /// A backtracking pattern whose evaluation errors at match time (backtrack
    /// limit exceeded) fails CLOSED: the entire string becomes `[REDACTED]`.
    /// Emitting a value the configured pattern could not check risks leaking
    /// exactly the data redaction exists to mask.
    fn replace_all(&self, text: &str, replacement: &str) -> String {
        match self {
            Self::Linear(re) => re.replace_all(text, replacement).into_owned(),
            Self::Backtracking(re) => replace_all_backtracking(re, text, replacement)
                .unwrap_or_else(|| REDACTED.to_owned()),
        }
    }
}

/// fancy-regex's `replace_all` panics on evaluation errors; iterate matches
/// manually so a backtrack-limit error surfaces as `None` instead.
fn replace_all_backtracking(
    re: &fancy_regex::Regex,
    text: &str,
    replacement: &str,
) -> Option<String> {
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for caps in re.captures_iter(text) {
        let caps = caps.ok()?;
        let matched = caps.get(0).expect("capture group 0 is the whole match");
        out.push_str(&text[last..matched.start()]);
        fancy_regex::Expander::default().append_expansion(&mut out, replacement, &caps);
        last = matched.end();
    }
    out.push_str(&text[last..]);
    Some(out)
}

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
///
/// `replacement` supports the regex crate's group expansion (`$1`, `${name}`;
/// `$$` for a literal `$`), so look-behind-style patterns can be rewritten as
/// capture groups that preserve their prefix, e.g. pattern `password=(\S+)`
/// with replacement `password=[REDACTED]`.
fn redact_patterns(value: &mut Value, patterns: &[RedactionPattern], replacement: &str) {
    match value {
        Value::Map(entries) => {
            for (_, child) in entries.iter_mut() {
                redact_patterns(child, patterns, replacement);
            }
        }
        Value::List(items) => {
            for item in items.iter_mut() {
                redact_patterns(item, patterns, replacement);
            }
        }
        Value::String(s) if !patterns.is_empty() => {
            let mut current = std::mem::take(s);
            for re in patterns {
                current = re.replace_all(&current, replacement);
            }
            *s = current;
        }
        _ => {}
    }
}

fn redact_message(
    message: &str,
    sensitive_keys: Option<&Vec<String>>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
) -> String {
    let message_is_sensitive = match sensitive_keys {
        Some(custom) => is_sensitive("message", custom),
        None => is_sensitive("message", DEFAULT_SENSITIVE_KEYS),
    };
    if message_is_sensitive {
        return REDACTED.to_owned();
    }

    let mut value = Value::String(message.to_owned());
    if let Some(patterns) = sensitive_patterns {
        redact_patterns(
            &mut value,
            patterns,
            pattern_replacement.unwrap_or(REDACTED),
        );
    }
    let Value::String(redacted) = value else {
        unreachable!("message redaction preserves the string value variant");
    };
    redacted
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
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
) -> Result<String, serde_json::Error> {
    let redacted_message = redact_message(
        message,
        sensitive_keys.as_ref(),
        sensitive_patterns,
        pattern_replacement,
    );
    // Redact against the caller's keys, or the static defaults with zero
    // per-record allocation (comparison is case-insensitive in `is_sensitive`).
    let mut root = Value::Map(fields);
    match &sensitive_keys {
        Some(custom) => redact(&mut root, custom),
        None => redact(&mut root, DEFAULT_SENSITIVE_KEYS),
    }
    // Then apply value-pattern redaction to any string leaves.
    if let Some(patterns) = sensitive_patterns {
        redact_patterns(&mut root, patterns, pattern_replacement.unwrap_or(REDACTED));
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
    upsert(
        &mut entries,
        "timestamp",
        Value::String(timestamp.to_owned()),
    );
    if !entries.iter().any(|(key, _)| key == "service") {
        entries.push(("service".to_owned(), Value::String(service.to_owned())));
    }
    // "stack" sits between "service" and "message": StackInfoRenderer runs
    // after add_service and before EventRenamer in the shared chain.
    if let Some(stack) = stack {
        upsert(&mut entries, "stack", Value::String(stack.to_owned()));
    }
    upsert(&mut entries, "message", Value::String(redacted_message));

    Value::Map(entries).to_json_string()
}

/// ANSI color codes for console rendering (applied only when `colors` is true).
const ANSI_DEBUG: &str = "\x1b[2m"; // dim
const ANSI_YELLOW: &str = "\x1b[33m";
const ANSI_RED: &str = "\x1b[31m";
const ANSI_BOLD_RED: &str = "\x1b[1;31m";
const ANSI_RESET: &str = "\x1b[0m";

/// Append `s` with control characters escaped.
///
/// The JSON renderer gets this from serde_json; the human-readable console path
/// does not, so without it a request-controlled field value or message could
/// embed a newline (forging a separate log line) or an ANSI escape sequence
/// (manipulating the operator's terminal). Escaping neutralizes both while
/// keeping ordinary text readable.
fn push_escaped(buf: &mut String, s: &str) {
    use std::fmt::Write;
    for ch in s.chars() {
        match ch {
            '\n' => buf.push_str("\\n"),
            '\r' => buf.push_str("\\r"),
            '\t' => buf.push_str("\\t"),
            c if c.is_control() => {
                let _ = write!(buf, "\\x{:02x}", c as u32);
            }
            c => buf.push(c),
        }
    }
}

/// Format a [`Value`] for the console renderer (human-readable, not JSON).
fn display_value(value: &Value, buf: &mut String) {
    match value {
        Value::Null => buf.push_str("None"),
        Value::Bool(b) => buf.push_str(if *b { "True" } else { "False" }),
        Value::Int(i) => buf.push_str(&i.to_string()),
        Value::Float(f) => buf.push_str(&f.to_string()),
        Value::String(s) => {
            buf.push('"');
            push_escaped(buf, s);
            buf.push('"');
        }
        Value::List(items) => {
            buf.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    buf.push_str(", ");
                }
                display_value(item, buf);
            }
            buf.push(']');
        }
        Value::Map(entries) => {
            buf.push('{');
            for (i, (k, v)) in entries.iter().enumerate() {
                if i > 0 {
                    buf.push_str(", ");
                }
                push_escaped(buf, k);
                buf.push(':');
                display_value(v, buf);
            }
            buf.push('}');
        }
        Value::Raw(json) => buf.push_str(json), // already-serialized; emit verbatim
    }
}

/// Render a single log line as a colored, human-readable console string.
///
/// Format: ``<timestamp> [<LEVEL>] <message>  k1=v1 k2=v2``
///
/// This is structguru's own stable dev format, not a structlog
/// ``ConsoleRenderer`` clone. Fields are rendered as ``k=v`` pairs after the
/// message. The same redaction pipeline as [`render_line`] is applied first.
#[allow(clippy::too_many_arguments, dead_code)]
pub fn render_line_console(
    fields: Vec<(String, Value)>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    colors: bool,
    timestamp: &str,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
    stack: Option<&str>,
) -> String {
    let redacted_message = redact_message(
        message,
        sensitive_keys.as_ref(),
        sensitive_patterns,
        pattern_replacement,
    );
    let mut root = Value::Map(fields);
    match &sensitive_keys {
        Some(custom) => redact(&mut root, custom),
        None => redact(&mut root, DEFAULT_SENSITIVE_KEYS),
    }
    if let Some(patterns) = sensitive_patterns {
        redact_patterns(&mut root, patterns, pattern_replacement.unwrap_or(REDACTED));
    }
    let Value::Map(mut entries) = root else {
        unreachable!("root is constructed as a map");
    };
    entries.retain(|(key, _)| key != REDACTED_MARKER_KEY);

    let canonical = normalize_level(level);
    let (level_pad, color) = level_style(&canonical, colors);

    let mut out = String::with_capacity(128);
    out.push_str(timestamp);
    out.push(' ');
    if colors {
        out.push_str(color);
    }
    out.push('[');
    out.push_str(&level_pad);
    out.push(']');
    if colors {
        out.push_str(ANSI_RESET);
    }
    out.push(' ');
    push_escaped(&mut out, &redacted_message);

    // Append user fields as k=v pairs (skip standard keys).
    let standard: &[&str] = &[
        "logger",
        "level",
        "severity",
        "timestamp",
        "service",
        "message",
        "stack",
    ];
    for (key, value) in &entries {
        if standard.contains(&key.as_str()) {
            continue;
        }
        out.push_str("  ");
        push_escaped(&mut out, key);
        out.push('=');
        display_value(value, &mut out);
    }
    // Suppress unused-variable lint for logger/service (kept in signature for API symmetry).
    let _ = (logger, service);

    if let Some(stack) = stack {
        out.push('\n');
        out.push_str(stack);
    }
    out
}

fn level_style(canonical_level: &str, colors: bool) -> (String, &'static str) {
    // Right-pad level to 8 chars inside the brackets: [INFO    ], [DEBUG   ], [CRITICAL].
    let pad = format!("{:<8}", canonical_level);
    let color = if !colors {
        ""
    } else {
        match canonical_level {
            "DEBUG" => ANSI_DEBUG,
            "WARN" => ANSI_YELLOW,
            "ERROR" => ANSI_RED,
            "CRITICAL" => ANSI_BOLD_RED,
            _ => "",
        }
    };
    (pad, color)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_fields_then_standard_keys_in_order() {
        let fields = vec![("request_id".to_owned(), Value::String("req-1".to_owned()))];
        let line = render_line(
            fields, "svc.mod", "warning", "checkout", "hello", "TS", None, None, None, None,
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
        let line = render_line(
            fields, "l", "info", "svc", "m", "TS", None, None, None, None,
        )
        .unwrap();

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
        let line = render_line(
            fields, "l", "info", "svc", "m", "TS", None, None, None, None,
        )
        .unwrap();

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
        let line = render_line(
            fields, "l", "warning", "cfg-svc", "m", "TS", None, None, None, None,
        )
        .unwrap();

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
        let line = render_line(
            fields, "l", "info", "svc", "m", "TS", None, keys, None, None,
        )
        .unwrap();

        // "ssn" is a default key but NOT in the custom set → not redacted.
        assert!(line.contains(r#""ssn":"123""#));
        assert!(line.contains(r#""secret_sauce":"[REDACTED]""#));
    }

    #[test]
    fn pattern_redacts_matching_substring_in_string_value() {
        let email =
            RedactionPattern::linear(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}").unwrap();
        let patterns = vec![email];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("Contact user@example.com for details".to_owned()),
        )];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""msg":"Contact [REDACTED] for details""#));
        assert!(!line.contains("user@example.com"));
    }

    #[test]
    fn pattern_redacts_matching_substring_in_message() {
        let patterns = vec![RedactionPattern::linear(r"secret=\w+").unwrap()];
        let line = render_line(
            vec![],
            "l",
            "info",
            "svc",
            "token secret=abc",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""message":"token [REDACTED]""#));
        assert!(!line.contains("secret=abc"));
    }

    #[test]
    fn custom_message_key_redacts_entire_message() {
        let line = render_line(
            vec![],
            "l",
            "info",
            "svc",
            "top secret",
            "TS",
            None,
            Some(vec!["message".to_owned()]),
            None,
            None,
        )
        .unwrap();

        assert!(line.contains(r#""message":"[REDACTED]""#));
        assert!(!line.contains("top secret"));
    }

    #[test]
    fn pattern_redaction_descends_into_nested_maps_and_lists() {
        let ssn = RedactionPattern::linear(r"\b\d{3}-\d{2}-\d{4}\b").unwrap();
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
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""note":"ssn is [REDACTED]""#));
        assert!(line.contains(r#""ok [REDACTED]""#));
        assert!(!line.contains("123-45-6789"));
        assert!(!line.contains("111-22-3333"));
    }

    #[test]
    fn pattern_redaction_skips_non_string_leaves_and_raw() {
        let any = RedactionPattern::linear(r".+").unwrap();
        let patterns = vec![any];
        let fields = vec![
            ("count".to_owned(), Value::Int(42)),
            ("flag".to_owned(), Value::Bool(true)),
            ("ratio".to_owned(), Value::Float(1.5)),
            ("nothing".to_owned(), Value::Null),
            // Raw holds pre-serialized JSON; patterns must not descend into it.
            ("raw".to_owned(), Value::Raw(r#""escaped""#.to_owned())),
        ];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

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
            RedactionPattern::linear(r"a@b\.com").unwrap(),
            RedactionPattern::linear(r"secret").unwrap(),
        ];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("secret email a@b.com here".to_owned()),
        )];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""msg":"[REDACTED] email [REDACTED] here""#));
    }

    #[test]
    fn pattern_replacement_expands_capture_groups() {
        // Lookbehind rewrite: `(?<=password=)\S+` becomes `password=(\S+)` with
        // a replacement that re-emits the prefix.
        let patterns = vec![RedactionPattern::linear(r"(password=)\S+").unwrap()];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("login with password=hunter2 ok".to_owned()),
        )];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            Some("$1[REDACTED]"),
        )
        .unwrap();

        assert!(line.contains(r#""msg":"login with password=[REDACTED] ok""#));
        assert!(!line.contains("hunter2"));
    }

    #[test]
    fn compile_rejects_lookbehind_without_opt_in() {
        assert!(RedactionPattern::compile(r"(?<=password=)\S+", false).is_err());
        assert!(RedactionPattern::compile(r"(?<=password=)\S+", true).is_ok());
    }

    #[test]
    fn compile_prefers_linear_engine_when_pattern_allows() {
        // No fancy constructs → the linear engine handles it even with opt-in.
        let pattern = RedactionPattern::compile(r"secret=\w+", true).unwrap();
        assert!(matches!(pattern, RedactionPattern::Linear(_)));
    }

    #[test]
    fn backtracking_pattern_supports_lookbehind() {
        let patterns = vec![RedactionPattern::compile(r"(?<=password=)\S+", true).unwrap()];
        let fields = vec![(
            "msg".to_owned(),
            Value::String("login with password=hunter2 ok".to_owned()),
        )];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""msg":"login with password=[REDACTED] ok""#));
        assert!(!line.contains("hunter2"));
    }

    #[test]
    fn backtracking_pattern_supports_backreferences_and_expansion() {
        let pattern = RedactionPattern::compile(r"\b(\w+) \1\b", true).unwrap();
        assert!(matches!(pattern, RedactionPattern::Backtracking(_)));
        assert_eq!(
            pattern.replace_all("dup dup unique", "$1 [REDACTED]"),
            "dup [REDACTED] unique",
        );
    }

    #[test]
    fn backtracking_evaluation_failure_fails_closed() {
        // The backreference forces the backtracking engine; a limit of 1 makes
        // evaluation exceed it, so the entire string must be redacted rather
        // than emitted unchecked.
        let pattern = RedactionPattern::backtracking_with_limit(r"(a+)+\1$", 1).unwrap();
        assert_eq!(
            pattern.replace_all("aaaaaaaaaaaaaaaaaaaaaaaaaaaa!", "[X]"),
            REDACTED,
        );
    }

    #[test]
    fn key_redaction_and_pattern_redaction_combine() {
        // Key-based redaction masks "token"; pattern-based masks the email in "msg".
        let patterns = vec![RedactionPattern::linear(r"leak@x\.io").unwrap()];
        let fields = vec![
            ("token".to_owned(), Value::String("abc".to_owned())),
            (
                "msg".to_owned(),
                Value::String("ping leak@x.io now".to_owned()),
            ),
        ];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            None,
            Some(&patterns),
            None,
        )
        .unwrap();

        assert!(line.contains(r#""token":"[REDACTED]""#));
        assert!(line.contains(r#""msg":"ping [REDACTED] now""#));
        assert!(!line.contains("leak@x.io"));
    }

    // -- console renderer ----------------------------------------------------

    #[test]
    fn console_renders_human_readable_line_without_colors() {
        let fields = vec![
            ("request_id".to_owned(), Value::String("req-1".to_owned())),
            ("count".to_owned(), Value::Int(42)),
        ];
        let line = render_line_console(
            fields,
            "svc.mod",
            "info",
            "svc",
            "hello world",
            false,
            "TS",
            None,
            None,
            None,
            None,
        );
        // No ANSI escape codes when colors=false.
        assert!(!line.contains('\x1b'));
        assert!(line.starts_with("TS [INFO    ] hello world"));
        assert!(line.contains("request_id=\"req-1\""));
        assert!(line.contains("count=42"));
    }

    #[test]
    fn console_applies_colors_per_level() {
        let line = render_line_console(
            vec![],
            "l",
            "error",
            "svc",
            "boom",
            true,
            "TS",
            None,
            None,
            None,
            None,
        );
        assert!(line.contains("\x1b[31m")); // ANSI_RED
        assert!(line.contains("\x1b[0m")); // ANSI_RESET
        assert!(line.contains("[ERROR   ]"));
    }

    #[test]
    fn console_redacts_sensitive_keys() {
        let fields = vec![("password".to_owned(), Value::String("hunter2".to_owned()))];
        let line = render_line_console(
            fields, "l", "info", "svc", "login", false, "TS", None, None, None, None,
        );
        assert!(line.contains("password=\"[REDACTED]\""));
        assert!(!line.contains("hunter2"));
    }

    #[test]
    fn console_redacts_sensitive_patterns_in_message() {
        let patterns = vec![RedactionPattern::linear(r"secret=\w+").unwrap()];
        let line = render_line_console(
            vec![],
            "l",
            "info",
            "svc",
            "token secret=abc",
            false,
            "TS",
            None,
            Some(&patterns),
            None,
            None,
        );
        assert!(line.contains("token [REDACTED]"));
        assert!(!line.contains("secret=abc"));
    }

    #[test]
    fn console_escapes_control_chars_in_message_and_values() {
        // A newline in the message would forge a second log line; an ANSI escape
        // in a field value would drive the operator's terminal. Both must be
        // neutralized in console output.
        let fields = vec![(
            "note".to_owned(),
            Value::String("val\x1b[2Jwiped".to_owned()),
        )];
        let line = render_line_console(
            fields,
            "l",
            "info",
            "svc",
            "line1\nlevel=CRITICAL forged",
            false,
            "TS",
            None,
            None,
            None,
            None,
        );
        // No raw newline or ESC survives into the rendered line.
        assert!(!line.contains('\n'));
        assert!(!line.contains('\x1b'));
        assert!(line.contains("line1\\nlevel=CRITICAL forged"));
        assert!(line.contains("note=\"val\\x1b[2Jwiped\""));
    }

    #[test]
    fn console_appends_stack_when_provided() {
        let line = render_line_console(
            vec![],
            "l",
            "info",
            "svc",
            "m",
            false,
            "TS",
            None,
            None,
            None,
            Some("Stack (most recent call last):\n  File x"),
        );
        assert!(line.contains("Stack (most recent call last)"));
    }
}
