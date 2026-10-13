//! Python field merging and conversion into owned Rust values.

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyBytes, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString,
    PyTuple, PyTupleMethods,
};
use std::borrow::Cow;
use std::collections::HashSet;
use structguru_core::Value;

const MAX_VALUE_DEPTH: usize = 64;
type ContainerStack = Vec<usize>;

#[pyfunction]
pub(crate) fn _convert_value_debug<'py>(
    py: Python<'py>,
    obj: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    value_to_py(py, &convert_py_value(&obj)?)
}

#[pyfunction]
pub(crate) fn _conversion_stats<'py>(
    py: Python<'py>,
    obj: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let stats = convert_py_value(&obj)?.stats();
    let result = PyDict::new(py);
    result.set_item("nodes", stats.nodes)?;
    result.set_item("max_depth", stats.max_depth)?;
    Ok(result)
}

#[pyfunction]
pub(crate) fn _render_json_debug(obj: Bound<'_, PyAny>) -> PyResult<String> {
    convert_py_value(&obj)?
        .to_json_string()
        .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Convert a Python field mapping into owned `(key, Value)` entries for the core renderer.
///
/// Keys that render alike (see [`is_plain_key`]) collapse into one entry, the
/// later value winning at the earlier key's position.
pub(crate) fn convert_fields(fields: &Bound<'_, PyDict>) -> PyResult<Vec<(String, Value)>> {
    let mut entries: Vec<(String, Value)> = Vec::with_capacity(fields.len());
    let mut plain = true;
    for item in fields.items().iter() {
        let pair = item.cast::<PyTuple>()?;
        let key = pair.get_item(0)?;
        let value = pair.get_item(1)?;
        plain &= is_plain_key(&key);
        entries.push((field_key(&key)?, convert_py_value(&value)?));
    }
    if !plain {
        structguru_core::collapse_duplicate_keys(&mut entries);
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

/// Whether `key` converts to exactly its own text: an exact `str` that is
/// valid UTF-8. Distinct plain keys can never render alike, so only a mapping
/// holding some other key — a lone surrogate, a `str` subclass with its own
/// equality, a non-string key — needs its rendered keys de-duplicated.
fn is_plain_key(key: &Bound<'_, PyAny>) -> bool {
    key.cast_exact::<PyString>()
        .is_ok_and(|text| text.to_str().is_ok())
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
/// Distinct keys that then render alike collapse, the later one in merge
/// order winning at the earlier one's position, except that a contextvar
/// never replaces a bound or kwargs key that renders like it.
///
/// The pairs of every source are collected before any value is converted,
/// because conversion may run `isoformat()` or a dataclass property. The
/// library never mutates a source in place — `bind()` replaces `_bound`,
/// `kwargs` is private to the call, and the contextvars mapping is
/// copy-on-write — so the iteration can never observe a size change.
pub(crate) fn merge_record_fields(
    bound: &Bound<'_, PyDict>,
    kwargs: &Bound<'_, PyDict>,
    contextvars: &Bound<'_, PyDict>,
    exception: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<(String, Value)>> {
    let mut pairs: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)> =
        Vec::with_capacity(bound.len() + kwargs.len() + contextvars.len());
    pairs.extend(bound.iter());
    let bound_end = pairs.len();
    pairs.extend(kwargs.iter());
    let kwargs_end = pairs.len();
    pairs.extend(contextvars.iter());
    // With plain keys, rendered-text equality is Python key equality, so the
    // merge can run on the converted keys.
    if !pairs.iter().all(|(key, _)| is_plain_key(key)) {
        return merge_rendered_alike_fields(bound, kwargs, contextvars, exception);
    }

    let mut entries: Vec<(String, Value)> =
        Vec::with_capacity(pairs.len() + usize::from(exception.is_some()));
    let mut pairs = pairs.into_iter();
    // Bound keys are unique, so no position lookup is needed.
    for (key, value) in pairs.by_ref().take(bound_end) {
        entries.push((field_key(&key)?, convert_py_value(&value)?));
    }
    for (key, value) in pairs.by_ref().take(kwargs_end - bound_end) {
        let key = field_key(&key)?;
        if RESERVED_FIELD_KEYS.contains(&key.as_str()) {
            continue;
        }
        let value = convert_py_value(&value)?;
        upsert_entry(&mut entries, key, value);
    }
    for (key, value) in pairs {
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

/// [`merge_record_fields`] for sources holding a key that is not plain (see
/// [`is_plain_key`]): merge by Python key equality exactly as the Python-side
/// [`merge_fields`] does for the enrichment hooks, then convert the merged
/// mapping, collapsing keys that render alike. Both paths therefore render
/// the same record.
fn merge_rendered_alike_fields(
    bound: &Bound<'_, PyDict>,
    kwargs: &Bound<'_, PyDict>,
    contextvars: &Bound<'_, PyDict>,
    exception: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<(String, Value)>> {
    let mut entries = convert_fields(&merge_fields(bound, kwargs, contextvars)?)?;
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
///
/// A contextvar fills only a key that is still absent. When some key is not
/// plain (see [`is_plain_key`]), absent also means that no bound or kwargs
/// key renders alike: rendering collapses such keys with the later one
/// winning, which would otherwise let the contextvar override the call.
#[pyfunction]
pub(crate) fn merge_fields<'py>(
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
    if contextvars.is_empty() {
        return Ok(merged);
    }
    let plain = merged.iter().all(|(key, _)| is_plain_key(&key))
        && contextvars.iter().all(|(key, _)| is_plain_key(&key));
    let taken: HashSet<String> = if plain {
        HashSet::new()
    } else {
        merged
            .iter()
            .filter_map(|(key, _)| key.cast::<PyString>().ok().map(string_to_owned))
            .collect()
    };
    for (key, value) in contextvars.iter() {
        if merged.contains(&key)? {
            continue;
        }
        if !plain
            && let Ok(text) = key.cast::<PyString>()
            && taken.contains(&string_to_owned(text))
        {
            continue;
        }
        merged.set_item(key, value)?;
    }
    Ok(merged)
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
        return convert_int(obj);
    }
    if obj.is_exact_instance_of::<PyFloat>() {
        return convert_float(obj);
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
        .map(|name| string_to_owned(&name))
        .unwrap_or_else(|_| "unknown".to_owned())
}

/// Owned copies of a record's text arguments under the same policy as field
/// values: a lone surrogate becomes U+FFFD instead of failing the record.
/// `os.fsdecode()` output and stdlib messages built from it reach every entry
/// point this way, so a message can never be rejected where a field is not.
pub(crate) fn text_arguments(
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

/// Copy a Python string, decoding surrogates (not representable in UTF-8, so
/// `to_str` rejects them) with [`decode_surrogates`] instead of failing the
/// record.
pub(crate) fn string_to_owned(value: &Bound<'_, PyString>) -> String {
    string_to_cow(value).into_owned()
}

/// Borrow a Python string's UTF-8 text, copying only to decode surrogates
/// (the [`string_to_owned`] policy without the copy).
pub(crate) fn string_to_cow<'a>(value: &'a Bound<'_, PyString>) -> Cow<'a, str> {
    match value.to_str() {
        Ok(text) => Cow::Borrowed(text),
        Err(_) => Cow::Owned(decode_surrogates(value)),
    }
}

/// The text of a Python string holding surrogate code points.
///
/// A high surrogate directly followed by a low one — a pair split across code
/// points, as `"\ud83d" + "\ude00"` builds — is the character it encodes, as
/// `json.loads` reads it; every other surrogate becomes one U+FFFD.
fn decode_surrogates(value: &Bound<'_, PyString>) -> String {
    // UTF-16 with `surrogatepass` keeps each surrogate as its own code unit,
    // so the standard UTF-16 decoder pairs them. `encode` is looked up on
    // `str` itself so a subclass cannot substitute its own.
    let encoded = value
        .py()
        .get_type::<PyString>()
        .call_method1("encode", (value, "utf-16-le", "surrogatepass"));
    let Ok(encoded) = encoded else {
        return value.to_string_lossy().into_owned();
    };
    let Ok(bytes) = encoded.cast::<PyBytes>() else {
        return value.to_string_lossy().into_owned();
    };
    let units = bytes
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|unit| u16::from_le_bytes(*unit));
    char::decode_utf16(units)
        .map(|decoded| decoded.unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

/// An `int`, or an instance of a subclass, as a JSON number. The value is read
/// from the object itself, never through a method a subclass may override.
fn convert_int(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    match obj.extract::<i64>() {
        Ok(value) => Ok(Value::Int(value)),
        // Outside i64: emit the decimal digits verbatim as a JSON number so
        // the value stays lossless, as `json.dumps` does.
        Err(_) => recover(obj, convert_big_int(obj)),
    }
}

/// A `float`, or an instance of a subclass such as `numpy.float64`, read
/// without calling any method the subclass overrides.
fn convert_float(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let value: f64 = obj.extract()?;
    // orjson emits `null` for NaN/Infinity; serde_json would error, so map here.
    Ok(if value.is_finite() {
        Value::Float(value)
    } else {
        Value::Null
    })
}

fn convert_big_int(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    // `int.__repr__` gives `-?[0-9]+`, which is valid JSON, even for a
    // subclass that overrides `__str__` or `__repr__`. It can still raise
    // `ValueError` past `sys.get_int_max_str_digits()`; `recover` turns that
    // into the marker.
    let digits = obj
        .py()
        .get_type::<PyInt>()
        .call_method1("__repr__", (obj,))?;
    Ok(Value::Raw(string_to_owned(digits.cast::<PyString>()?)))
}

fn convert_dict(
    dict: &Bound<'_, PyDict>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<Value> {
    let mut entries = Vec::with_capacity(dict.len());
    let mut plain = true;
    // Snapshot before conversion invokes Python (isoformat/dataclass properties
    // can mutate this dict, including from another free-threaded worker).
    // Iterating the live dict would panic when its size changes.
    for item in dict.items().iter() {
        let pair = item.cast::<PyTuple>()?;
        let key = pair.get_item(0)?;
        let value = pair.get_item(1)?;
        let (key, plain_key) = convert_map_key(&key, depth, containers)?;
        plain &= plain_key;
        entries.push((key, convert_py_value_inner(&value, depth + 1, containers)?));
    }
    // `1` and `"1"`, or `None` and NaN, are distinct keys that render alike.
    if !plain {
        structguru_core::collapse_duplicate_keys(&mut entries);
    }
    Ok(Value::Map(entries))
}

/// Render a mapping key as the string JSON requires.
///
/// Strings are used as-is. Any other key goes through the value conversion: a
/// string result (Enum, datetime, UUID) is used directly, a scalar result uses
/// its JSON text (`1`, `1.5`, `true`, `null`), and a container result — a
/// tuple, a dataclass — becomes `<unsupported: T>`. Also returns whether the
/// key is plain (see [`is_plain_key`]); the caller collapses keys that render
/// alike.
fn convert_map_key(
    key: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut ContainerStack,
) -> PyResult<(String, bool)> {
    if let Ok(text) = key.cast::<PyString>() {
        return Ok(match text.to_str() {
            Ok(valid) => (valid.to_owned(), key.is_exact_instance_of::<PyString>()),
            Err(_) => (decode_surrogates(text), false),
        });
    }
    let text = match convert_py_value_inner(key, depth + 1, containers)? {
        Value::String(text) => text,
        Value::List(_) | Value::Map(_) => marker_text("unsupported", key),
        scalar => scalar
            .to_json_string()
            .unwrap_or_else(|_| marker_text("unsupported", key)),
    };
    Ok((text, false))
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

    // int and float subclasses (numpy.float64 is a float subclass) render
    // their number. Checked after Enum, so an IntEnum member keeps rendering
    // its `.value`.
    if obj.is_instance_of::<PyInt>() {
        return convert_int(obj);
    }
    if obj.is_instance_of::<PyFloat>() {
        return convert_float(obj);
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
