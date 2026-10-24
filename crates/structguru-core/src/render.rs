//! Native render pipeline: assemble the event map, redact, and serialize.
//!
//! This reproduces the output contract of the Python structlog processor chain
//! for the common (non-exception) case: key-based redaction, canonical level,
//! syslog severity, and the `event -> message` rename, rendered as compact JSON.

use crate::value::Value;
use crate::{normalize_level, syslog_severity};
use regex::Regex;
use std::borrow::Cow;
use std::collections::HashMap;

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
    /// (`$1`, `${name}`; `$$` for a literal `$`). Borrows `text` back when
    /// nothing matched, so scanning clean text does not allocate. The linear
    /// engine checks `is_match` first: on the short field keys and values that
    /// make up most records it costs about a third of an empty `replace_all`.
    ///
    /// A backtracking pattern whose evaluation errors at match time (backtrack
    /// limit exceeded) fails CLOSED: the entire string becomes `[REDACTED]`.
    /// Emitting a value the configured pattern could not check risks leaking
    /// exactly the data redaction exists to mask.
    pub fn replace_all<'t>(&self, text: &'t str, replacement: &str) -> Cow<'t, str> {
        match self {
            Self::Linear(re) if re.is_match(text) => re.replace_all(text, replacement),
            Self::Linear(_) => Cow::Borrowed(text),
            Self::Backtracking(re) => replace_all_backtracking(re, text, replacement)
                .unwrap_or_else(|| Cow::Owned(REDACTED.to_owned())),
        }
    }
}

/// fancy-regex's `replace_all` panics on evaluation errors; iterate matches
/// manually so a backtrack-limit error surfaces as `None` instead.
fn replace_all_backtracking<'t>(
    re: &fancy_regex::Regex,
    text: &'t str,
    replacement: &str,
) -> Option<Cow<'t, str>> {
    let mut out: Option<String> = None;
    let mut last = 0;
    for caps in re.captures_iter(text) {
        let caps = caps.ok()?;
        let matched = caps.get(0).expect("capture group 0 is the whole match");
        let out = out.get_or_insert_with(|| String::with_capacity(text.len()));
        out.push_str(&text[last..matched.start()]);
        fancy_regex::Expander::default().append_expansion(out, replacement, &caps);
        last = matched.end();
    }
    Some(match out {
        None => Cow::Borrowed(text),
        Some(mut out) => {
            out.push_str(&text[last..]);
            Cow::Owned(out)
        }
    })
}

/// Run every pattern over `text` in declaration order, so later patterns see
/// the output of earlier ones. `None` when no pattern matched.
fn apply_patterns(text: &str, patterns: &[RedactionPattern], replacement: &str) -> Option<String> {
    let mut current = Cow::Borrowed(text);
    for pattern in patterns {
        let replaced = match pattern.replace_all(&current, replacement) {
            Cow::Owned(replaced) => replaced,
            Cow::Borrowed(_) => continue,
        };
        current = Cow::Owned(replaced);
    }
    match current {
        Cow::Borrowed(_) => None,
        Cow::Owned(redacted) => Some(redacted),
    }
}

/// Internal marker set by `RedactingProcessor`. This constant is the single
/// source of truth: `structguru.redaction.REDACTED_MARKER_KEY` is built from
/// it at import. `strip_redaction_marker` removes it before rendering on the
/// standard path; drop it here too so a user field of the same name never
/// reaches the output.
pub const REDACTED_MARKER_KEY: &str = "_structguru_redacted";

/// Default sensitive keys. This constant is the single source of truth:
/// `structguru.redaction.DEFAULT_SENSITIVE_KEYS` is built from it at import.
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
    keys.iter().any(|candidate| {
        let candidate = candidate.as_ref();
        if key.is_ascii() && candidate.is_ascii() {
            // Default keys and ordinary fields need no lowercase allocation.
            candidate.eq_ignore_ascii_case(key)
        } else {
            // Match the Unicode lowercasing used by Python locals redaction,
            // including non-ASCII letters that lowercase to ASCII (e.g. K).
            candidate.to_lowercase() == key.to_lowercase()
        }
    })
}

/// Recursively redact sensitive keys in place (key-based, matches the default
/// `RedactingProcessor`: no value-pattern matching).
fn redact(value: &mut Value, keys: &[impl AsRef<str>]) {
    match value {
        Value::Keyed {
            aliases,
            value: child,
        } => {
            let redacted = if aliases.iter().any(|alias| is_sensitive(alias, keys)) {
                Value::String(REDACTED.to_owned())
            } else {
                redact(child, keys);
                std::mem::replace(child.as_mut(), Value::Null)
            };
            *value = redacted;
        }
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

/// Recursively apply regex patterns in place to every map key, string value,
/// and integer at any depth.
///
/// Each compiled pattern runs via `replace_all` in declaration order, so later
/// patterns see the output of earlier ones. An integer is matched against its
/// decimal text (`Value::Raw` included: it only ever carries the digits of an
/// integer outside the `i64` range) and becomes the redacted *string* when a
/// pattern matches, so a 16-digit card number cannot pass as a number.
/// Floats, booleans, and null are not pattern-matched.
///
/// Keys are redacted too: a secret used as a key (an email address mapping to
/// a count) must not survive where the same text as a value would not. When
/// redaction makes keys of one map equal, they collapse into one entry (see
/// [`merge_duplicate_keys`]) so JSON output never repeats a key.
///
/// `replacement` supports the regex crate's group expansion (`$1`, `${name}`;
/// `$$` for a literal `$`), so look-behind-style patterns can be rewritten as
/// capture groups that preserve their prefix, e.g. pattern `(password=)\S+`
/// with replacement `$1[REDACTED]`. The group must wrap the prefix, not the
/// secret — `password=(\S+)` with `$1[REDACTED]` would re-emit the secret.
fn redact_patterns(value: &mut Value, patterns: &[RedactionPattern], replacement: &str) {
    match value {
        Value::Keyed { value, .. } => redact_patterns(value, patterns, replacement),
        Value::Map(entries) => {
            let mut renamed = false;
            for (key, child) in entries.iter_mut() {
                if let Some(redacted) = apply_patterns(key, patterns, replacement) {
                    *key = redacted;
                    renamed = true;
                }
                redact_patterns(child, patterns, replacement);
            }
            if renamed {
                merge_duplicate_keys(entries);
            }
        }
        Value::List(items) => {
            for item in items.iter_mut() {
                redact_patterns(item, patterns, replacement);
            }
        }
        Value::String(text) => {
            if let Some(redacted) = apply_patterns(text, patterns, replacement) {
                *text = redacted;
            }
        }
        Value::Int(number) => {
            let mut digits = [0; 20];
            let digits = integer_text(*number, &mut digits);
            if let Some(redacted) = apply_patterns(digits, patterns, replacement) {
                *value = Value::String(redacted);
            }
        }
        Value::Raw(json) if is_integer_literal(json) => {
            if let Some(redacted) = apply_patterns(json, patterns, replacement) {
                *value = Value::String(redacted);
            }
        }
        _ => {}
    }
}

/// Decimal text of `number` in a stack buffer (20 bytes fit `i64::MIN`), so
/// matching integers does not allocate.
fn integer_text(number: i64, buffer: &mut [u8; 20]) -> &str {
    let mut rest = number.unsigned_abs();
    let mut start = buffer.len();
    loop {
        start -= 1;
        buffer[start] = b'0' + (rest % 10) as u8;
        rest /= 10;
        if rest == 0 {
            break;
        }
    }
    if number < 0 {
        start -= 1;
        buffer[start] = b'-';
    }
    std::str::from_utf8(&buffer[start..]).expect("ASCII digits are UTF-8")
}

fn is_integer_literal(json: &str) -> bool {
    let digits = json.strip_prefix('-').unwrap_or(json);
    !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
}

/// Collapse entries whose keys became equal into one: the first occurrence
/// keeps its position and takes the last value, as assigning into a dict
/// would.
fn merge_duplicate_keys(entries: &mut Vec<(String, Value)>) {
    let mut positions: HashMap<String, usize> = HashMap::with_capacity(entries.len());
    let mut merged: Vec<(String, Value)> = Vec::with_capacity(entries.len());
    for (key, value) in entries.drain(..) {
        match positions.get(&key) {
            Some(&position) => merged[position].1 = value,
            None => {
                positions.insert(key.clone(), merged.len());
                merged.push((key, value));
            }
        }
    }
    *entries = merged;
}

/// Strip the marker key, then apply key-based and pattern-based redaction to
/// the user fields shared by both renderers.
fn redact_fields(
    mut fields: Vec<(String, Value)>,
    sensitive_keys: Option<&[String]>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
) -> Vec<(String, Value)> {
    // strip_redaction_marker semantics: the marker key never reaches the renderer.
    fields.retain(|(key, _)| key != REDACTED_MARKER_KEY);
    // Redact against the caller's keys, or the static defaults with zero
    // per-record allocation (comparison is case-insensitive in `is_sensitive`).
    let mut root = Value::Map(fields);
    match sensitive_keys {
        Some(custom) => redact(&mut root, custom),
        None => redact(&mut root, DEFAULT_SENSITIVE_KEYS),
    }
    if let Some(patterns) = sensitive_patterns.filter(|patterns| !patterns.is_empty()) {
        redact_patterns(&mut root, patterns, pattern_replacement.unwrap_or(REDACTED));
    }
    let Value::Map(entries) = root else {
        unreachable!("root is constructed as a map");
    };
    entries
}

fn redact_text(
    key: &str,
    text: &str,
    sensitive_keys: Option<&[String]>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
) -> String {
    let key_is_sensitive = match sensitive_keys {
        Some(custom) => is_sensitive(key, custom),
        None => is_sensitive(key, DEFAULT_SENSITIVE_KEYS),
    };
    if key_is_sensitive {
        return REDACTED.to_owned();
    }
    sensitive_patterns
        .and_then(|patterns| {
            apply_patterns(text, patterns, pattern_replacement.unwrap_or(REDACTED))
        })
        .unwrap_or_else(|| text.to_owned())
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
    sensitive_keys: Option<&[String]>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
) -> Result<String, serde_json::Error> {
    let redacted_message = redact_text(
        "message",
        message,
        sensitive_keys,
        sensitive_patterns,
        pattern_replacement,
    );
    let mut entries = redact_fields(
        fields,
        sensitive_keys,
        sensitive_patterns,
        pattern_replacement,
    );

    let canonical = normalize_level(level);
    let severity = syslog_severity(&canonical);

    // Standard keys mirror the structlog processors exactly. logger/level/
    // severity/timestamp/message are authoritative overrides; the processors
    // *assign* into the event dict, which replaces a colliding user field's
    // value at its original position — so upsert in place rather than
    // drop-and-append. "service" uses setdefault semantics — a user-provided
    // "service" wins — matching `add_service`.
    upsert(
        &mut entries,
        "logger",
        Value::String(redact_text(
            "logger",
            logger,
            sensitive_keys,
            sensitive_patterns,
            pattern_replacement,
        )),
    );
    upsert(&mut entries, "level", Value::String(canonical));
    upsert(&mut entries, "severity", Value::Int(i64::from(severity)));
    upsert(
        &mut entries,
        "timestamp",
        Value::String(timestamp.to_owned()),
    );
    if !entries.iter().any(|(key, _)| key == "service") {
        entries.push((
            "service".to_owned(),
            Value::String(redact_text(
                "service",
                service,
                sensitive_keys,
                sensitive_patterns,
                pattern_replacement,
            )),
        ));
    }
    // "stack" sits between "service" and "message": StackInfoRenderer runs
    // after add_service and before EventRenamer in the shared chain.
    if let Some(stack) = stack {
        let redacted_stack = redact_text(
            "stack",
            stack,
            sensitive_keys,
            sensitive_patterns,
            pattern_replacement,
        );
        upsert(&mut entries, "stack", Value::String(redacted_stack));
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

/// Whether `ch` must not reach console output raw.
///
/// The JSON renderer gets escaping from serde_json; the human-readable console
/// path does not, so without this a request-controlled message or field could
/// embed a newline (forging a separate log line), an ANSI escape sequence
/// (manipulating the operator's terminal), a Unicode line or paragraph
/// separator (a line break for `str.splitlines()` and many viewers), or a
/// bidirectional formatting control (reordering how the rest of the line
/// displays). Covers C0/C1 controls, U+2028, U+2029, and the bidi controls.
fn is_console_unsafe(ch: char) -> bool {
    ch.is_control()
        || matches!(
            ch,
            '\u{2028}'
                | '\u{2029}'
                | '\u{061C}'
                | '\u{200E}'
                | '\u{200F}'
                | '\u{202A}'..='\u{202E}'
                | '\u{2066}'..='\u{2069}'
        )
}

fn push_char_escape(buf: &mut String, ch: char) {
    use std::fmt::Write;
    match ch {
        '\n' => buf.push_str("\\n"),
        '\r' => buf.push_str("\\r"),
        '\t' => buf.push_str("\\t"),
        c if u32::from(c) <= 0xFF => {
            let _ = write!(buf, "\\x{:02x}", u32::from(c));
        }
        c => {
            let _ = write!(buf, "\\u{:04x}", u32::from(c));
        }
    }
}

/// Append free text (timestamp, level, message, stack and exception lines)
/// with unsafe characters escaped. Backslashes stay literal, so paths and
/// regexes remain readable.
fn push_escaped(buf: &mut String, s: &str) {
    // Printable ASCII needs no escaping: copy it in one step.
    if s.bytes().all(|byte| (0x20..0x7F).contains(&byte)) {
        buf.push_str(s);
        return;
    }
    for ch in s.chars() {
        if is_console_unsafe(ch) {
            push_char_escape(buf, ch);
        } else {
            buf.push(ch);
        }
    }
}

/// Append `s` as a double-quoted console string. `"` and `\` are escaped as
/// well, so a value cannot close its quotes and forge a following `k=v` pair,
/// and a literal backslash sequence stays distinguishable from an escape.
fn push_quoted(buf: &mut String, s: &str) {
    buf.push('"');
    if s.bytes()
        .all(|byte| (0x20..0x7F).contains(&byte) && byte != b'"' && byte != b'\\')
    {
        buf.push_str(s);
    } else {
        for ch in s.chars() {
            match ch {
                '"' => buf.push_str("\\\""),
                '\\' => buf.push_str("\\\\"),
                c if is_console_unsafe(c) => push_char_escape(buf, c),
                c => buf.push(c),
            }
        }
    }
    buf.push('"');
}

/// Append a field key bare, or quoted when its text could be mistaken for
/// separators: whitespace, `=`, quotes, backslashes, and unsafe characters at
/// any depth, plus the `:`, `,`, `{`, `}` delimiters inside a nested map.
fn push_key(buf: &mut String, key: &str, nested: bool) {
    let ambiguous = key.is_empty()
        || key.chars().any(|ch| {
            ch.is_whitespace()
                || matches!(ch, '=' | '"' | '\\')
                || (nested && matches!(ch, ':' | ',' | '{' | '}'))
                || is_console_unsafe(ch)
        });
    if ambiguous {
        push_quoted(buf, key);
    } else {
        buf.push_str(key);
    }
}

/// Append a multi-line block (stack, exception) with every line indented as a
/// continuation of the record and escaped after redaction.
fn push_block(buf: &mut String, text: &str) {
    for line in text.split('\n') {
        buf.push_str("\n  ");
        push_escaped(buf, line);
    }
}

/// Format a [`Value`] for the console renderer (human-readable, not JSON).
fn display_value(value: &Value, buf: &mut String) {
    use std::fmt::Write;
    match value {
        Value::Null => buf.push_str("None"),
        Value::Bool(b) => buf.push_str(if *b { "True" } else { "False" }),
        Value::Int(i) => {
            let _ = write!(buf, "{i}");
        }
        // The JSON renderer's shortest round-trip text: 1.0 stays `1.0` and
        // 1e300 stays short. Non-finite floats render like JSON null.
        Value::Float(f) => match serde_json::Number::from_f64(*f) {
            Some(number) => {
                let _ = write!(buf, "{number}");
            }
            None => buf.push_str("None"),
        },
        Value::String(s) => push_quoted(buf, s),
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
                push_key(buf, k, true);
                buf.push(':');
                display_value(v, buf);
            }
            buf.push('}');
        }
        Value::Raw(json) => buf.push_str(json), // already-serialized; emit verbatim
        Value::Keyed { value, .. } => display_value(value, buf),
    }
}

/// Render a single log line as a colored, human-readable console string.
///
/// Format: ``<timestamp> [<LEVEL>] <message>  k1=v1  k2=v2``, then the stack
/// and a formatted (string) `exception` as indented blocks.
///
/// This is structguru's own stable dev format, not a structlog
/// ``ConsoleRenderer`` clone. The same redaction pipeline as [`render_line`]
/// is applied first, and the fields shown are the ones the JSON renderer keeps:
/// user `logger`/`level`/`severity`/`timestamp`/`message` values (which JSON
/// overrides) are omitted, as is a user `stack` when a stack is rendered.
#[allow(clippy::too_many_arguments)]
pub fn render_line_console(
    fields: Vec<(String, Value)>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    colors: bool,
    timestamp: &str,
    sensitive_keys: Option<&[String]>,
    sensitive_patterns: Option<&[RedactionPattern]>,
    pattern_replacement: Option<&str>,
    stack: Option<&str>,
) -> String {
    let redacted_message = redact_text(
        "message",
        message,
        sensitive_keys,
        sensitive_patterns,
        pattern_replacement,
    );
    let entries = redact_fields(
        fields,
        sensitive_keys,
        sensitive_patterns,
        pattern_replacement,
    );

    let canonical = normalize_level(level);
    let (level_pad, color) = level_style(&canonical, colors);

    let mut out = String::with_capacity(128);
    push_escaped(&mut out, timestamp);
    out.push(' ');
    if colors {
        out.push_str(color);
    }
    out.push('[');
    push_escaped(&mut out, &level_pad);
    out.push(']');
    if colors {
        out.push_str(ANSI_RESET);
    }
    out.push(' ');
    push_escaped(&mut out, &redacted_message);

    let mut exception = None;
    for (key, value) in entries {
        match (key.as_str(), value) {
            ("logger" | "level" | "severity" | "timestamp" | "message", _) => {}
            ("stack", _) if stack.is_some() => {}
            ("exception", Value::String(text)) => exception = Some(text),
            (_, value) => {
                out.push_str("  ");
                push_key(&mut out, &key, false);
                out.push('=');
                display_value(&value, &mut out);
            }
        }
    }
    // Logger and service are not part of the console layout.
    let _ = (logger, service);

    if let Some(stack) = stack {
        let redacted_stack = redact_text(
            "stack",
            stack,
            sensitive_keys,
            sensitive_patterns,
            pattern_replacement,
        );
        push_block(&mut out, &redacted_stack);
    }
    if let Some(exception) = exception {
        push_block(&mut out, &exception);
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
    fn stack_redaction_applies_patterns_and_custom_keys_in_both_formats() {
        let patterns = vec![RedactionPattern::linear(r"(token=)\w+").unwrap()];
        for keys in [None, Some(vec!["STACK".to_owned()])] {
            let keys = keys.as_deref();
            let expected = if keys.is_some() {
                "[REDACTED]"
            } else {
                "frame\ntoken=[MASKED]"
            };
            let json = render_line(
                vec![],
                "l",
                "info",
                "svc",
                "trace",
                "TS",
                Some("frame\ntoken=REVIEW_SENTINEL"),
                keys,
                Some(&patterns),
                Some("$1[MASKED]"),
            )
            .unwrap();
            let record: serde_json::Value = serde_json::from_str(&json).unwrap();
            assert_eq!(record["stack"], expected);
            let console = render_line_console(
                vec![],
                "l",
                "info",
                "svc",
                "trace",
                false,
                "TS",
                keys,
                Some(&patterns),
                Some("$1[MASKED]"),
                Some("frame\ntoken=REVIEW_SENTINEL"),
            );
            let expected_console = expected.replace('\n', "\n  ");
            assert_eq!(
                console,
                format!("TS [INFO    ] trace\n  {expected_console}")
            );
        }
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
        let keys = vec!["secret_sauce".to_owned()];
        let line = render_line(
            fields,
            "l",
            "info",
            "svc",
            "m",
            "TS",
            None,
            Some(&keys),
            None,
            None,
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
            Some(&["message".to_owned()][..]),
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
    fn pattern_redaction_skips_bool_float_null_and_non_integer_raw() {
        let any = RedactionPattern::linear(r"\d+|true|null|escaped").unwrap();
        let patterns = vec![any];
        let fields = vec![
            ("flag".to_owned(), Value::Bool(true)),
            ("ratio".to_owned(), Value::Float(1.5)),
            ("nothing".to_owned(), Value::Null),
            // Raw that is not integer digits is emitted verbatim, unmatched.
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

        assert!(line.contains(r#""flag":true"#));
        assert!(line.contains(r#""ratio":1.5"#));
        assert!(line.contains(r#""nothing":null"#));
        assert!(line.contains(r#""raw":"escaped""#));
    }

    #[test]
    fn pattern_redacts_map_keys_at_every_depth() {
        let patterns = vec![RedactionPattern::linear(r"\w+@corp\.com").unwrap()];
        let fields = vec![
            ("bob@corp.com".to_owned(), Value::String("x".to_owned())),
            (
                "rows".to_owned(),
                Value::List(vec![Value::Map(vec![(
                    "alice@corp.com".to_owned(),
                    Value::Int(3),
                )])]),
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

        assert!(line.starts_with(r#"{"[REDACTED]":"x","rows":[{"[REDACTED]":3}],"logger""#));
    }

    #[test]
    fn pattern_redacted_keys_that_collide_keep_first_position_and_last_value() {
        let patterns = vec![RedactionPattern::linear(r"\w+@corp\.com").unwrap()];
        let fields = vec![
            ("a@corp.com".to_owned(), Value::Int(1)),
            (
                "nested".to_owned(),
                Value::Map(vec![
                    ("[REDACTED]".to_owned(), Value::Int(1)),
                    ("keep".to_owned(), Value::Int(2)),
                    ("x@corp.com".to_owned(), Value::Int(3)),
                ]),
            ),
            ("b@corp.com".to_owned(), Value::Int(4)),
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

        assert!(line.starts_with(r#"{"[REDACTED]":4,"nested":{"[REDACTED]":3,"keep":2},"logger""#));
    }

    #[test]
    fn pattern_redacts_matching_integers_into_strings() {
        let patterns = vec![RedactionPattern::linear(r"\b\d{16,}\b").unwrap()];
        let fields = vec![
            ("card".to_owned(), Value::Int(4_111_111_111_111_111)),
            ("negative".to_owned(), Value::Int(-4_111_111_111_111_111)),
            ("qty".to_owned(), Value::Int(16)),
            (
                "big".to_owned(),
                Value::Raw("123456789012345678901234".to_owned()),
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

        assert!(line.starts_with(
            r#"{"card":"[REDACTED]","negative":"-[REDACTED]","qty":16,"big":"[REDACTED]","#
        ));
    }

    #[test]
    fn integer_text_matches_display() {
        for number in [0, 7, -7, 42, i64::MAX, i64::MIN, -1_000_000] {
            assert_eq!(integer_text(number, &mut [0; 20]), number.to_string());
        }
    }

    #[test]
    fn apply_patterns_borrows_when_nothing_matches() {
        let patterns = vec![
            RedactionPattern::linear(r"secret").unwrap(),
            RedactionPattern::compile(r"(?<=k=)\w+", true).unwrap(),
        ];
        assert_eq!(apply_patterns("plain text", &patterns, REDACTED), None);
        assert_eq!(
            apply_patterns("secret k=v", &patterns, REDACTED).as_deref(),
            Some("[REDACTED] k=[REDACTED]"),
        );
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
        // Lookbehind rewrite: `(?<=password=)\S+` becomes `(password=)\S+` with
        // a replacement that re-emits the captured prefix.
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
    fn backtracking_evaluation_failure_redacts_through_render_pipeline() {
        let pattern = RedactionPattern::backtracking_with_limit(r"(a+)+\1$", 1).unwrap();
        let text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaa!";
        assert_eq!(
            apply_patterns(text, &[pattern], "[X]"),
            Some(REDACTED.to_owned())
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
    fn console_quotes_ambiguous_keys_and_escapes_quoted_values() {
        let fields = vec![
            (
                "user".to_owned(),
                Value::String(r#"bob"  admin="true"#.to_owned()),
            ),
            ("a=1  b".to_owned(), Value::String("v".to_owned())),
            ("lit".to_owned(), Value::String(r"x\ny".to_owned())),
            (
                "nested".to_owned(),
                Value::Map(vec![("a:1, b".to_owned(), Value::Int(2))]),
            ),
        ];
        let line = render_line_console(
            fields, "l", "info", "svc", "m", false, "TS", None, None, None, None,
        );
        assert_eq!(
            line,
            r#"TS [INFO    ] m  user="bob\"  admin=\"true"  "a=1  b"="v"  lit="x\\ny"  nested={"a:1, b":2}"#,
        );
    }

    #[test]
    fn console_escapes_line_separators_and_bidi_controls() {
        let fields = vec![(
            "k\u{202E}".to_owned(),
            Value::String("a\u{2028}b".to_owned()),
        )];
        let line = render_line_console(
            fields,
            "l",
            "info",
            "svc",
            "m\u{2029}\u{061C}",
            false,
            "TS",
            None,
            None,
            None,
            Some("s\u{2066}"),
        );
        assert_eq!(
            line,
            r#"TS [INFO    ] m\u2029\u061c  "k\u202e"="a\u2028b"
  s\u2066"#,
        );
    }

    #[test]
    fn console_floats_match_json_number_text() {
        let fields = vec![
            ("one".to_owned(), Value::Float(1.0)),
            ("big".to_owned(), Value::Float(1e300)),
            ("negz".to_owned(), Value::Float(-0.0)),
            ("nan".to_owned(), Value::Float(f64::NAN)),
        ];
        let line = render_line_console(
            fields, "l", "info", "svc", "m", false, "TS", None, None, None, None,
        );
        assert_eq!(
            line,
            "TS [INFO    ] m  one=1.0  big=1e+300  negz=-0.0  nan=None"
        );
    }

    #[test]
    fn console_renders_string_exception_as_block_after_stack() {
        let fields = vec![
            ("code".to_owned(), Value::Int(1)),
            (
                "exception".to_owned(),
                Value::String("Traceback:\n  frame\nValueError: x".to_owned()),
            ),
            ("service".to_owned(), Value::String("user-svc".to_owned())),
            ("stack".to_owned(), Value::String("user-stack".to_owned())),
            ("logger".to_owned(), Value::String("spoof".to_owned())),
        ];
        let line = render_line_console(
            fields,
            "l",
            "error",
            "svc",
            "failed",
            false,
            "TS",
            None,
            None,
            None,
            Some("Stack:\n  f"),
        );
        assert_eq!(
            line,
            "TS [ERROR   ] failed  code=1  service=\"user-svc\"\n  Stack:\n    f\n  Traceback:\n    frame\n  ValueError: x",
        );
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
        assert_eq!(
            line,
            "TS [INFO    ] m\n  Stack (most recent call last):\n    File x"
        );
    }

    #[test]
    fn console_escapes_and_indents_each_stack_line() {
        for (stack, expected) in [
            ("", "\n  "),
            ("\n", "\n  \n  "),
            ("frame\r\n\tentry\n", "\n  frame\\r\n  \\tentry\n  "),
            (
                "Stack:\n\x1b[2J2030-01-01 [INFO] forged\x00\x08\u{85}",
                "\n  Stack:\n  \\x1b[2J2030-01-01 [INFO] forged\\x00\\x08\\x85",
            ),
        ] {
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
                Some(stack),
            );
            assert_eq!(line, format!("TS [INFO    ] m{expected}"));
        }
    }
}
