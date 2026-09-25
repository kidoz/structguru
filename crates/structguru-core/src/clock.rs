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

/// Unix seconds bounding years 1 through 9999, the range of Python's `datetime`.
const UNIX_SECONDS_RANGE: std::ops::Range<f64> = -62_135_596_800.0..253_402_300_800.0;

/// `seconds` since the Unix epoch, as `time.time()` and `LogRecord.created`
/// give them, in the [`now_iso8601`] format; `None` when the value is not
/// finite or falls outside years 1 through 9999.
///
/// The fraction rounds to the nearest microsecond, ties to even, the way
/// `datetime.fromtimestamp` rounds it.
pub fn unix_to_iso8601(seconds: f64) -> Option<String> {
    if !UNIX_SECONDS_RANGE.contains(&seconds) {
        return None;
    }
    let whole = seconds.floor();
    // `seconds - whole` is exact, so this rounds only once, like CPython.
    let micros = ((seconds - whole) * 1_000_000.0).round_ties_even();
    let (whole, micros) = if micros >= 1_000_000.0 {
        (whole + 1.0, 0)
    } else {
        (whole, micros as u32)
    };
    let datetime = OffsetDateTime::from_unix_timestamp(whole as i64).ok()?;
    let fmt = format_description!("[year]-[month]-[day]T[hour]:[minute]:[second]");
    Some(format!("{}.{micros:06}Z", datetime.format(&fmt).ok()?))
}

#[cfg(test)]
mod tests {
    use super::{now_iso8601, unix_to_iso8601};

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

    #[test]
    fn formats_unix_seconds_like_datetime_fromtimestamp() {
        let cases = [
            (0.0, "1970-01-01T00:00:00.000000Z"),
            (1_000_000_000.123456, "2001-09-09T01:46:40.123456Z"),
            // Rounds to the nearest microsecond, carrying into the second.
            (1_700_000_000.999_999_6, "2023-11-14T22:13:21.000000Z"),
            // A tie rounds to even: 1/128 s is 7812.5 us.
            (0.0078125, "1970-01-01T00:00:00.007812Z"),
            (-1.5, "1969-12-31T23:59:58.500000Z"),
            (-62_135_596_800.0, "0001-01-01T00:00:00.000000Z"),
        ];
        for (seconds, expected) in cases {
            assert_eq!(
                unix_to_iso8601(seconds).as_deref(),
                Some(expected),
                "{seconds}"
            );
        }
    }

    #[test]
    fn rejects_unrepresentable_unix_seconds() {
        for seconds in [
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
            1e20,
            -62_135_596_801.0,
            253_402_300_800.0,
        ] {
            assert_eq!(unix_to_iso8601(seconds), None, "{seconds}");
        }
    }
}
