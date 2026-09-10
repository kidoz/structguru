use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString, PyTuple,
    PyTupleMethods,
};
use std::borrow::Cow;
use std::time::Duration;
use structguru_core::{Pipeline, RedactionPattern, StringWriter, Value};

const MAX_VALUE_DEPTH: usize = 64;
type ContainerStack = Vec<usize>;

#[pyfunction]
fn version() -> &'static str {
    structguru_core::version()
}

#[pyfunction]
fn normalize_level(level: &str) -> String {
    structguru_core::normalize_level(level)
}

#[pyfunction]
fn syslog_severity(canonical_level: &str) -> u8 {
    structguru_core::syslog_severity(canonical_level)
}

#[pyfunction]
fn normalized_syslog_severity(level: &str) -> u8 {
    structguru_core::normalized_syslog_severity(level)
}

/// The core's level table: `(method name, numeric level)` pairs in display
/// order, with the canonical method first for each number.
#[pyfunction]
fn level_table() -> Vec<(&'static str, u8)> {
    structguru_core::LEVEL_TABLE.to_vec()
}

#[pyfunction]
fn _convert_value_debug<'py>(
    py: Python<'py>,
    obj: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    value_to_py(py, &convert_py_value(&obj)?)
}

#[pyfunction]
fn _conversion_stats<'py>(py: Python<'py>, obj: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let stats = convert_py_value(&obj)?.stats();
    let result = PyDict::new(py);
    result.set_item("nodes", stats.nodes)?;
    result.set_item("max_depth", stats.max_depth)?;
    Ok(result)
}

#[pyfunction]
fn _render_json_debug(obj: Bound<'_, PyAny>) -> PyResult<String> {
    convert_py_value(&obj)?
        .to_json_string()
        .map_err(|err| PyValueError::new_err(err.to_string()))
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
fn validate_patterns(patterns: Vec<String>, allow_backtracking: bool) -> PyResult<usize> {
    for p in &patterns {
        RedactionPattern::compile(p, allow_backtracking).map_err(PyValueError::new_err)?;
    }
    Ok(patterns.len())
}

/// Render one log line: convert + redact `fields`, append the standard keys, emit JSON.
///
/// This is the standalone form: patterns (if any) are recompiled per call. For
/// the hot path, prefer `render_line_with_config` + `RedactionConfig`, which
/// compile patterns once at enable time.
/// Convert a Python field mapping into owned `(key, Value)` entries for the core renderer.
fn convert_fields(fields: &Bound<'_, PyDict>) -> PyResult<Vec<(String, Value)>> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    for item in fields.items().iter() {
        let pair = item.cast::<PyTuple>()?;
        let key = pair.get_item(0)?;
        let value = pair.get_item(1)?;
        let key = string_to_owned(
            key.cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("field keys must be strings"))?,
        );
        entries.push((key, convert_py_value(&value)?));
    }
    Ok(entries)
}

/// Call keywords that steer the record and are never structured fields.
const RESERVED_FIELD_KEYS: [&str; 2] = ["exc_info", "stack_info"];

fn field_key(key: &Bound<'_, PyAny>) -> PyResult<String> {
    key.cast::<PyString>()
        .map(string_to_owned)
        .map_err(|_| PyTypeError::new_err("field keys must be strings"))
}

/// Replace the value of an existing `key` in place, or append the entry.
fn upsert_entry(entries: &mut Vec<(String, Value)>, key: String, value: Value) {
    match entries.iter_mut().find(|(existing, _)| *existing == key) {
        Some((_, slot)) => *slot = value,
        None => entries.push((key, value)),
    }
}

/// Merge a record's field sources into converted entries.
///
/// Reproduces the facade's dict merge: `{**bound, **kwargs}` keeps a bound
/// key's position while taking the kwargs value, contextvars only fill keys
/// that are still absent (`setdefault`), and `exception` is assigned last.
///
/// Each source dict is iterated with no Python code running in between: its
/// pairs are collected before any value is converted, because conversion may
/// run `isoformat()` or a dataclass property. The library never mutates a
/// source in place — `bind()` replaces `_bound`, `kwargs` is private to the
/// call, and the contextvars mapping is copy-on-write — so the iteration can
/// never observe a size change.
fn merge_record_fields(
    bound: &Bound<'_, PyDict>,
    kwargs: &Bound<'_, PyDict>,
    contextvars: &Bound<'_, PyDict>,
    exception: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<(String, Value)>> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(
        bound.len() + kwargs.len() + contextvars.len() + usize::from(exception.is_some()),
    );
    let mut pairs: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)> =
        Vec::with_capacity(bound.len().max(kwargs.len()).max(contextvars.len()));

    // Bound keys are unique, so no position lookup is needed.
    pairs.extend(bound.iter());
    for (key, value) in pairs.drain(..) {
        entries.push((field_key(&key)?, convert_py_value(&value)?));
    }
    pairs.extend(kwargs.iter());
    for (key, value) in pairs.drain(..) {
        let key = field_key(&key)?;
        if RESERVED_FIELD_KEYS.contains(&key.as_str()) {
            continue;
        }
        let value = convert_py_value(&value)?;
        upsert_entry(&mut entries, key, value);
    }
    pairs.extend(contextvars.iter());
    for (key, value) in pairs.drain(..) {
        let key = field_key(&key)?;
        if entries.iter().any(|(existing, _)| *existing == key) {
            continue;
        }
        let value = convert_py_value(&value)?;
        entries.push((key, value));
    }
    if let Some(exception) = exception {
        let value = convert_py_value(exception)?;
        upsert_entry(&mut entries, "exception".to_owned(), value);
    }
    Ok(entries)
}

/// Merge a record's field sources into a Python dict.
///
/// Same semantics as the merge inside the fused `log` entry points, for the
/// path where a Python hook (OpenTelemetry enrichment, the metric or Sentry
/// processor) needs the merged mapping before rendering.
#[pyfunction]
fn merge_fields<'py>(
    bound: &Bound<'py, PyDict>,
    kwargs: &Bound<'py, PyDict>,
    contextvars: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let merged = bound.copy()?;
    for (key, value) in kwargs.iter() {
        if let Ok(text) = key.cast::<PyString>()
            && let Ok(text) = text.to_str()
            && RESERVED_FIELD_KEYS.contains(&text)
        {
            continue;
        }
        merged.set_item(key, value)?;
    }
    for (key, value) in contextvars.iter() {
        if !merged.contains(&key)? {
            merged.set_item(key, value)?;
        }
    }
    Ok(merged)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, timestamp=None, sensitive_keys=None, sensitive_patterns=None, stack=None, pattern_replacement=None))]
fn render_line(
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
            .map(|p| RedactionPattern::linear(p))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?,
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
/// Built once at `configure()` time and passed to `render_line_with_config`
/// on the hot path. If a pattern fails to compile, construction raises
/// `ValueError` so the Python bridge can fall back to the standard path.
#[pyclass(name = "RedactionConfig")]
struct RedactionConfig {
    patterns: Vec<RedactionPattern>,
    replacement: String,
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
            .map(|p| RedactionPattern::compile(p, allow_backtracking))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?;
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
fn render_line_with_config(
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
fn render_line_console(
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
            .map(|p| RedactionPattern::linear(p))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?,
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
fn render_console_with_config(
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
struct RuntimeConfig {
    service: String,
    sensitive_keys: Option<Vec<String>>,
    patterns: Vec<RedactionPattern>,
    replacement: String,
    console: bool,
    colors: bool,
    blocking: bool,
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
            .map(|pattern| RedactionPattern::compile(pattern, allow_backtracking))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?;
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
}

/// Merge, redact, and render one record in the configured format.
///
/// Returns the line with its trailing newline and, when `json_copy` is set,
/// the redacted JSON record for the Sentry hook: the line itself in JSON mode,
/// a second render in console mode.
#[allow(clippy::too_many_arguments)]
fn render_record(
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
) -> PyResult<(String, Option<String>)> {
    let entries = merge_record_fields(bound, kwargs, contextvars, exception)?;
    let logger = string_to_cow(logger);
    let message = string_to_cow(message);
    let stack = stack.map(string_to_cow);
    let stack = stack.as_deref();
    let timestamp = structguru_core::now_iso8601();
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

#[pyclass(name = "_NativeStringWriter")]
struct NativeStringWriter {
    writer: StringWriter,
}

/// Result of handing one line to the queue, observed under the queue lock.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Outcome {
    Accepted = 0,
    Full = 1,
    Closed = 2,
}

impl Outcome {
    /// Text form returned by the `*_outcome` writer methods.
    fn as_str(self) -> &'static str {
        match self {
            Outcome::Accepted => "accepted",
            Outcome::Full => "full",
            Outcome::Closed => "closed",
        }
    }

    /// Numeric form returned by the fused `log` entry points: 0 accepted,
    /// 1 full (dropped), 2 closed (rejected by a retired writer).
    fn code(self) -> u8 {
        self as u8
    }
}

impl NativeStringWriter {
    fn enqueue_owned(&self, py: Python<'_>, message: String, blocking: bool) -> Outcome {
        if !blocking {
            return match self.writer.try_enqueue_with_reason(message) {
                Ok(()) => Outcome::Accepted,
                Err(structguru_core::EnqueueError::Full(_)) => Outcome::Full,
                Err(structguru_core::EnqueueError::Closed(_)) => Outcome::Closed,
            };
        }
        // Keep the uncontended path attached; only a full/closed queue needs
        // the blocking path, whose sole rejection reason is closure.
        match self.writer.enqueue_if_space(message) {
            Ok(()) => Outcome::Accepted,
            Err(message) => {
                if py.detach(|| self.writer.enqueue_blocking(message).is_ok()) {
                    Outcome::Accepted
                } else {
                    Outcome::Closed
                }
            }
        }
    }
}

#[pymethods]
impl NativeStringWriter {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        maxsize,
        paused=false,
        fail_after=None,
        target="memory",
        file_path=None,
        file_max_bytes=0,
        file_backup_count=0,
        also_stdout=false,
    ))]
    fn new(
        py: Python<'_>,
        maxsize: usize,
        paused: bool,
        fail_after: Option<usize>,
        target: &str,
        file_path: Option<String>,
        file_max_bytes: usize,
        file_backup_count: usize,
        also_stdout: bool,
    ) -> PyResult<Self> {
        use structguru_core::{MultiSink, RotatingFileSink, SinkError, StringSink, WriteSink};

        // File creation and sidecar lock acquisition can block. Keep only
        // Rust-owned arguments across detachment so other Python threads can
        // keep running (and logging through the current runtime) during setup.
        let target = target.to_owned();
        let writer = py
            .detach(move || -> Result<StringWriter, SinkError> {
                // file_path drives real output; also_stdout mirrors to stdout.
                // Otherwise target selects stdout or a memory/test sink.
                let mut sinks: Vec<Box<dyn StringSink>> = Vec::new();

                if let Some(path) = &file_path {
                    let file = RotatingFileSink::new(path, file_max_bytes, file_backup_count)?;
                    sinks.push(Box::new(file));
                }
                if also_stdout {
                    sinks.push(Box::new(WriteSink::new(std::io::stdout())));
                }

                let writer = if !sinks.is_empty() {
                    if sinks.len() == 1 {
                        StringWriter::with_boxed_sink(maxsize, sinks.pop().unwrap())
                    } else {
                        StringWriter::with_boxed_sink(maxsize, Box::new(MultiSink::new(sinks)))
                    }
                } else {
                    match target.as_str() {
                        "stdout" => StringWriter::new_stdout(maxsize),
                        "null" => StringWriter::new_null(maxsize),
                        "memory" => {
                            if let Some(fail_after) = fail_after {
                                StringWriter::new_failing(maxsize, fail_after, paused)
                            } else if paused {
                                StringWriter::new_paused(maxsize)
                            } else {
                                StringWriter::new(maxsize)
                            }
                        }
                        other => {
                            return Err(SinkError::new(format!("unknown writer target: {other}")));
                        }
                    }
                };
                Ok(writer)
            })
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
        Ok(Self { writer })
    }

    #[getter]
    fn maxsize(&self) -> usize {
        self.writer.maxsize()
    }

    fn try_enqueue(&self, message: &str) -> bool {
        self.writer.try_enqueue(message.to_owned()).is_ok()
    }

    fn enqueue_blocking(&self, py: Python<'_>, message: &str) -> bool {
        let message = message.to_owned();
        // The queue normally has room, and the push is a mutex plus a
        // VecDeque append: detaching from the GIL for it costs more than the
        // push itself and forces a GIL hand-off per record when other threads
        // are runnable. Only give the GIL up for the wait on a full queue.
        match self.writer.enqueue_if_space(message) {
            Ok(()) => true,
            Err(message) => py.detach(|| self.writer.enqueue_blocking(message).is_ok()),
        }
    }

    /// Return the enqueue outcome without racing a later close or reconfigure.
    fn enqueue_outcome(&self, py: Python<'_>, message: &str, blocking: bool) -> &'static str {
        self.enqueue_owned(py, message.to_owned(), blocking)
            .as_str()
    }

    /// Render one JSON record and hand it to the writer in a single call.
    ///
    /// Renders exactly like `render_line_with_config` (when `config` is given)
    /// or `render_line` (otherwise), appends the newline, and pushes the line
    /// into the queue without ever materialising a Python string. `blocking`
    /// selects `enqueue_blocking` semantics (wait for space with the GIL
    /// released) over `try_enqueue` (a full queue counts as a drop). Returns
    /// whether the line was accepted.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (fields, logger, level, service, message, blocking, config=None, sensitive_keys=None, stack=None, timestamp=None))]
    fn render_enqueue_json(
        &self,
        py: Python<'_>,
        fields: &Bound<'_, PyDict>,
        logger: &Bound<'_, PyString>,
        level: &str,
        service: &Bound<'_, PyString>,
        message: &Bound<'_, PyString>,
        blocking: bool,
        config: Option<&RedactionConfig>,
        sensitive_keys: Option<Vec<String>>,
        stack: Option<&Bound<'_, PyString>>,
        timestamp: Option<&str>,
    ) -> PyResult<bool> {
        Ok(self.render_enqueue_json_outcome(
            py,
            fields,
            logger,
            level,
            service,
            message,
            blocking,
            config,
            sensitive_keys,
            stack,
            timestamp,
        )? == "accepted")
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (fields, logger, level, service, message, blocking, config=None, sensitive_keys=None, stack=None, timestamp=None))]
    fn render_enqueue_json_outcome(
        &self,
        py: Python<'_>,
        fields: &Bound<'_, PyDict>,
        logger: &Bound<'_, PyString>,
        level: &str,
        service: &Bound<'_, PyString>,
        message: &Bound<'_, PyString>,
        blocking: bool,
        config: Option<&RedactionConfig>,
        sensitive_keys: Option<Vec<String>>,
        stack: Option<&Bound<'_, PyString>>,
        timestamp: Option<&str>,
    ) -> PyResult<&'static str> {
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
        let (patterns, replacement) = match config {
            Some(config) if !config.patterns.is_empty() => (
                Some(config.patterns.as_slice()),
                Some(config.replacement.as_str()),
            ),
            Some(config) => (None, Some(config.replacement.as_str())),
            None => (None, None),
        };
        let mut line = structguru_core::render_line(
            entries,
            logger,
            level,
            service,
            message,
            timestamp,
            stack,
            sensitive_keys.as_deref(),
            patterns,
            replacement,
        )
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
        line.push('\n');
        Ok(self.enqueue_owned(py, line, blocking).as_str())
    }

    /// Merge, render, and enqueue one record in a single call.
    ///
    /// Fields merge with the facade's semantics: bound fields first, then call
    /// kwargs (overriding a bound key in place; `exc_info` and `stack_info`
    /// are never fields), then contextvars for keys still absent, then
    /// `exception` as the `exception` field. The record renders in `config`'s
    /// format and is pushed without ever becoming a Python string. Returns the
    /// outcome code: 0 accepted, 1 full, 2 closed.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (config, logger, level, message, bound, kwargs, contextvars, exception, stack))]
    fn log(
        &self,
        py: Python<'_>,
        config: &Bound<'_, RuntimeConfig>,
        logger: &Bound<'_, PyString>,
        level: &str,
        message: &Bound<'_, PyString>,
        bound: &Bound<'_, PyDict>,
        kwargs: &Bound<'_, PyDict>,
        contextvars: &Bound<'_, PyDict>,
        exception: Option<&Bound<'_, PyAny>>,
        stack: Option<&Bound<'_, PyString>>,
    ) -> PyResult<u8> {
        let config = config.get();
        let (line, _) = render_record(
            config,
            logger,
            level,
            message,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            false,
        )?;
        Ok(self.enqueue_owned(py, line, config.blocking).code())
    }

    /// Like [`log`](Self::log), returning the rendered line as well.
    ///
    /// Returns `(outcome, line, json)`: `line` keeps its trailing newline and
    /// is what stream and callable sinks receive; `json` is the redacted JSON
    /// record for the Sentry hook when `json_copy` is set, rendered separately
    /// in console mode.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (config, logger, level, message, bound, kwargs, contextvars, exception, stack, json_copy))]
    fn log_capture(
        &self,
        py: Python<'_>,
        config: &Bound<'_, RuntimeConfig>,
        logger: &Bound<'_, PyString>,
        level: &str,
        message: &Bound<'_, PyString>,
        bound: &Bound<'_, PyDict>,
        kwargs: &Bound<'_, PyDict>,
        contextvars: &Bound<'_, PyDict>,
        exception: Option<&Bound<'_, PyAny>>,
        stack: Option<&Bound<'_, PyString>>,
        json_copy: bool,
    ) -> PyResult<(u8, String, Option<String>)> {
        let config = config.get();
        let (line, json) = render_record(
            config,
            logger,
            level,
            message,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            json_copy,
        )?;
        let outcome = self.enqueue_owned(py, line.clone(), config.blocking);
        Ok((outcome.code(), line, json))
    }

    fn flush(&self, py: Python<'_>) {
        py.detach(|| self.writer.flush());
    }

    fn close(&self, py: Python<'_>) {
        py.detach(|| self.writer.close());
    }

    fn resume(&self) {
        self.writer.resume();
    }

    /// Neutralize after a fork; the caller replaces this with a fresh writer.
    fn abandon(&self) {
        self.writer.abandon();
    }

    fn messages(&self) -> Vec<String> {
        self.writer.messages()
    }

    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let metrics = self.writer.metrics();
        let result = PyDict::new(py);
        result.set_item("enqueued", metrics.enqueued)?;
        result.set_item("dropped", metrics.dropped)?;
        result.set_item("dequeued", metrics.dequeued)?;
        result.set_item("written", metrics.written)?;
        result.set_item("sink_errors", metrics.sink_errors)?;
        result.set_item("depth", metrics.depth)?;
        result.set_item("maxsize", metrics.maxsize)?;
        result.set_item("in_flight", metrics.in_flight)?;
        result.set_item("closed", metrics.closed)?;
        result.set_item("worker_done", metrics.worker_done)?;
        result.set_item("paused", metrics.paused)?;
        Ok(result)
    }
}

/// Composed pre-render filter (sampling + rate limiting) in native Rust.
///
/// Returns `True` when the record should be rendered. Dropped records never
/// reach `render_line`, so they cost zero rendering. Drop counters are kept
/// distinct from the writer's transport `dropped` counter.
#[pyclass(name = "NativeFilter")]
struct NativeFilter {
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
    /// `level` is the canonical method name.
    #[pyo3(signature = (key, level))]
    fn allow(&self, key: &Bound<'_, PyString>, level: &str) -> bool {
        // Filter the same text the renderer emits, including surrogate
        // replacement, while retaining a borrowed fast path for valid UTF-8.
        match key.to_str() {
            Ok(key) => self.pipeline.allow(key, level),
            Err(_) => self.pipeline.allow(&key.to_string_lossy(), level),
        }
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

/// Marker emitted in place of a value nested deeper than `MAX_VALUE_DEPTH`.
const DEPTH_MARKER: &str = "<max depth exceeded>";

fn convert_py_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let mut containers = ContainerStack::with_capacity(8);
    convert_py_value_inner(obj, 1, &mut containers)
}

/// Convert one Python object into an owned [`Value`].
///
/// Conversion is total for value-shape problems: a value the renderer cannot
/// represent becomes a marker string — `<unsupported: T>`, `<cycle: T>`, or
/// `<max depth exceeded>` — instead of failing the whole record. A logging
/// call therefore never raises, and a bridged stdlib record is never lost,
/// because of one field. Only a `BaseException` that is not an `Exception`
/// (`KeyboardInterrupt`, `SystemExit`) still propagates.
fn convert_py_value_inner(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    if depth > MAX_VALUE_DEPTH {
        return Ok(Value::String(DEPTH_MARKER.to_owned()));
    }

    if obj.is_none() {
        return Ok(Value::Null);
    }
    if obj.is_exact_instance_of::<PyBool>() {
        return Ok(Value::Bool(obj.extract()?));
    }
    if obj.is_exact_instance_of::<PyInt>() {
        return match obj.extract::<i64>() {
            Ok(value) => Ok(Value::Int(value)),
            // Outside i64: emit the decimal digits verbatim as a JSON number so
            // the value stays lossless, as `json.dumps` does.
            Err(_) => recover(obj, convert_big_int(obj)),
        };
    }
    if obj.is_exact_instance_of::<PyFloat>() {
        let value: f64 = obj.extract()?;
        // orjson emits `null` for NaN/Infinity; serde_json would error, so map here.
        return Ok(if value.is_finite() {
            Value::Float(value)
        } else {
            Value::Null
        });
    }
    if let Ok(value) = obj.cast::<PyString>() {
        return Ok(Value::String(string_to_owned(value)));
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        let Some(container_id) = enter_container(obj, containers) else {
            return Ok(marker("cycle", obj));
        };
        let result = convert_dict(dict, depth, containers);
        leave_container(container_id, containers);
        return result;
    }
    if let Ok(list) = obj.cast::<PyList>() {
        let Some(container_id) = enter_container(obj, containers) else {
            return Ok(marker("cycle", obj));
        };
        let result = convert_items(list.iter(), list.len(), depth, containers);
        leave_container(container_id, containers);
        return result;
    }
    if let Ok(tuple) = obj.cast::<PyTuple>() {
        let Some(container_id) = enter_container(obj, containers) else {
            return Ok(marker("cycle", obj));
        };
        let result = convert_items(tuple.iter(), tuple.len(), depth, containers);
        leave_container(container_id, containers);
        return result;
    }

    // Exotic leaves (datetime/date/UUID/Enum/dataclass) delegate to the
    // object's own Python conversion. A conversion that raises — a duck-typed
    // `isoformat()` that fails, a dataclass field whose access raises — must
    // not lose the record either, so it collapses to the unsupported marker.
    recover(obj, convert_exotic_leaf(obj, depth, containers))
}

/// Map a Python `Exception` raised while converting `obj` to the
/// `<unsupported: T>` marker. Anything that is not an `Exception`
/// (`KeyboardInterrupt`, `SystemExit`) propagates untouched.
fn recover(obj: &Bound<'_, PyAny>, result: PyResult<Value>) -> PyResult<Value> {
    match result {
        Err(err) if err.is_instance_of::<PyException>(obj.py()) => Ok(marker("unsupported", obj)),
        other => other,
    }
}

/// `<kind: TypeName>` — the text of a fallback marker for `obj`.
fn marker_text(kind: &str, obj: &Bound<'_, PyAny>) -> String {
    format!("<{kind}: {}>", type_name(obj))
}

fn marker(kind: &str, obj: &Bound<'_, PyAny>) -> Value {
    Value::String(marker_text(kind, obj))
}

fn type_name(obj: &Bound<'_, PyAny>) -> String {
    obj.get_type()
        .name()
        .map(|name| name.to_string())
        .unwrap_or_else(|_| "unknown".to_owned())
}

/// Owned copies of a record's text arguments under the same policy as field
/// values: an unpaired surrogate becomes U+FFFD instead of failing the record.
/// `os.fsdecode()` output and stdlib messages built from it reach every entry
/// point this way, so a message can never be rejected where a field is not.
fn text_arguments(
    logger: &Bound<'_, PyString>,
    service: &Bound<'_, PyString>,
    message: &Bound<'_, PyString>,
    stack: Option<&Bound<'_, PyString>>,
) -> (String, String, String, Option<String>) {
    (
        string_to_owned(logger),
        string_to_owned(service),
        string_to_owned(message),
        stack.map(string_to_owned),
    )
}

/// Copy a Python string, replacing unpaired surrogates (not representable in
/// UTF-8, so `to_str` rejects them) with U+FFFD instead of failing the record.
fn string_to_owned(value: &Bound<'_, PyString>) -> String {
    string_to_cow(value).into_owned()
}

/// Borrow a Python string's UTF-8 text, copying only to replace unpaired
/// surrogates with U+FFFD (the [`string_to_owned`] policy without the copy).
fn string_to_cow<'a>(value: &'a Bound<'_, PyString>) -> Cow<'a, str> {
    match value.to_str() {
        Ok(text) => Cow::Borrowed(text),
        Err(_) => value.to_string_lossy(),
    }
}

fn convert_big_int(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    // `str()` of an exact int is `-?[0-9]+`, which is valid JSON. It can still
    // raise `ValueError` past `sys.get_int_max_str_digits()`; `recover` turns
    // that into the marker.
    let digits = obj.str()?;
    Ok(Value::Raw(string_to_owned(&digits)))
}

fn convert_dict(
    dict: &Bound<'_, PyDict>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    let mut entries = Vec::with_capacity(dict.len());
    // Snapshot before conversion invokes Python (isoformat/dataclass properties
    // can mutate this dict, including from another free-threaded worker).
    // Iterating the live dict would panic when its size changes.
    for item in dict.items().iter() {
        let pair = item.cast::<PyTuple>()?;
        let key = pair.get_item(0)?;
        let value = pair.get_item(1)?;
        let key = convert_map_key(&key, depth, containers)?;
        entries.push((key, convert_py_value_inner(&value, depth + 1, containers)?));
    }
    Ok(Value::Map(entries))
}

/// Render a mapping key as the string JSON requires.
///
/// Strings are used as-is. Any other key goes through the value conversion: a
/// string result (Enum, datetime, UUID) is used directly, a scalar result uses
/// its JSON text (`1`, `1.5`, `true`, `null`), and a container result — a
/// tuple, a dataclass — becomes `<unsupported: T>`.
fn convert_map_key(
    key: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<String> {
    if let Ok(text) = key.cast::<PyString>() {
        return Ok(string_to_owned(text));
    }
    Ok(match convert_py_value_inner(key, depth + 1, containers)? {
        Value::String(text) => text,
        Value::List(_) | Value::Map(_) => marker_text("unsupported", key),
        scalar => scalar
            .to_json_string()
            .unwrap_or_else(|_| marker_text("unsupported", key)),
    })
}

fn convert_items<'py>(
    items: impl Iterator<Item = Bound<'py, PyAny>>,
    capacity: usize,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    let mut values = Vec::with_capacity(capacity);
    for item in items {
        values.push(convert_py_value_inner(&item, depth + 1, containers)?);
    }
    Ok(Value::List(values))
}

/// Handle exotic Python leaves (datetime, date, UUID, Enum, dataclass) natively
/// without crossing into orjson. Anything else becomes `<unsupported: T>`.
///
/// Every probe looks at the object's *type*, never the instance: an instance
/// lookup would run an arbitrary `__getattr__` — Django's `LazyObject`
/// evaluates itself, an ORM proxy may hit the database — for every unsupported
/// object that reaches the renderer.
fn convert_exotic_leaf(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    let type_object = obj.get_type();

    // Enum: the EnumType metaclass exposes `__members__` on the class; the
    // member's payload is its `.value`.
    if type_object.hasattr("__members__")? {
        let value = obj.getattr("value")?;
        // Recurse with depth + 1 like every other container path: an enum whose
        // `.value` cycles back (e.g. a member whose `_value_` is itself) would
        // otherwise recurse forever at constant depth, bypass MAX_VALUE_DEPTH,
        // and overflow the native stack into an uncatchable process abort.
        return convert_py_value_inner(&value, depth + 1, containers);
    }

    // datetime.datetime / datetime.date / datetime.time: `isoformat()` gives
    // the ISO 8601 text. (datetime is a subclass of date, so both are covered.)
    if type_object.hasattr("isoformat")? {
        let iso = obj.call_method0("isoformat")?;
        return Ok(Value::String(string_to_owned(iso.cast::<PyString>()?)));
    }

    // uuid.UUID: has both .hex and .int attributes; str() gives canonical form.
    // The type name check avoids misdetecting objects that happen to have both.
    if type_object.hasattr("hex")? && type_object.hasattr("int")? && type_name(obj) == "UUID" {
        return Ok(Value::String(string_to_owned(&obj.str()?)));
    }

    // dataclass: use dataclasses.fields() rather than __dict__, so slots=True
    // dataclasses and inherited fields follow Python's canonical field order.
    if type_object.hasattr("__dataclass_fields__")? {
        let Some(container_id) = enter_container(obj, containers) else {
            return Ok(marker("cycle", obj));
        };
        let result = convert_dataclass(obj, depth, containers);
        leave_container(container_id, containers);
        return result;
    }

    // Unsupported type (Decimal/bytes/set/timedelta/Path/request objects/...):
    // a type marker, never `str()`/`repr()`, so nothing the object holds —
    // headers, cookies, bodies — can leak into the log line.
    Ok(marker("unsupported", obj))
}

fn convert_dataclass(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    let dataclasses = obj.py().import("dataclasses")?;
    let fields = dataclasses.call_method1("fields", (obj,))?;
    let fields = fields.cast::<PyTuple>()?;
    let mut entries = Vec::with_capacity(fields.len());
    for field in fields.iter() {
        let key = field.getattr("name")?.extract::<String>()?;
        let value = obj.getattr(key.as_str())?;
        entries.push((key, convert_py_value_inner(&value, depth + 1, containers)?));
    }
    Ok(Value::Map(entries))
}

/// Push `obj` onto the container stack, or return `None` when it is already on
/// the current path (a cycle). Every `Some` must be paired with
/// [`leave_container`] before the caller returns, including on error paths,
/// so a recovered failure cannot leave a stale entry that later reports a
/// false cycle.
fn enter_container(obj: &Bound<'_, PyAny>, containers: &mut ContainerStack) -> Option<usize> {
    let container_id = obj.as_ptr() as usize;
    if containers.contains(&container_id) {
        return None;
    }
    containers.push(container_id);
    Some(container_id)
}

fn leave_container(container_id: usize, containers: &mut ContainerStack) {
    // Pop outside the assertion: release builds compile `debug_assert_eq!` out
    // entirely, and a pop that never ran made every repeated reference — the
    // same dict in two list slots — look like a cycle.
    let popped = containers.pop();
    debug_assert_eq!(popped, Some(container_id));
}

fn value_to_py<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Value::Null => Ok(py.None().into_bound(py)),
        Value::Bool(value) => value.into_bound_py_any(py),
        Value::Int(value) => value.into_bound_py_any(py),
        Value::Float(value) => value.into_bound_py_any(py),
        Value::String(value) => value.into_bound_py_any(py),
        Value::List(items) => {
            let values: Vec<Bound<'py, PyAny>> = items
                .iter()
                .map(|item| value_to_py(py, item))
                .collect::<PyResult<_>>()?;
            Ok(PyList::new(py, values)?.into_any())
        }
        Value::Map(entries) => {
            let dict = PyDict::new(py);
            for (key, value) in entries {
                dict.set_item(key, value_to_py(py, value)?)?;
            }
            Ok(dict.into_any())
        }
        Value::Raw(json) => py.import("json")?.call_method1("loads", (json.as_str(),)),
    }
}

// The module stores no borrowed Python objects or unsynchronized native globals.
// Exported classes own Send/Sync Rust state, and Python object traversal remains
// scoped to an attached `Python` token, so importing this extension must not
// re-enable the GIL on a free-threaded CPython build.
#[pymodule(name = "_rust", gil_used = false)]
fn rust_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(normalize_level, module)?)?;
    module.add_function(wrap_pyfunction!(syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(normalized_syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(level_table, module)?)?;
    module.add_function(wrap_pyfunction!(_convert_value_debug, module)?)?;
    module.add_function(wrap_pyfunction!(_conversion_stats, module)?)?;
    module.add_function(wrap_pyfunction!(_render_json_debug, module)?)?;
    module.add_function(wrap_pyfunction!(validate_patterns, module)?)?;
    module.add_function(wrap_pyfunction!(render_line, module)?)?;
    module.add_function(wrap_pyfunction!(render_line_with_config, module)?)?;
    module.add_function(wrap_pyfunction!(render_line_console, module)?)?;
    module.add_function(wrap_pyfunction!(render_console_with_config, module)?)?;
    module.add_class::<NativeStringWriter>()?;
    module.add_class::<NativeFilter>()?;
    module.add_class::<RedactionConfig>()?;
    module.add_class::<RuntimeConfig>()?;
    module.add_function(wrap_pyfunction!(merge_fields, module)?)?;
    Ok(())
}
