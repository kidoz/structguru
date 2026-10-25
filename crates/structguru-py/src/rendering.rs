//! Rendering entry points and compiled per-record configuration.

use crate::conversion::{
    convert_fields, merge_record_fields, string_to_cow, string_to_owned, text_arguments,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use structguru_core::{RedactionPattern, Value};

// Never expose compiler diagnostics: they embed the pattern source.
fn compile_pattern(
    pattern: &str,
    allow_backtracking: bool,
    index: usize,
) -> PyResult<RedactionPattern> {
    RedactionPattern::compile(pattern, allow_backtracking).map_err(|_| {
        PyValueError::new_err(format!(
            "sensitive_patterns[{index}]: invalid or unsupported regex"
        ))
    })
}

/// Validate regex pattern strings for native value-pattern redaction.
///
/// Compiles each pattern with Rust's `regex` engine and returns the count.
/// With `allow_backtracking`, patterns the linear engine rejects (look-around,
/// backreferences) are compiled with the bounded backtracking engine instead.
/// Raises `ValueError` on the first pattern that fails to compile so
/// `configure()` can fail loudly at setup time.
#[pyfunction]
#[pyo3(signature = (patterns, allow_backtracking=false))]
pub(crate) fn validate_patterns(
    patterns: Vec<String>,
    allow_backtracking: bool,
) -> PyResult<usize> {
    for (index, p) in patterns.iter().enumerate() {
        compile_pattern(p, allow_backtracking, index)?;
    }
    Ok(patterns.len())
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, timestamp=None, sensitive_keys=None, sensitive_patterns=None, stack=None, pattern_replacement=None))]
pub(crate) fn render_line(
    fields: &Bound<'_, PyDict>,
    logger: &Bound<'_, PyString>,
    level: &str,
    service: &Bound<'_, PyString>,
    message: &Bound<'_, PyString>,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<Vec<String>>,
    stack: Option<&Bound<'_, PyString>>,
    pattern_replacement: Option<&str>,
) -> PyResult<String> {
    let entries = convert_fields(fields)?;
    let (logger, service, message, stack) = text_arguments(logger, service, message, stack);
    let (logger, service, message, stack) = (
        logger.as_str(),
        service.as_str(),
        message.as_str(),
        stack.as_deref(),
    );
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let compiled_patterns: Vec<RedactionPattern> = match &sensitive_patterns {
        Some(patterns) => patterns
            .iter()
            .enumerate()
            .map(|(index, p)| compile_pattern(p, false, index))
            .collect::<PyResult<Vec<_>>>()?,
        None => Vec::new(),
    };
    let patterns_ref = if compiled_patterns.is_empty() {
        None
    } else {
        Some(compiled_patterns.as_slice())
    };
    structguru_core::render_line(
        entries,
        logger,
        level,
        service,
        message,
        timestamp,
        stack,
        sensitive_keys.as_deref(),
        patterns_ref,
        pattern_replacement,
    )
    .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Compiled redaction patterns held across calls to avoid per-record recompilation.
///
/// Used by standalone rendering helpers and benchmarks. Production logging
/// uses `RuntimeConfig`; invalid patterns raise `ValueError` at construction.
#[pyclass(name = "RedactionConfig")]
pub(crate) struct RedactionConfig {
    pub(crate) patterns: Vec<RedactionPattern>,
    pub(crate) replacement: String,
}

#[pymethods]
impl RedactionConfig {
    #[new]
    #[pyo3(signature = (patterns, replacement=None, allow_backtracking=false))]
    fn new(
        patterns: Vec<String>,
        replacement: Option<String>,
        allow_backtracking: bool,
    ) -> PyResult<Self> {
        let compiled = patterns
            .iter()
            .enumerate()
            .map(|(index, p)| compile_pattern(p, allow_backtracking, index))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            patterns: compiled,
            replacement: replacement.unwrap_or_else(|| "[REDACTED]".to_owned()),
        })
    }

    /// Number of compiled patterns.
    #[getter]
    fn len(&self) -> usize {
        self.patterns.len()
    }

    fn __bool__(&self) -> bool {
        !self.patterns.is_empty()
    }
}

/// Hot-path render using a pre-built `RedactionConfig` (patterns compiled once).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, config, timestamp=None, sensitive_keys=None, stack=None))]
pub(crate) fn render_line_with_config(
    fields: &Bound<'_, PyDict>,
    logger: &Bound<'_, PyString>,
    level: &str,
    service: &Bound<'_, PyString>,
    message: &Bound<'_, PyString>,
    config: &RedactionConfig,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    stack: Option<&Bound<'_, PyString>>,
) -> PyResult<String> {
    let entries = convert_fields(fields)?;
    let (logger, service, message, stack) = text_arguments(logger, service, message, stack);
    let (logger, service, message, stack) = (
        logger.as_str(),
        service.as_str(),
        message.as_str(),
        stack.as_deref(),
    );
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let patterns_ref = if config.patterns.is_empty() {
        None
    } else {
        Some(config.patterns.as_slice())
    };
    structguru_core::render_line(
        entries,
        logger,
        level,
        service,
        message,
        timestamp,
        stack,
        sensitive_keys.as_deref(),
        patterns_ref,
        Some(&config.replacement),
    )
    .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Render a colored, human-readable console line (patterns compiled per call).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, colors, timestamp=None, sensitive_keys=None, sensitive_patterns=None, stack=None, pattern_replacement=None))]
pub(crate) fn render_line_console(
    fields: &Bound<'_, PyDict>,
    logger: &Bound<'_, PyString>,
    level: &str,
    service: &Bound<'_, PyString>,
    message: &Bound<'_, PyString>,
    colors: bool,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<Vec<String>>,
    stack: Option<&Bound<'_, PyString>>,
    pattern_replacement: Option<&str>,
) -> PyResult<String> {
    let entries = convert_fields(fields)?;
    let (logger, service, message, stack) = text_arguments(logger, service, message, stack);
    let (logger, service, message, stack) = (
        logger.as_str(),
        service.as_str(),
        message.as_str(),
        stack.as_deref(),
    );
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let compiled: Vec<RedactionPattern> = match &sensitive_patterns {
        Some(patterns) => patterns
            .iter()
            .enumerate()
            .map(|(index, p)| compile_pattern(p, false, index))
            .collect::<PyResult<Vec<_>>>()?,
        None => Vec::new(),
    };
    let patterns_ref = if compiled.is_empty() {
        None
    } else {
        Some(compiled.as_slice())
    };
    Ok(structguru_core::render_line_console(
        entries,
        logger,
        level,
        service,
        message,
        colors,
        timestamp,
        sensitive_keys.as_deref(),
        patterns_ref,
        pattern_replacement,
        stack,
    ))
}

/// Hot-path console render using a pre-built `RedactionConfig`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, colors, config, timestamp=None, sensitive_keys=None, stack=None))]
pub(crate) fn render_console_with_config(
    fields: &Bound<'_, PyDict>,
    logger: &Bound<'_, PyString>,
    level: &str,
    service: &Bound<'_, PyString>,
    message: &Bound<'_, PyString>,
    colors: bool,
    config: &RedactionConfig,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    stack: Option<&Bound<'_, PyString>>,
) -> PyResult<String> {
    let entries = convert_fields(fields)?;
    let (logger, service, message, stack) = text_arguments(logger, service, message, stack);
    let (logger, service, message, stack) = (
        logger.as_str(),
        service.as_str(),
        message.as_str(),
        stack.as_deref(),
    );
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let patterns_ref = if config.patterns.is_empty() {
        None
    } else {
        Some(config.patterns.as_slice())
    };
    Ok(structguru_core::render_line_console(
        entries,
        logger,
        level,
        service,
        message,
        colors,
        timestamp,
        sensitive_keys.as_deref(),
        patterns_ref,
        Some(&config.replacement),
        stack,
    ))
}

/// Render and delivery policy shared by every record of one configuration.
///
/// Built once by `configure()` and passed to the fused `log` entry points on
/// `_NativeStringWriter`, so the service name, redaction keys, compiled
/// patterns, output format, and overflow policy are never re-marshalled per
/// record. Frozen: a record reads it without a borrow check, and a level-only
/// update shares it between runtime snapshots.
#[pyclass(name = "RuntimeConfig", frozen)]
pub(crate) struct RuntimeConfig {
    service: String,
    sensitive_keys: Option<Vec<String>>,
    pub(crate) patterns: Vec<RedactionPattern>,
    pub(crate) replacement: String,
    console: bool,
    colors: bool,
    pub(crate) blocking: bool,
}

impl RuntimeConfig {
    fn patterns(&self) -> Option<&[RedactionPattern]> {
        (!self.patterns.is_empty()).then_some(self.patterns.as_slice())
    }

    fn render_json(
        &self,
        entries: Vec<(String, Value)>,
        logger: &str,
        level: &str,
        message: &str,
        stack: Option<&str>,
        timestamp: &str,
    ) -> PyResult<String> {
        structguru_core::render_line(
            entries,
            logger,
            level,
            &self.service,
            message,
            timestamp,
            stack,
            self.sensitive_keys.as_deref(),
            self.patterns(),
            Some(&self.replacement),
        )
        .map_err(|err| PyValueError::new_err(err.to_string()))
    }

    fn render_console(
        &self,
        entries: Vec<(String, Value)>,
        logger: &str,
        level: &str,
        message: &str,
        stack: Option<&str>,
        timestamp: &str,
    ) -> String {
        structguru_core::render_line_console(
            entries,
            logger,
            level,
            &self.service,
            message,
            self.colors,
            timestamp,
            self.sensitive_keys.as_deref(),
            self.patterns(),
            Some(&self.replacement),
            stack,
        )
    }
}

#[pymethods]
impl RuntimeConfig {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        service,
        sensitive_keys=None,
        sensitive_patterns=None,
        pattern_replacement=None,
        allow_backtracking=false,
        console=false,
        colors=false,
        blocking=true,
    ))]
    fn new(
        service: &Bound<'_, PyString>,
        sensitive_keys: Option<Vec<String>>,
        sensitive_patterns: Option<Vec<String>>,
        pattern_replacement: Option<String>,
        allow_backtracking: bool,
        console: bool,
        colors: bool,
        blocking: bool,
    ) -> PyResult<Self> {
        let patterns = sensitive_patterns
            .unwrap_or_default()
            .iter()
            .enumerate()
            .map(|(index, pattern)| compile_pattern(pattern, allow_backtracking, index))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            service: string_to_owned(service),
            sensitive_keys,
            patterns,
            replacement: pattern_replacement.unwrap_or_else(|| "[REDACTED]".to_owned()),
            console,
            colors,
            blocking,
        })
    }

    #[getter]
    fn service(&self) -> &str {
        &self.service
    }

    #[getter]
    fn console(&self) -> bool {
        self.console
    }

    #[getter]
    fn colors(&self) -> bool {
        self.colors
    }

    #[getter]
    fn blocking(&self) -> bool {
        self.blocking
    }

    /// Number of compiled value-redaction patterns.
    #[getter]
    fn pattern_count(&self) -> usize {
        self.patterns.len()
    }

    /// Apply the compiled value patterns to `text` in declaration order, as
    /// the renderer does to every string field.
    ///
    /// Structured exception locals call this on each whole rendering before
    /// truncating it, so a match that straddles the cut is still redacted.
    fn redact_patterns(&self, text: &str) -> String {
        self.patterns
            .iter()
            .fold(text.to_owned(), |current, pattern| {
                pattern
                    .replace_all(&current, &self.replacement)
                    .into_owned()
            })
    }
}

/// Merge, redact, and render one record in the configured format.
///
/// Returns the line with its trailing newline and, when `json_copy` is set,
/// the redacted JSON record for the Sentry hook: the line itself in JSON mode,
/// a second render in console mode.
#[allow(clippy::too_many_arguments)]
pub(crate) fn render_record(
    config: &RuntimeConfig,
    logger: &Bound<'_, PyString>,
    level: &str,
    message: &Bound<'_, PyString>,
    bound: &Bound<'_, PyDict>,
    kwargs: &Bound<'_, PyDict>,
    contextvars: &Bound<'_, PyDict>,
    exception: Option<&Bound<'_, PyAny>>,
    stack: Option<&Bound<'_, PyString>>,
    json_copy: bool,
    created: Option<&Bound<'_, PyAny>>,
) -> PyResult<(String, Option<String>)> {
    let entries = merge_record_fields(bound, kwargs, contextvars, exception)?;
    // A filter or record factory may set `created` to anything; a value that
    // is not a representable number stamps the current time instead.
    let created = created.and_then(|value| value.extract::<f64>().ok());
    let logger = string_to_cow(logger);
    let message = string_to_cow(message);
    let stack = stack.map(string_to_cow);
    let stack = stack.as_deref();
    let timestamp = created
        .and_then(structguru_core::unix_to_iso8601)
        .unwrap_or_else(structguru_core::now_iso8601);
    if config.console {
        let json = if json_copy {
            Some(config.render_json(
                entries.clone(),
                &logger,
                level,
                &message,
                stack,
                &timestamp,
            )?)
        } else {
            None
        };
        let mut line = config.render_console(entries, &logger, level, &message, stack, &timestamp);
        line.push('\n');
        return Ok((line, json));
    }
    let mut line = config.render_json(entries, &logger, level, &message, stack, &timestamp)?;
    let json = json_copy.then(|| line.clone());
    line.push('\n');
    Ok((line, json))
}
