use std::collections::HashSet;

use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyOverflowError, PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString, PyTuple,
    PyTupleMethods,
};
use structguru_core::Value;

const MAX_VALUE_DEPTH: usize = 64;

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

fn convert_py_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let mut containers = HashSet::new();
    convert_py_value_inner(obj, 1, &mut containers)
}

fn convert_py_value_inner(
    obj: &Bound<'_, PyAny>,
    depth: usize,
    containers: &mut HashSet<usize>,
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
        return Ok(Value::Float(obj.extract()?));
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

    Err(PyTypeError::new_err(format!(
        "unsupported value type: {}",
        obj.get_type().name()?
    )))
}

fn enter_container(obj: &Bound<'_, PyAny>, containers: &mut HashSet<usize>) -> PyResult<usize> {
    let container_id = obj.as_ptr() as usize;
    if !containers.insert(container_id) {
        return Err(PyValueError::new_err(
            "cycle detected while converting Python value",
        ));
    }
    Ok(container_id)
}

fn leave_container(container_id: usize, containers: &mut HashSet<usize>) {
    containers.remove(&container_id);
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
    Ok(())
}
