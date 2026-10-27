//! Native orchestration for ordinary records that need no Python slow path.

use crate::dispatch::CallableDispatcher;
use crate::filter::NativeFilter;
use crate::rendering::RuntimeConfig;
use crate::writer::NativeStringWriter;
use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyString, PyTuple};

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (runtime, writer, config, record_filter, level_threshold, enrich, fused, formatter, name, bound, opt_exception, opt_stack, level, message, args, kwargs, contextvars, getframe, dispatcher, levelno=None, created=None))]
pub(crate) fn submit_plain<'py>(
    runtime: &Bound<'py, PyAny>,
    writer: &NativeStringWriter,
    config: &Bound<'py, RuntimeConfig>,
    record_filter: Option<&NativeFilter>,
    level_threshold: i64,
    enrich: bool,
    fused: bool,
    formatter: &Bound<'py, PyAny>,
    name: Option<&Bound<'py, PyString>>,
    bound: &Bound<'py, PyDict>,
    opt_exception: &Bound<'py, PyAny>,
    opt_stack: &Bound<'py, PyAny>,
    level: &str,
    message: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
    contextvars: &Bound<'py, PyAny>,
    getframe: &Bound<'py, PyAny>,
    dispatcher: &CallableDispatcher,
    levelno: Option<&Bound<'py, PyAny>>,
    created: Option<&Bound<'py, PyAny>>,
) -> PyResult<Option<u8>> {
    let level_number = match levelno {
        Some(value) => match value.extract::<i64>() {
            Ok(value) => value,
            Err(_) => return Ok(None),
        },
        None => i64::from(structguru_core::method_level_number(level)),
    };
    if level_number < level_threshold {
        return Ok(Some(0));
    }
    let stack = match kwargs.get_item(pyo3::intern!(runtime.py(), "stack_info"))? {
        Some(value) => value.is_truthy()?,
        None => false,
    };
    if stack || opt_stack.is_truthy()? {
        return Ok(None);
    }
    let exception = match kwargs.get_item(pyo3::intern!(runtime.py(), "exc_info"))? {
        Some(value) => value,
        None => opt_exception.clone(),
    };
    if exception.is_truthy()? || enrich || !fused || !dispatcher.idle() {
        return Ok(None);
    }

    let no_format = message
        .cast_exact::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .is_some_and(|text| !text.contains(['{', '}']));
    let formatted;
    let consumed;
    if no_format {
        formatted = message.clone().cast_into::<PyString>()?;
        consumed = None;
    } else {
        let prepared = formatter.call1((message, args, kwargs, runtime))?;
        let prepared = prepared.cast::<PyTuple>()?;
        formatted = prepared.get_item(0)?.cast_into::<PyString>()?;
        consumed = Some(prepared.get_item(1)?);
    }
    if let Some(consumed) = consumed {
        let none = runtime.py().None();
        for key in consumed.try_iter()? {
            kwargs.call_method1("pop", (key?, &none))?;
        }
    }
    submit_ready(
        writer,
        config,
        record_filter,
        runtime,
        name,
        level,
        level_number,
        &formatted,
        bound,
        kwargs,
        contextvars,
        getframe,
        dispatcher,
        created,
    )
    .map(Some)
}

#[allow(clippy::too_many_arguments)]
fn submit_ready<'py>(
    writer: &NativeStringWriter,
    config: &Bound<'py, RuntimeConfig>,
    record_filter: Option<&NativeFilter>,
    runtime: &Bound<'py, PyAny>,
    name: Option<&Bound<'py, PyString>>,
    level: &str,
    level_number: i64,
    formatted: &Bound<'py, PyString>,
    bound: &Bound<'py, PyDict>,
    kwargs: &Bound<'py, PyDict>,
    contextvars: &Bound<'py, PyAny>,
    getframe: &Bound<'py, PyAny>,
    dispatcher: &CallableDispatcher,
    created: Option<&Bound<'py, PyAny>>,
) -> PyResult<u8> {
    if let Some(filter) = record_filter
        && !filter.allow_level(formatted, level_number)
    {
        return Ok(0);
    }
    let contextvars = contextvars.call0()?.cast_into::<PyDict>()?;
    writer.log_plain(
        runtime.py(),
        config,
        name,
        level,
        formatted,
        bound,
        kwargs,
        &contextvars,
        getframe,
        dispatcher,
        created,
        Some(level_number),
    )
}

fn is_standard_record_attr(key: &Bound<'_, PyAny>, standard: &Bound<'_, PyAny>) -> PyResult<bool> {
    if let Ok(text) = key.cast_exact::<PyString>()
        && let Ok(text) = text.to_str()
    {
        return Ok(matches!(
            text,
            "name"
                | "msg"
                | "args"
                | "levelname"
                | "levelno"
                | "pathname"
                | "filename"
                | "module"
                | "exc_info"
                | "exc_text"
                | "stack_info"
                | "lineno"
                | "funcName"
                | "created"
                | "msecs"
                | "relativeCreated"
                | "thread"
                | "threadName"
                | "processName"
                | "process"
                | "taskName"
                | "message"
                | "asctime"
        ));
    }
    standard.contains(key)
}

fn record_extras<'py>(
    record: &Bound<'py, PyAny>,
    standard: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let fields = PyDict::new(record.py());
    let attributes = record
        .getattr(pyo3::intern!(record.py(), "__dict__"))?
        .cast_into::<PyDict>()?;
    for (key, value) in attributes.iter() {
        if key.cast::<PyString>().is_err() {
            return Err(PyTypeError::new_err("keywords must be strings"));
        }
        if !is_standard_record_attr(&key, standard)? {
            fields.set_item(key, value)?;
        }
    }
    Ok(fields)
}

fn stdlib_method(level: &Bound<'_, PyAny>) -> PyResult<&'static str> {
    let method = match level.extract::<i64>() {
        Ok(level) => structguru_core::method_for_level_number(level),
        Err(_) if level.ge(50)? => "critical",
        Err(_) => "debug",
    };
    Ok(if method == "trace" { "debug" } else { method })
}

#[allow(clippy::too_many_arguments)]
fn prepared_stdlib<'py>(
    runtime: &Bound<'py, PyAny>,
    name: &Bound<'py, PyAny>,
    method: &str,
    message: &Bound<'py, PyAny>,
    extras: &Bound<'py, PyDict>,
    controls: &Bound<'py, PyDict>,
    created: &Bound<'py, PyAny>,
    levelno: &Bound<'py, PyAny>,
    has_stack: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py = runtime.py();
    let values = vec![
        runtime.clone().into_any(),
        name.clone().into_any(),
        PyString::new(py, method).into_any(),
        message.clone().into_any(),
        extras.clone().into_any(),
        controls.clone().into_any(),
        created.clone().into_any(),
        levelno.clone().into_any(),
        has_stack.into_bound_py_any(py)?,
    ];
    Ok(PyTuple::new(py, values)?.into_any())
}

#[pyfunction]
#[pyo3(signature = (record, current_runtime, formatter, contextvars, getframe, dispatcher, standard_attrs))]
pub(crate) fn submit_stdlib<'py>(
    record: &Bound<'py, PyAny>,
    current_runtime: &Bound<'py, PyAny>,
    formatter: &Bound<'py, PyAny>,
    contextvars: &Bound<'py, PyAny>,
    getframe: &Bound<'py, PyAny>,
    dispatcher: &CallableDispatcher,
    standard_attrs: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = record.py();
    let name = record.getattr(pyo3::intern!(py, "name"))?;
    if name.cast::<PyString>().is_err() {
        return Ok(None);
    }
    let extras = record_extras(record, standard_attrs)?;
    let controls = PyDict::new(py);
    let exception = record.getattr(pyo3::intern!(py, "exc_info"))?;
    let has_exception = exception.is_truthy()?;
    if has_exception {
        controls.set_item(pyo3::intern!(py, "exc_info"), exception)?;
    }
    let initial_level = record.getattr(pyo3::intern!(py, "levelno"))?;
    let method = stdlib_method(&initial_level)?;
    let has_stack = record
        .getattr(pyo3::intern!(py, "stack_info"))?
        .is_truthy()?;
    let message = record.call_method0(pyo3::intern!(py, "getMessage"))?;
    if has_stack {
        controls.set_item(
            pyo3::intern!(py, "stack_info"),
            record.getattr(pyo3::intern!(py, "stack_info"))?,
        )?;
    }
    let levelno = record.getattr(pyo3::intern!(py, "levelno"))?;
    let created = record.getattr(pyo3::intern!(py, "created"))?;
    let runtime = current_runtime.call0()?;
    if runtime.is_none() {
        return prepared_stdlib(
            &runtime, &name, method, &message, &extras, &controls, &created, &levelno, has_stack,
        )
        .map(Some);
    }
    let level_number = match levelno.extract::<i64>() {
        Ok(level) => level,
        Err(_) => {
            return prepared_stdlib(
                &runtime, &name, method, &message, &extras, &controls, &created, &levelno,
                has_stack,
            )
            .map(Some);
        }
    };
    if level_number
        < runtime
            .getattr(pyo3::intern!(py, "level_threshold"))?
            .extract::<i64>()?
    {
        return Ok(Some(0u8.into_bound_py_any(py)?));
    }
    if has_stack
        || has_exception
        || runtime.getattr(pyo3::intern!(py, "enrich"))?.is_truthy()?
        || !runtime.getattr(pyo3::intern!(py, "fused"))?.is_truthy()?
        || !dispatcher.idle()
    {
        return prepared_stdlib(
            &runtime, &name, method, &message, &extras, &controls, &created, &levelno, has_stack,
        )
        .map(Some);
    }
    let writer = runtime.getattr(pyo3::intern!(py, "writer"))?;
    let Ok(writer) = writer.cast::<NativeStringWriter>() else {
        return prepared_stdlib(
            &runtime, &name, method, &message, &extras, &controls, &created, &levelno, has_stack,
        )
        .map(Some);
    };
    let writer = writer.try_borrow()?;
    let no_format = message
        .cast_exact::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .is_some_and(|text| !text.contains(['{', '}']));
    let formatted = if no_format {
        message.clone().cast_into::<PyString>()?
    } else {
        let args = PyTuple::empty(py);
        let prepared = formatter.call1((&message, args, &controls, &runtime))?;
        prepared.get_item(0)?.cast_into::<PyString>()?
    };
    let config = runtime
        .getattr(pyo3::intern!(py, "config"))?
        .cast_into::<RuntimeConfig>()?;
    let filter = runtime.getattr(pyo3::intern!(py, "record_filter"))?;
    let filter = if filter.is_none() {
        None
    } else {
        Some(filter.cast::<NativeFilter>()?.try_borrow()?)
    };
    let outcome = submit_ready(
        &writer,
        &config,
        filter.as_deref(),
        &runtime,
        Some(name.cast::<PyString>()?),
        method,
        level_number,
        &formatted,
        &extras,
        &controls,
        contextvars,
        getframe,
        dispatcher,
        Some(&created),
    )?;
    Ok(Some(outcome.into_bound_py_any(py)?))
}
