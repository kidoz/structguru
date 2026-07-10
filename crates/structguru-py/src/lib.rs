use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyOverflowError, PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString, PyTuple,
    PyTupleMethods,
};
use regex::Regex;
use std::time::Duration;
use structguru_core::{Pipeline, StringWriter, Value};

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
/// Raises `ValueError` on the first pattern that fails to compile (e.g.
/// backreferences, look-around) so the Python bridge can fall back to the
/// standard structlog path with a warning.
#[pyfunction]
fn validate_patterns(patterns: Vec<String>) -> PyResult<usize> {
    for p in &patterns {
        Regex::new(p).map_err(|err| PyValueError::new_err(err.to_string()))?;
    }
    Ok(patterns.len())
}

/// Render one log line: convert + redact `fields`, append the standard keys, emit JSON.
///
/// This is the standalone form: patterns (if any) are recompiled per call. For
/// the hot path, prefer `render_line_with_config` + `RedactionConfig`, which
/// compile patterns once at enable time.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (fields, logger, level, service, message, timestamp=None, sensitive_keys=None, sensitive_patterns=None, stack=None, pattern_replacement=None))]
fn render_line(
    fields: &Bound<'_, PyDict>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<Vec<String>>,
    stack: Option<&str>,
    pattern_replacement: Option<&str>,
) -> PyResult<String> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    for (key, value) in fields.iter() {
        let key = key
            .cast::<PyString>()
            .map_err(|_| PyTypeError::new_err("field keys must be strings"))?
            .to_str()?
            .to_owned();
        entries.push((key, convert_py_value(&value)?));
    }
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let compiled_patterns: Vec<Regex> = match &sensitive_patterns {
        Some(patterns) => patterns
            .iter()
            .map(|p| Regex::new(p))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| PyValueError::new_err(err.to_string()))?,
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
        sensitive_keys,
        patterns_ref,
        pattern_replacement,
    )
    .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Compiled redaction patterns held across calls to avoid per-record recompilation.
///
/// Built once at `enable_native()` time and passed to `render_line_with_config`
/// on the hot path. If a pattern fails to compile, construction raises
/// `ValueError` so the Python bridge can fall back to the standard path.
#[pyclass(name = "RedactionConfig")]
struct RedactionConfig {
    patterns: Vec<Regex>,
    replacement: String,
}

#[pymethods]
impl RedactionConfig {
    #[new]
    #[pyo3(signature = (patterns, replacement=None))]
    fn new(patterns: Vec<String>, replacement: Option<String>) -> PyResult<Self> {
        let compiled = patterns
            .into_iter()
            .map(|p| Regex::new(&p))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
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
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    config: &RedactionConfig,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    stack: Option<&str>,
) -> PyResult<String> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    for (key, value) in fields.iter() {
        let key = key
            .cast::<PyString>()
            .map_err(|_| PyTypeError::new_err("field keys must be strings"))?
            .to_str()?
            .to_owned();
        entries.push((key, convert_py_value(&value)?));
    }
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
        sensitive_keys,
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
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    colors: bool,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    sensitive_patterns: Option<Vec<String>>,
    stack: Option<&str>,
    pattern_replacement: Option<&str>,
) -> PyResult<String> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    for (key, value) in fields.iter() {
        let key = key
            .cast::<PyString>()
            .map_err(|_| PyTypeError::new_err("field keys must be strings"))?
            .to_str()?
            .to_owned();
        entries.push((key, convert_py_value(&value)?));
    }
    let generated;
    let timestamp = match timestamp {
        Some(value) => value,
        None => {
            generated = structguru_core::now_iso8601();
            &generated
        }
    };
    let compiled: Vec<Regex> = match &sensitive_patterns {
        Some(patterns) => patterns
            .iter()
            .map(|p| Regex::new(p))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| PyValueError::new_err(err.to_string()))?,
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
        sensitive_keys,
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
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    colors: bool,
    config: &RedactionConfig,
    timestamp: Option<&str>,
    sensitive_keys: Option<Vec<String>>,
    stack: Option<&str>,
) -> PyResult<String> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    for (key, value) in fields.iter() {
        let key = key
            .cast::<PyString>()
            .map_err(|_| PyTypeError::new_err("field keys must be strings"))?
            .to_str()?
            .to_owned();
        entries.push((key, convert_py_value(&value)?));
    }
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
        sensitive_keys,
        patterns_ref,
        Some(&config.replacement),
        stack,
    ))
}

#[pyclass(name = "_NativeStringWriter")]
struct NativeStringWriter {
    writer: StringWriter,
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
        maxsize: usize,
        paused: bool,
        fail_after: Option<usize>,
        target: &str,
        file_path: Option<String>,
        file_max_bytes: usize,
        file_backup_count: usize,
        also_stdout: bool,
    ) -> PyResult<Self> {
        use structguru_core::{MultiSink, RotatingFileSink, StringSink, WriteSink};

        // Compose sinks. file_path (if set) drives real output; also_stdout
        // mirrors to stdout as well. When no file_path is given, `target`
        // selects stdout or a memory/test sink.
        let mut sinks: Vec<Box<dyn StringSink>> = Vec::new();

        if let Some(path) = &file_path {
            let file = RotatingFileSink::new(path, file_max_bytes, file_backup_count)
                .map_err(|err| PyValueError::new_err(err.to_string()))?;
            sinks.push(Box::new(file));
        }
        if also_stdout {
            sinks.push(Box::new(WriteSink::new(std::io::stdout())));
        }

        let writer = if !sinks.is_empty() {
            // Real output sink(s) composed.
            if sinks.len() == 1 {
                StringWriter::with_boxed_sink(maxsize, sinks.pop().unwrap())
            } else {
                StringWriter::with_boxed_sink(maxsize, Box::new(MultiSink::new(sinks)))
            }
        } else {
            // No file_path and not also_stdout: memory/test targets.
            match target {
                "stdout" => StringWriter::new_stdout(maxsize),
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
                    return Err(PyValueError::new_err(format!(
                        "unknown writer target: {other}"
                    )));
                }
            }
        };
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
        // Release the GIL while blocked on a full queue so other Python threads run.
        py.detach(|| self.writer.enqueue_blocking(message).is_ok())
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
        let period = Duration::from_secs_f64(rate_limit_period);
        Ok(Self {
            pipeline: Pipeline::new(sample_rate, sample_max_level, rate_limit_max, period),
        })
    }

    /// Returns `True` when the record should be kept (rendered).
    ///
    /// `key` is the rate-limit grouping key (usually the formatted message);
    /// `level` is the canonical method name.
    #[pyo3(signature = (key, level))]
    fn allow(&self, key: &str, level: &str) -> bool {
        self.pipeline.allow(key, level)
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

fn convert_py_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let mut containers = ContainerStack::with_capacity(8);
    convert_py_value_inner(obj, 1, &mut containers)
}

fn convert_py_value_inner(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    if depth > MAX_VALUE_DEPTH {
        return Err(PyRecursionError::new_err(format!(
            "maximum conversion depth {MAX_VALUE_DEPTH} exceeded"
        )));
    }

    if obj.is_none() {
        return Ok(Value::Null);
    }
    if obj.is_exact_instance_of::<PyBool>() {
        return Ok(Value::Bool(obj.extract()?));
    }
    if obj.is_exact_instance_of::<PyInt>() {
        return obj.extract::<i64>().map(Value::Int).map_err(|err| {
            if err.is_instance_of::<PyOverflowError>(obj.py()) {
                PyOverflowError::new_err("integer is outside the supported i64 range")
            } else {
                err
            }
        });
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
        return Ok(Value::String(value.to_str()?.to_owned()));
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        let container_id = enter_container(obj, containers)?;
        let mut entries = Vec::with_capacity(dict.len());
        for (key, value) in dict.iter() {
            let key = key
                .cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("map keys must be strings"))?
                .to_str()?
                .to_owned();
            entries.push((key, convert_py_value_inner(&value, depth + 1, containers)?));
        }
        leave_container(container_id, containers);
        return Ok(Value::Map(entries));
    }
    if let Ok(list) = obj.cast::<PyList>() {
        let container_id = enter_container(obj, containers)?;
        let mut values = Vec::with_capacity(list.len());
        for item in list.iter() {
            values.push(convert_py_value_inner(&item, depth + 1, containers)?);
        }
        leave_container(container_id, containers);
        return Ok(Value::List(values));
    }
    if let Ok(tuple) = obj.cast::<PyTuple>() {
        let container_id = enter_container(obj, containers)?;
        let mut values = Vec::with_capacity(tuple.len());
        for item in tuple.iter() {
            values.push(convert_py_value_inner(&item, depth + 1, containers)?);
        }
        leave_container(container_id, containers);
        return Ok(Value::List(values));
    }

    // Exotic leaves: handle datetime/date/UUID/Enum/dataclass natively by
    // delegating to the Python object's own serialization methods, which
    // produce byte-identical output to orjson for the parity-tested cases.
    // Genuinely unsupported types (Decimal/bytes/set/timedelta/Path) raise
    // TypeError, matching the orjson rejection contract.
    convert_exotic_leaf(obj, depth, containers)
}

/// Handle exotic Python leaves (datetime, date, UUID, Enum, dataclass) natively
/// without crossing into orjson. Falls back to `TypeError` for unsupported types,
/// matching orjson's default rejection behavior.
fn convert_exotic_leaf(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    // Enum: has a `.value` attribute and its type's `__class__` has `__members__`.
    // Detect via getattr("value") + checking the *type* has __members__ (Enum
    // metaclass marker). This avoids misdetecting objects that happen to have a
    // `value` attribute but aren't enums.
    if obj.hasattr("value")? && obj.get_type().hasattr("__members__")? {
        let value = obj.getattr("value")?;
        return convert_py_value_inner(&value, depth, containers);
    }

    // datetime.datetime / datetime.date: have an .isoformat() method.
    // (datetime is a subclass of date, so both are covered.)
    if obj.hasattr("isoformat")? {
        let iso = obj.call_method0("isoformat")?;
        let s = iso.extract::<&str>()?.to_owned();
        return Ok(Value::String(s));
    }

    // uuid.UUID: has both .hex and .int attributes; str() gives canonical form.
    if obj.hasattr("hex")? && obj.hasattr("int")? {
        // Verify it's actually from the uuid module to avoid misdetecting objects
        // that happen to have both attributes.
        let type_name = obj
            .get_type()
            .name()
            .map(|n| n.to_string())
            .unwrap_or_default();
        if type_name == "UUID" {
            let s = obj.str()?.to_str()?.to_owned();
            return Ok(Value::String(s));
        }
    }

    // dataclass: has __dataclass_fields__ → convert as an ordered Map.
    if obj.hasattr("__dataclass_fields__")? {
        let dict = obj.getattr("__dict__")?;
        if let Ok(d) = dict.cast::<PyDict>() {
            let container_id = enter_container(obj, containers)?;
            let mut entries = Vec::with_capacity(d.len());
            for (key, value) in d.iter() {
                let key = key.extract::<String>()?;
                entries.push((key, convert_py_value_inner(&value, depth + 1, containers)?));
            }
            leave_container(container_id, containers);
            return Ok(Value::Map(entries));
        }
    }

    // Unsupported type — raise TypeError, matching orjson's rejection of
    // Decimal/bytes/bytearray/set/frozenset/timedelta/Path/etc.
    let type_name = obj
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_else(|_| "unknown".to_owned());
    Err(PyTypeError::new_err(format!(
        "Object of type {type_name} is not serializable"
    )))
}

fn enter_container(obj: &Bound<'_, PyAny>, containers: &mut ContainerStack) -> PyResult<usize> {
    let container_id = obj.as_ptr() as usize;
    if containers.contains(&container_id) {
        return Err(PyValueError::new_err(
            "cycle detected while converting Python value",
        ));
    }
    containers.push(container_id);
    Ok(container_id)
}

fn leave_container(container_id: usize, containers: &mut ContainerStack) {
    debug_assert_eq!(containers.pop(), Some(container_id));
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

#[pymodule(name = "_rust")]
fn rust_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(normalize_level, module)?)?;
    module.add_function(wrap_pyfunction!(syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(normalized_syslog_severity, module)?)?;
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
    Ok(())
}
