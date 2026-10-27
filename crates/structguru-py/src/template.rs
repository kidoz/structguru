//! Template metadata analysis and guarded scalar formatting.
//!
//! CPython remains the authoritative full brace parser. The Python LRU owns
//! caching and warnings; native code holds no cross-call Python state.
//! Exceptional field strings use the original Python path splitter to retain
//! subclass and lone-surrogate behavior.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::frozenset::PyFrozenSetBuilder;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyString, PyTuple};

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

fn is_scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_none()
}

fn push_scalar(output: &mut String, value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if let Ok(text) = value.cast_exact::<PyString>() {
        let Ok(text) = text.to_str() else {
            return Ok(false);
        };
        output.push_str(text);
    } else if value.is_exact_instance_of::<PyBool>() {
        output.push_str(if value.is_truthy()? { "True" } else { "False" });
    } else if value.is_none() {
        output.push_str("None");
    } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
        let rendered = value.str()?;
        let Ok(rendered) = rendered.to_str() else {
            return Ok(false);
        };
        output.push_str(rendered);
    } else {
        return Ok(false);
    }
    Ok(true)
}

/// Format the scalar-only subset without reimplementing CPython's full brace
/// grammar. Any other shape returns `None` for the authoritative Python path.
#[pyfunction]
pub(crate) fn _format_simple<'py>(
    message: &Bound<'py, PyString>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
    sensitive: &Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyString>, Bound<'py, PyFrozenSet>)>> {
    if !message.is_exact_instance_of::<PyString>() || !args.iter().all(|value| is_scalar(&value)) {
        return Ok(None);
    }
    let Ok(text) = message.to_str() else {
        return Ok(None);
    };
    let py = message.py();
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut consumed = None;
    let mut offset = 0;
    let mut automatic_index = 0;
    let mut automatic = false;
    let mut manual = false;
    while offset < bytes.len() {
        let Some(relative) = bytes[offset..]
            .iter()
            .position(|byte| matches!(byte, b'{' | b'}'))
        else {
            output.push_str(&text[offset..]);
            break;
        };
        let brace = offset + relative;
        output.push_str(&text[offset..brace]);
        if bytes.get(brace + 1) == Some(&bytes[brace]) {
            output.push(char::from(bytes[brace]));
            offset = brace + 2;
            continue;
        }
        if bytes[brace] == b'}' {
            return Ok(None);
        }
        let Some(relative_end) = bytes[brace + 1..].iter().position(|byte| *byte == b'}') else {
            return Ok(None);
        };
        let end = brace + 1 + relative_end;
        let field = &text[brace + 1..end];
        if field.contains(['!', ':', '.', '[', ']', '{']) || !field.is_ascii() {
            return Ok(None);
        }
        let value = if field.is_empty() {
            if manual {
                return Ok(None);
            }
            automatic = true;
            let index = automatic_index;
            automatic_index += 1;
            let Ok(value) = args.get_item(index) else {
                return Ok(None);
            };
            value
        } else if field.bytes().all(|byte| byte.is_ascii_digit()) {
            if automatic {
                return Ok(None);
            }
            manual = true;
            let Ok(index) = field.parse::<usize>() else {
                return Ok(None);
            };
            let Ok(value) = args.get_item(index) else {
                return Ok(None);
            };
            value
        } else {
            if consumed.is_none() {
                consumed = Some(PyFrozenSetBuilder::new(py)?);
            }
            consumed.as_mut().expect("initialized above").add(field)?;
            if sensitive.contains(field.to_ascii_lowercase().as_str())? {
                output.push_str("[REDACTED]");
                offset = end + 1;
                continue;
            }
            let Some(value) = kwargs.get_item(field)? else {
                return Ok(None);
            };
            value
        };
        if !push_scalar(&mut output, &value)? {
            return Ok(None);
        }
        offset = end + 1;
    }
    let consumed = match consumed {
        Some(consumed) => consumed.finalize(),
        None => PyFrozenSetBuilder::new(py)?.finalize(),
    };
    Ok(Some((PyString::new(py, &output), consumed)))
}
