//! Timestamp generation for the native render path.
//!
//! Produces an ISO-8601 UTC timestamp matching structlog's
//! `TimeStamper(fmt="iso", utc=True)` output, e.g. `2026-07-08T16:19:24.616660Z`.
//!
//! The whole-second prefix (`YYYY-MM-DDTHH:MM:SS`) is cached per thread and only
//! re-rendered when the second rolls over; the microsecond suffix is appended
//! cheaply on every call. This keeps the calendar breakdown off the hot path
//! (architecture §5/§14).

use std::cell::RefCell;

use time::OffsetDateTime;
use time::macros::format_description;

thread_local! {
    // (unix_second, "YYYY-MM-DDTHH:MM:SS")
    static PREFIX_CACHE: RefCell<(i64, String)> = const { RefCell::new((i64::MIN, String::new())) };
}

/// Current time as an ISO-8601 UTC string with microsecond precision + `Z`.
pub fn now_iso8601() -> String {
    let now = OffsetDateTime::now_utc();
    let secs = now.unix_timestamp();
    let micros = now.microsecond(); // 0..1_000_000

    PREFIX_CACHE.with(|cell| {
        let mut cache = cell.borrow_mut();
        if cache.0 != secs {
            let fmt = format_description!("[year]-[month]-[day]T[hour]:[minute]:[second]");
            cache.1 = now.format(&fmt).unwrap_or_default();
            cache.0 = secs;
        }
        format!("{}.{:06}Z", cache.1, micros)
    })
}

#[cfg(test)]
mod tests {
    use super::now_iso8601;

    #[test]
    fn produces_iso8601_microsecond_utc() {
        let ts = now_iso8601();
        // e.g. 2026-07-08T16:19:24.616660Z
        assert_eq!(ts.len(), 27, "unexpected timestamp: {ts}");
        assert!(ts.ends_with('Z'));
        assert_eq!(&ts[4..5], "-");
        assert_eq!(&ts[10..11], "T");
        assert_eq!(&ts[19..20], ".");
    }
}
