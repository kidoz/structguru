//! Template metadata analysis using CPython's authoritative brace parser.
//!
//! The Python LRU still owns caching and warnings. These functions hold no
//! cross-call Python state; exceptional field strings use the original Python
//! path splitter to retain subclass and lone-surrogate behavior.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::frozenset::PyFrozenSetBuilder;
use pyo3::types::{PyFrozenSet, PyString, PyTuple};

type FieldPath<'py> = (Bound<'py, PyString>, Vec<Bound<'py, PyString>>);
type TemplateInfo<'py> = (Bound<'py, PyFrozenSet>, Bound<'py, PyFrozenSet>, bool, bool);

fn field_path<'py>(
    fallback: &Bound<'py, PyAny>,
    field: &Bound<'py, PyString>,
) -> PyResult<FieldPath<'py>> {
    if !field.is_exact_instance_of::<PyString>() {
        return fallback.call1((field,))?.extract();
    }
    let Ok(text) = field.to_str() else {
        return fallback.call1((field,))?.extract();
    };
    let py = field.py();
    let bytes = text.as_bytes();
    let end = bytes
        .iter()
        .position(|c| matches!(c, b'.' | b'['))
        .unwrap_or(bytes.len());
    let root = if end == bytes.len() {
        field.clone()
    } else {
        PyString::new(py, &text[..end])
    };
    let mut path = Vec::new();
    let mut offset = end;
    while offset < bytes.len() {
        match bytes[offset] {
            b'.' => {
                let start = offset + 1;
                offset = start;
                while offset < bytes.len() && !matches!(bytes[offset], b'.' | b'[') {
                    offset += 1;
                }
                path.push(PyString::new(py, &text[start..offset]));
            }
            b'[' => {
                if let Some(close) = text[offset + 1..].find(']') {
                    let start = offset + 1;
                    offset = start + close;
                    path.push(PyString::new(py, &text[start..offset]));
                    offset += 1;
                } else {
                    // The regex skips unmatched brackets and can still find
                    // later dots or bracket pairs in an invalid field name.
                    offset += 1;
                }
            }
            _ => offset += 1,
        }
    }
    Ok((root, path))
}

fn lowercase<'py>(text: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyString>> {
    if let Ok(ascii) = text.to_str()
        && ascii.is_ascii()
    {
        return Ok(if ascii.bytes().any(|c| c.is_ascii_uppercase()) {
            PyString::new(text.py(), &ascii.to_ascii_lowercase())
        } else {
            text.clone()
        });
    }
    Ok(text
        .call_method0(pyo3::intern!(text.py(), "lower"))?
        .cast_into()?)
}

fn is_digit(text: &Bound<'_, PyString>) -> PyResult<bool> {
    if let Ok(ascii) = text.to_str()
        && ascii.is_ascii()
    {
        return Ok(!ascii.is_empty() && ascii.bytes().all(|c| c.is_ascii_digit()));
    }
    // Python's Unicode digit tables and lowercasing remain authoritative.
    text.call_method0(pyo3::intern!(text.py(), "isdigit"))?
        .extract()
}

fn analyze<'py>(
    parser: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
    default_keys: &Bound<'py, PyAny>,
    message: &Bound<'py, PyString>,
) -> PyResult<TemplateInfo<'py>> {
    let py = message.py();
    let mut consumed = PyFrozenSetBuilder::new(py)?;
    let mut names = PyFrozenSetBuilder::new(py)?;
    let mut has_path = false;
    let mut pending = vec![message.clone()];
    while let Some(current) = pending.pop() {
        for parsed in parser.call1((current,))?.try_iter()? {
            let parsed = parsed?;
            let parsed = parsed.cast::<PyTuple>()?;
            let field = parsed.get_item(1)?;
            if field.is_none() {
                continue;
            }
            let (root, path) = field_path(fallback, field.cast::<PyString>()?)?;
            if root.is_truthy()? && !is_digit(&root)? {
                names.add(lowercase(&root)?)?;
                consumed.add(root)?;
            }
            if !path.is_empty() {
                has_path = true;
                for part in path {
                    names.add(lowercase(&part)?)?;
                }
            }
            let spec = parsed.get_item(2)?;
            if spec.is_truthy()? && spec.contains("{")? {
                pending.push(spec.cast_into::<PyString>()?);
            }
        }
    }
    let names = names.finalize();
    let default_key = !default_keys
        .call_method1(pyo3::intern!(py, "isdisjoint"), (&names,))?
        .extract::<bool>()?;
    Ok((consumed.finalize(), names, default_key, has_path))
}

#[pyfunction]
pub(crate) fn _template_field_path<'py>(
    fallback: &Bound<'py, PyAny>,
    field: &Bound<'py, PyString>,
) -> PyResult<FieldPath<'py>> {
    field_path(fallback, field)
}

/// Return metadata or the original Exception object, as the cached Python
/// implementation did. Control-flow exceptions still propagate.
#[pyfunction]
pub(crate) fn _analyze_template<'py>(
    parser: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
    default_keys: &Bound<'py, PyAny>,
    message: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyAny>> {
    match analyze(parser, fallback, default_keys, message) {
        Ok(info) => Ok(info.into_pyobject(message.py())?.into_any()),
        Err(error) if error.is_instance_of::<PyException>(message.py()) => {
            Ok(error.value(message.py()).clone().into_any())
        }
        Err(error) => Err(error),
    }
}
