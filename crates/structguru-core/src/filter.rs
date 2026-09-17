//! Pre-render record filters: sampling and rate limiting.
//!
//! These run *before* `render_line`, so a dropped record costs zero rendering.
//! A [`Pipeline`] composes filters and keeps distinct drop counters (sampled vs
//! rate-limited), separate from the writer's transport-level `dropped` counter.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Outcome of a pre-render filter check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Keep,
    Drop,
}

/// A pre-render filter deciding whether a record should be rendered.
pub trait RecordFilter: Send + Sync {
    /// Inspect `key` (the rate-limit grouping key, usually the formatted message)
    /// and `level` (the canonical method name) and return `Keep` or `Drop`.
    fn allow(&self, key: &str, level: &str) -> Decision;
}

/// Numeric level for a method name; unknown names rank as `info`.
fn method_level_num(method: &str) -> u8 {
    crate::level_number(method).unwrap_or(20)
}

/// Probabilistic sampler: keeps a record with probability `rate`.
///
/// With `max_level_num` set, only records at or below that level are sampled;
/// more severe records always pass — the native analog of wrapping
/// `SamplingProcessor` in `ConditionalProcessor(max_level=...)`.
pub struct Sampler {
    rate: f64,
    max_level_num: Option<u8>,
}

impl Sampler {
    /// `rate` is the fraction of records to keep (`0.0`–`1.0`); `1.0` keeps all.
    /// `max_level` is the method name of the highest level that is sampled.
    pub fn new(rate: f64, max_level: Option<&str>) -> Self {
        Self {
            rate: rate.clamp(0.0, 1.0),
            max_level_num: max_level.map(method_level_num),
        }
    }
}

impl RecordFilter for Sampler {
    fn allow(&self, _key: &str, level: &str) -> Decision {
        if let Some(max) = self.max_level_num
            && method_level_num(level) > max
        {
            return Decision::Keep;
        }
        if self.rate >= 1.0 {
            return Decision::Keep;
        }
        if self.rate <= 0.0 {
            return Decision::Drop;
        }
        if fastrand::f64() < self.rate {
            Decision::Keep
        } else {
            Decision::Drop
        }
    }
}

/// Keys the limiter tracks at most; beyond this the least recently seen are
/// evicted so a stream of unique messages cannot grow memory for a period.
pub const RATE_LIMIT_MAX_KEYS: usize = 8192;

/// Sliding-window rate limiter keyed by the formatted message.
///
/// Allows at most `max_count` records per key within `period`. Stale buckets are
/// pruned lazily on access and periodically garbage-collected, mirroring the
/// Python `RateLimitingProcessor` semantics. The key table is capped at
/// `max_keys` entries; when a new key would exceed it, stale buckets are swept
/// and then the least recently seen tenth of the table is evicted.
pub struct RateLimiter {
    max_count: usize,
    period: Duration,
    max_keys: usize,
    buckets: Mutex<RateLimiterState>,
}

/// One key's sliding window plus when the key was last offered a record,
/// dropped or kept, so eviction spares keys that are actively being limited.
#[derive(Default)]
struct Bucket {
    hits: VecDeque<Instant>,
    last_seen: Option<Instant>,
}

struct RateLimiterState {
    /// Per-key sliding-window timestamps.
    timestamps: HashMap<String, Bucket>,
    /// Bounded GC frequency so sweeping does not dominate the hot path.
    cleanup_counter: u64,
    cleanup_interval: u64,
}

impl RateLimiter {
    /// `max_count` records per `period`; both must be positive.
    pub fn new(max_count: usize, period: Duration) -> Self {
        Self::with_max_keys(max_count, period, RATE_LIMIT_MAX_KEYS)
    }

    /// Like [`RateLimiter::new`] with an explicit key-table cap (at least 1).
    pub fn with_max_keys(max_count: usize, period: Duration, max_keys: usize) -> Self {
        Self {
            max_count,
            period,
            max_keys: max_keys.max(1),
            buckets: Mutex::new(RateLimiterState {
                timestamps: HashMap::new(),
                cleanup_counter: 0,
                cleanup_interval: 1000,
            }),
        }
    }

    fn allow_key(&self, key: &str, now: Instant) -> Decision {
        let mut state = self.buckets.lock().expect("rate limiter state poisoned");
        let cutoff = now.checked_sub(self.period);

        // Prune the candidate bucket up to the window edge.
        if let Some(cutoff) = cutoff
            && let Some(bucket) = state.timestamps.get_mut(key)
        {
            while bucket.hits.front().is_some_and(|&t| t <= cutoff) {
                bucket.hits.pop_front();
            }
        }

        if !state.timestamps.contains_key(key) && state.timestamps.len() >= self.max_keys {
            if let Some(cutoff) = cutoff {
                sweep_stale(&mut state, cutoff);
            }
            if state.timestamps.len() >= self.max_keys {
                evict_least_recent(&mut state, self.max_keys - self.max_keys / 10);
            }
        }
        let bucket = state.timestamps.entry(key.to_owned()).or_default();
        bucket.last_seen = Some(now);
        if bucket.hits.len() >= self.max_count {
            return Decision::Drop;
        }
        bucket.hits.push_back(now);

        // Periodic GC of all stale buckets, mirroring the Python processor.
        state.cleanup_counter += 1;
        if state.cleanup_counter >= state.cleanup_interval {
            state.cleanup_counter = 0;
            if let Some(cutoff) = cutoff {
                sweep_stale(&mut state, cutoff);
            }
        }

        Decision::Keep
    }

    #[cfg(test)]
    fn tracked_keys(&self) -> usize {
        self.buckets
            .lock()
            .expect("rate limiter state poisoned")
            .timestamps
            .len()
    }
}

/// Drop timestamps at or before `cutoff` from every bucket and remove empty buckets.
fn sweep_stale(state: &mut RateLimiterState, cutoff: Instant) {
    state.timestamps.retain(|_, bucket| {
        while bucket.hits.front().is_some_and(|&t| t <= cutoff) {
            bucket.hits.pop_front();
        }
        !bucket.hits.is_empty()
    });
}

/// Shrink the key table to `keep` entries, dropping those seen least recently.
fn evict_least_recent(state: &mut RateLimiterState, keep: usize) {
    let excess = state.timestamps.len().saturating_sub(keep);
    if excess == 0 {
        return;
    }
    let mut by_age: Vec<(Option<Instant>, String)> = state
        .timestamps
        .iter()
        .map(|(key, bucket)| (bucket.last_seen, key.clone()))
        .collect();
    by_age.sort_unstable_by_key(|(last, _)| *last);
    for (_, key) in by_age.into_iter().take(excess) {
        state.timestamps.remove(&key);
    }
}

impl RecordFilter for RateLimiter {
    fn allow(&self, key: &str, _level: &str) -> Decision {
        self.allow_key(key, Instant::now())
    }
}

/// Drop accounting for a [`Pipeline`], distinct from the writer's counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FilterStats {
    pub sampled: u64,
    pub rate_limited: u64,
}

/// A filter stage in a [`Pipeline`]. Carries the kind so the pipeline can
/// increment the right counter without dynamic dispatch on the accounting path.
enum Stage {
    Sampler(Sampler),
    RateLimiter(RateLimiter),
}

/// Composed pre-render filter chain.
///
/// Filters run in declaration order; the first `Drop` wins and is attributed to
/// that filter's counter. When no filter drops, rendering proceeds.
pub struct Pipeline {
    stages: Vec<Stage>,
    stats: Mutex<FilterStats>,
}

impl Pipeline {
    /// Build from config. `sample_rate < 1.0` adds a sampler stage (level-gated
    /// when `sample_max_level` is set); `rate_limit_max` adds a rate limiter stage.
    pub fn new(
        sample_rate: f64,
        sample_max_level: Option<&str>,
        rate_limit_max: Option<usize>,
        rate_limit_period: Duration,
    ) -> Self {
        let mut stages = Vec::new();
        if sample_rate < 1.0 {
            stages.push(Stage::Sampler(Sampler::new(sample_rate, sample_max_level)));
        }
        if let Some(max_count) = rate_limit_max {
            stages.push(Stage::RateLimiter(RateLimiter::new(
                max_count,
                rate_limit_period,
            )));
        }
        Self {
            stages,
            stats: Mutex::new(FilterStats::default()),
        }
    }

    /// Returns `false` when any stage drops the record.
    pub fn allow(&self, key: &str, level: &str) -> bool {
        for stage in &self.stages {
            let (decision, sampled, rate_limited) = match stage {
                Stage::Sampler(f) => {
                    let d = f.allow(key, level);
                    (d, d == Decision::Drop, false)
                }
                Stage::RateLimiter(f) => {
                    let d = f.allow(key, level);
                    (d, false, d == Decision::Drop)
                }
            };
            if decision == Decision::Drop {
                let mut stats = self.stats.lock().expect("filter stats poisoned");
                if sampled {
                    stats.sampled += 1;
                }
                if rate_limited {
                    stats.rate_limited += 1;
                }
                return false;
            }
        }
        true
    }

    /// Whether any filter stage is configured.
    pub fn is_empty(&self) -> bool {
        self.stages.is_empty()
    }

    /// Snapshot of drop counters.
    pub fn stats(&self) -> FilterStats {
        *self.stats.lock().expect("filter stats poisoned")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sampler_rate_zero_drops_all() {
        let s = Sampler::new(0.0, None);
        for _ in 0..100 {
            assert_eq!(s.allow("k", "info"), Decision::Drop);
        }
    }

    #[test]
    fn sampler_rate_one_keeps_all() {
        let s = Sampler::new(1.0, None);
        for _ in 0..100 {
            assert_eq!(s.allow("k", "info"), Decision::Keep);
        }
    }

    #[test]
    fn sampler_rate_half_is_statistically_bounded() {
        // Mirror the Python test/test_sampling.py statistical bound.
        let s = Sampler::new(0.5, None);
        let mut kept = 0;
        for _ in 0..1000 {
            if s.allow("k", "info") == Decision::Keep {
                kept += 1;
            }
        }
        assert!(kept > 300 && kept < 700, "kept={kept}");
    }

    #[test]
    fn sampler_max_level_gates_sampling_to_low_levels() {
        // rate=0 would drop everything, but only records <= INFO are sampled.
        let s = Sampler::new(0.0, Some("info"));
        assert_eq!(s.allow("k", "debug"), Decision::Drop);
        assert_eq!(s.allow("k", "info"), Decision::Drop);
        assert_eq!(s.allow("k", "success"), Decision::Drop);
        assert_eq!(s.allow("k", "warning"), Decision::Keep);
        assert_eq!(s.allow("k", "error"), Decision::Keep);
        assert_eq!(s.allow("k", "critical"), Decision::Keep);
    }

    #[test]
    fn pipeline_level_gated_sampler_counts_only_gated_drops() {
        let p = Pipeline::new(0.0, Some("debug"), None, Duration::from_secs(60));
        assert!(!p.allow("k", "debug"));
        assert!(p.allow("k", "info"));
        assert!(p.allow("k", "error"));
        let stats = p.stats();
        assert_eq!(stats.sampled, 1);
    }

    #[test]
    fn rate_limiter_allows_under_limit_then_drops() {
        let r = RateLimiter::new(3, Duration::from_secs(60));
        let now = Instant::now();
        assert_eq!(r.allow_key("alpha", now), Decision::Keep);
        assert_eq!(r.allow_key("alpha", now), Decision::Keep);
        assert_eq!(r.allow_key("alpha", now), Decision::Keep);
        assert_eq!(r.allow_key("alpha", now), Decision::Drop);
    }

    #[test]
    fn rate_limiter_keys_are_independent() {
        let r = RateLimiter::new(1, Duration::from_secs(60));
        let now = Instant::now();
        assert_eq!(r.allow_key("alpha", now), Decision::Keep);
        assert_eq!(r.allow_key("beta", now), Decision::Keep);
        // alpha is exhausted; beta is independent.
        assert_eq!(r.allow_key("alpha", now), Decision::Drop);
        assert_eq!(r.allow_key("beta", now), Decision::Drop);
    }

    #[test]
    fn rate_limiter_key_table_is_capped_and_keeps_hot_keys() {
        let limiter = RateLimiter::with_max_keys(2, Duration::from_secs(60), 100);
        let start = Instant::now();
        // A hot key seen throughout must survive eviction and stay limited.
        assert_eq!(limiter.allow_key("hot", start), Decision::Keep);
        for i in 0..1_000 {
            let now = start + Duration::from_millis(i);
            assert_eq!(
                limiter.allow_key(&format!("unique {i}"), now),
                Decision::Keep
            );
            if i % 50 == 0 {
                limiter.allow_key("hot", now);
            }
        }
        assert!(
            limiter.tracked_keys() <= 100,
            "table grew to {}",
            limiter.tracked_keys()
        );
        assert_eq!(
            limiter.allow_key("hot", start + Duration::from_secs(1)),
            Decision::Drop
        );
    }

    #[test]
    fn rate_limiter_window_expires() {
        // Use a tiny period so we can sleep past it without slowing the suite.
        let r = RateLimiter::new(1, Duration::from_millis(40));
        let now = Instant::now();
        assert_eq!(r.allow_key("k", now), Decision::Keep);
        assert_eq!(r.allow_key("k", now), Decision::Drop);
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(r.allow_key("k", Instant::now()), Decision::Keep);
    }

    #[test]
    fn pipeline_with_no_filters_keeps_all() {
        let p = Pipeline::new(1.0, None, None, Duration::from_secs(60));
        assert!(p.is_empty());
        for _ in 0..10 {
            assert!(p.allow("k", "info"));
        }
        let stats = p.stats();
        assert_eq!(stats.sampled, 0);
        assert_eq!(stats.rate_limited, 0);
    }

    #[test]
    fn pipeline_sampler_only_counts_sampled() {
        let p = Pipeline::new(0.0, None, None, Duration::from_secs(60));
        for _ in 0..5 {
            assert!(!p.allow("k", "info"));
        }
        let stats = p.stats();
        assert_eq!(stats.sampled, 5);
        assert_eq!(stats.rate_limited, 0);
    }

    #[test]
    fn pipeline_rate_limiter_only_counts_rate_limited() {
        let p = Pipeline::new(1.0, None, Some(2), Duration::from_secs(60));
        assert!(p.allow("k", "info"));
        assert!(p.allow("k", "info"));
        assert!(!p.allow("k", "info"));
        let stats = p.stats();
        assert_eq!(stats.sampled, 0);
        assert_eq!(stats.rate_limited, 1);
    }

    #[test]
    fn pipeline_sampler_short_circuits_before_rate_limiter() {
        // rate=0 drops everything; the rate limiter never runs, so its counter stays 0.
        let p = Pipeline::new(0.0, None, Some(2), Duration::from_secs(60));
        for _ in 0..10 {
            assert!(!p.allow("k", "info"));
        }
        let stats = p.stats();
        assert_eq!(stats.sampled, 10);
        assert_eq!(stats.rate_limited, 0);
    }

    #[test]
    fn pipeline_sampler_then_rate_limiter_both_count() {
        // rate=1 so the sampler always keeps; the rate limiter governs drops.
        let p = Pipeline::new(1.0, None, Some(1), Duration::from_secs(60));
        assert!(p.allow("alpha", "info"));
        assert!(!p.allow("alpha", "info"));
        assert!(p.allow("beta", "info"));
        let stats = p.stats();
        assert_eq!(stats.sampled, 0);
        assert_eq!(stats.rate_limited, 1);
    }
}
