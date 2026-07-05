//! Pure Rust core helpers for structguru.
//!
//! This crate is intentionally PyO3-free so the logger engine can grow with
//! normal Rust unit tests and benchmarks before crossing the Python boundary.

mod queue;
mod value;

pub use queue::{BoundedQueue, QueueMetrics};
pub use value::{Value, ValueStats};

/// Return the Rust core crate version.
pub fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Normalize a structguru/loguru-style level name to the canonical field value.
///
/// Unknown levels follow the current Python processor contract: uppercase the
/// provided value and let the severity mapping fall back separately.
pub fn normalize_level(level: &str) -> String {
    match level.to_ascii_lowercase().as_str() {
        "trace" | "debug" => "DEBUG".to_owned(),
        "info" | "success" => "INFO".to_owned(),
        "warning" | "warn" => "WARN".to_owned(),
        "error" | "exception" => "ERROR".to_owned(),
        "critical" | "fatal" => "CRITICAL".to_owned(),
        _ => level.to_ascii_uppercase(),
    }
}

/// Return the RFC 5424 syslog severity code for a canonical level.
///
/// Unknown levels default to informational severity, matching
/// `structguru.processors.add_syslog_severity`.
pub fn syslog_severity(canonical_level: &str) -> u8 {
    match canonical_level {
        "DEBUG" => 7,
        "INFO" => 6,
        "WARN" => 4,
        "ERROR" => 3,
        "CRITICAL" => 2,
        _ => 6,
    }
}

/// Normalize a level and immediately derive its syslog severity.
pub fn normalized_syslog_severity(level: &str) -> u8 {
    syslog_severity(&normalize_level(level))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_known_level_aliases() {
        assert_eq!(normalize_level("trace"), "DEBUG");
        assert_eq!(normalize_level("warning"), "WARN");
        assert_eq!(normalize_level("exception"), "ERROR");
        assert_eq!(normalize_level("fatal"), "CRITICAL");
        assert_eq!(normalize_level("success"), "INFO");
    }

    #[test]
    fn uppercases_unknown_levels() {
        assert_eq!(normalize_level("notice"), "NOTICE");
        assert_eq!(normalize_level("custom_level"), "CUSTOM_LEVEL");
    }

    #[test]
    fn maps_canonical_levels_to_syslog_severity() {
        assert_eq!(syslog_severity("DEBUG"), 7);
        assert_eq!(syslog_severity("INFO"), 6);
        assert_eq!(syslog_severity("WARN"), 4);
        assert_eq!(syslog_severity("ERROR"), 3);
        assert_eq!(syslog_severity("CRITICAL"), 2);
    }

    #[test]
    fn unknown_syslog_severity_defaults_to_info() {
        assert_eq!(syslog_severity("NOTICE"), 6);
        assert_eq!(normalized_syslog_severity("notice"), 6);
    }
}
