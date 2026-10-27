//! Python bindings for sampling and rate limiting.

use crate::conversion::string_to_cow;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString};
use std::time::Duration;
use structguru_core::Pipeline;

/// Composed pre-render filter (sampling + rate limiting) in native Rust.
///
/// Returns `True` when the record should be rendered. Dropped records never
/// reach `render_line`, so they cost zero rendering. Drop counters are kept
/// distinct from the writer's transport `dropped` counter.
#[pyclass(name = "NativeFilter")]
pub(crate) struct NativeFilter {
    pipeline: Pipeline,
}

#[pymethods]
impl NativeFilter {
    #[new]
    #[pyo3(signature = (sample_rate=1.0, sample_max_level=None, rate_limit_max=None, rate_limit_period=60.0))]
    fn new(
        sample_rate: f64,
        sample_max_level: Option<&str>,
        rate_limit_max: Option<usize>,
        rate_limit_period: f64,
    ) -> PyResult<Self> {
        if !sample_rate.is_finite() || !(0.0..=1.0).contains(&sample_rate) {
            return Err(PyValueError::new_err(format!(
                "sample_rate must be between 0.0 and 1.0, got {sample_rate}"
            )));
        }
        if let Some(max_count) = rate_limit_max
            && max_count < 1
        {
            return Err(PyValueError::new_err(format!(
                "rate_limit_max must be >= 1, got {max_count}"
            )));
        }
        if rate_limit_period <= 0.0 || !rate_limit_period.is_finite() {
            return Err(PyValueError::new_err(format!(
                "rate_limit_period must be > 0, got {rate_limit_period}"
            )));
        }
        // `from_secs_f64` panics on values too large for `Duration` (e.g. 1e300,
        // reachable via configure() or STRUCTGURU_NATIVE_RATE_LIMIT at import).
        // A panic here crosses PyO3 as an uncatchable PanicException; fail with a
        // clean ValueError instead.
        let period = Duration::try_from_secs_f64(rate_limit_period)
            .map_err(|err| PyValueError::new_err(format!("rate_limit_period too large: {err}")))?;
        Ok(Self {
            pipeline: Pipeline::new(sample_rate, sample_max_level, rate_limit_max, period),
        })
    }

    /// Returns `True` when the record should be kept (rendered).
    ///
    /// `key` is the rate-limit grouping key (usually the formatted message);
    /// `level` is the record's numeric level as in `logging`, so a custom
    /// stdlib level ranks by its own number, or a method name such as
    /// `"info"`.
    #[pyo3(signature = (key, level))]
    fn allow(&self, key: &Bound<'_, PyString>, level: &Bound<'_, PyAny>) -> PyResult<bool> {
        let level = record_level(level)?;
        // Filter the same text the renderer emits, including surrogate
        // replacement, while retaining a borrowed fast path for valid UTF-8.
        Ok(self.pipeline.allow(&string_to_cow(key), level))
    }

    pub(crate) fn allow_level(&self, key: &Bound<'_, PyString>, level: i64) -> bool {
        let level = u32::try_from(level.max(0)).unwrap_or(u32::MAX);
        self.pipeline.allow(&string_to_cow(key), level)
    }

    /// Whether any filter stage is configured.
    fn is_empty(&self) -> bool {
        self.pipeline.is_empty()
    }

    /// Snapshot of pre-render drop counters (`sampled`, `rate_limited`).
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = self.pipeline.stats();
        let result = PyDict::new(py);
        result.set_item("sampled", stats.sampled)?;
        result.set_item("rate_limited", stats.rate_limited)?;
        Ok(result)
    }
}

/// A filter rank for a level given as a `logging` number or a method name.
/// Numbers outside `u32` saturate, so ordering is kept.
fn record_level(level: &Bound<'_, PyAny>) -> PyResult<u32> {
    if let Ok(number) = level.cast::<PyInt>() {
        return Ok(match number.extract::<i64>() {
            Ok(value) => u32::try_from(value.max(0)).unwrap_or(u32::MAX),
            Err(_) if number.lt(0)? => 0,
            Err(_) => u32::MAX,
        });
    }
    let name = level.cast::<PyString>()?;
    Ok(structguru_core::method_level_number(&string_to_cow(name)))
}
