use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyOverflowError, PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString, PyTuple,
    PyTupleMethods,
};
use structguru_core::{BoundedQueue, StringWriter, Value};

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

/// Render one log line: convert + redact `fields`, append the standard keys, emit JSON.
#[pyfunction]
#[pyo3(signature = (fields, logger, level, service, message, timestamp=None))]
fn render_line(
    fields: &Bound<'_, PyDict>,
    logger: &str,
    level: &str,
    service: &str,
    message: &str,
    timestamp: Option<&str>,
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
    structguru_core::render_line(entries, logger, level, service, message, timestamp)
        .map_err(|err| PyValueError::new_err(err.to_string()))
}

#[pyclass(name = "_NativeStringQueue")]
struct NativeStringQueue {
    queue: BoundedQueue<String>,
}

#[pymethods]
impl NativeStringQueue {
    #[new]
    fn new(maxsize: usize) -> Self {
        Self {
            queue: BoundedQueue::new(maxsize),
        }
    }

    #[getter]
    fn maxsize(&self) -> usize {
        self.queue.maxsize()
    }

    fn depth(&self) -> usize {
        self.queue.len()
    }

    fn try_enqueue(&self, item: &str) -> bool {
        self.queue.try_enqueue(item.to_owned()).is_ok()
    }

    fn try_dequeue(&self) -> Option<String> {
        self.queue.try_dequeue()
    }

    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let metrics = self.queue.metrics();
        let result = PyDict::new(py);
        result.set_item("enqueued", metrics.enqueued)?;
        result.set_item("dropped", metrics.dropped)?;
        result.set_item("dequeued", metrics.dequeued)?;
        result.set_item("depth", self.queue.len())?;
        result.set_item("maxsize", self.queue.maxsize())?;
        Ok(result)
    }
}

#[pyclass(name = "_NativeStringWriter")]
struct NativeStringWriter {
    writer: StringWriter,
}

#[pymethods]
impl NativeStringWriter {
    #[new]
    #[pyo3(signature = (maxsize, paused=false, fail_after=None, target="memory"))]
    fn new(maxsize: usize, paused: bool, fail_after: Option<usize>, target: &str) -> PyResult<Self> {
        let writer = match target {
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
                return Err(PyValueError::new_err(format!("unknown writer target: {other}")));
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

    // Exotic leaf (datetime/date/UUID/Enum/dataclass/...): delegate to orjson so
    // the output matches the current renderer exactly, and so genuinely
    // unsupported types (Decimal/bytes/set) raise the same TypeError they do today.
    let orjson = obj.py().import("orjson")?;
    let dumped = orjson.call_method1("dumps", (obj,))?;
    let json = String::from_utf8(dumped.extract::<Vec<u8>>()?)
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
    Ok(Value::Raw(json))
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
        Value::Raw(json) => py.import("orjson")?.call_method1("loads", (json.as_str(),)),
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
    module.add_function(wrap_pyfunction!(render_line, module)?)?;
    module.add_class::<NativeStringQueue>()?;
    module.add_class::<NativeStringWriter>()?;
    Ok(())
}
