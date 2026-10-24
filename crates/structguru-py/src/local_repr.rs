//! Bounded Python representations for exception locals and message redaction.
//!
//! Traversal state is local to one attached call. Python hooks classify unusual
//! containers and preserve their spelling; exact builtins need no hook. Parts
//! remain Python strings so custom reprs retain lone surrogates and budgets
//! count Python characters rather than UTF-8 bytes.

use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyComplex, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet,
    PySlice, PyString, PyTuple, PyType,
};

#[derive(Clone, Copy)]
enum Kind {
    Mapping,
    Sequence,
    NamedTuple,
    Dataclass,
    Namespace,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Self::Mapping => "_mapping",
            Self::Sequence => "_sequence",
            Self::NamedTuple => "_named_tuple",
            Self::Dataclass => "_dataclass",
            Self::Namespace => "_namespace",
        }
    }
}

struct LocalRepr<'py> {
    py: Python<'py>,
    keys: Bound<'py, PyFrozenSet>,
    hooks: [Bound<'py, PyAny>; 4],
    budget: usize,
    max_items: usize,
    max_depth: usize,
    size: usize,
    parts: Vec<Bound<'py, PyString>>,
    active: Vec<usize>,
    truncated: bool,
    redacted: bool,
    elided: bool,
}

fn scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyBytes>()
        || value.is_exact_instance_of::<PyByteArray>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyComplex>()
        || value.is_instance_of::<PyType>()
}

impl<'py> LocalRepr<'py> {
    fn text(&self, text: &str) -> Bound<'py, PyString> {
        // Intern at distinct literal sites: a single dynamic intern! site
        // would cache only its first argument.
        match text {
            "{" => pyo3::intern!(self.py, "{").clone(),
            "}" => pyo3::intern!(self.py, "}").clone(),
            "[" => pyo3::intern!(self.py, "[").clone(),
            "]" => pyo3::intern!(self.py, "]").clone(),
            "(" => pyo3::intern!(self.py, "(").clone(),
            ")" => pyo3::intern!(self.py, ")").clone(),
            ", " => pyo3::intern!(self.py, ", ").clone(),
            ": " => pyo3::intern!(self.py, ": ").clone(),
            "=" => pyo3::intern!(self.py, "=").clone(),
            "..." => pyo3::intern!(self.py, "...").clone(),
            "[REDACTED]" => pyo3::intern!(self.py, "[REDACTED]").clone(),
            other => PyString::new(self.py, other),
        }
    }

    fn write(&mut self, mut text: Bound<'py, PyString>) -> PyResult<()> {
        let room = self.budget.saturating_sub(self.size);
        if text.len()? > room {
            text = text
                .get_item(PySlice::new(self.py, 0, room as isize, 1))?
                .cast_into::<PyString>()?;
            self.truncated = true;
            self.elided = true;
        }
        self.size += text.len()?;
        self.parts.push(text);
        Ok(())
    }

    fn literal(&mut self, text: &str) -> PyResult<()> {
        self.write(self.text(text))
    }

    fn redacted(&mut self) -> PyResult<()> {
        self.redacted = true;
        self.literal("[REDACTED]")
    }

    fn sensitive(&self, key: &Bound<'py, PyAny>) -> PyResult<bool> {
        if key.is_exact_instance_of::<PyString>() {
            return self
                .keys
                .as_any()
                .contains(key.call_method0(pyo3::intern!(self.py, "lower"))?);
        }
        if key.is_none()
            || key.is_exact_instance_of::<PyInt>()
            || key.is_exact_instance_of::<PyFloat>()
            || key.is_exact_instance_of::<PyBool>()
        {
            return Ok(false);
        }
        self.hooks[3].call1((key, &self.keys))?.extract()
    }

    fn kind(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Kind>> {
        if scalar(value) {
            return Ok(None);
        }
        if value.is_exact_instance_of::<PyDict>() {
            return Ok(Some(Kind::Mapping));
        }
        if value.is_exact_instance_of::<PyList>()
            || value.is_exact_instance_of::<PyTuple>()
            || value.is_exact_instance_of::<PySet>()
            || value.is_exact_instance_of::<PyFrozenSet>()
        {
            return Ok(Some(Kind::Sequence));
        }
        let kind = self.hooks[0].call1((value.get_type(),))?;
        if kind.is_none() {
            return Ok(None);
        }
        Ok(Some(match kind.extract::<&str>()? {
            "_mapping" => Kind::Mapping,
            "_sequence" => Kind::Sequence,
            "_named_tuple" => Kind::NamedTuple,
            "_dataclass" => Kind::Dataclass,
            "_namespace" => Kind::Namespace,
            _ => return Err(PyValueError::new_err("unknown representation kind")),
        }))
    }

    fn failure(&mut self, error: PyErr) -> PyResult<()> {
        if !error.is_instance_of::<PyException>(self.py) {
            return Err(error);
        }
        let name = error
            .get_type(self.py)
            .getattr(pyo3::intern!(self.py, "__name__"))?;
        let text = pyo3::intern!(self.py, "<repr failed: {}>")
            .call_method1(pyo3::intern!(self.py, "format"), (name,))?
            .cast_into::<PyString>()?;
        self.write(text)
    }

    fn leaf(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        let sliced;
        let value = if value.is_exact_instance_of::<PyString>()
            || value.is_exact_instance_of::<PyBytes>()
            || value.is_exact_instance_of::<PyByteArray>()
        {
            let stop = self.budget.saturating_sub(self.size).saturating_add(1);
            sliced = value.get_item(PySlice::new(
                self.py,
                0,
                stop.min(isize::MAX as usize) as isize,
                1,
            ))?;
            &sliced
        } else {
            value
        };
        match value.repr() {
            Ok(text) => self.write(text),
            Err(error) => self.failure(error),
        }
    }

    fn value(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if self.truncated {
            return Ok(());
        }
        let Some(kind) = self.kind(value)? else {
            return self.leaf(value);
        };
        if depth >= self.max_depth {
            self.elided = true;
            return self.literal("...");
        }
        let id = value.as_ptr() as usize;
        if self.active.contains(&id) {
            return self.literal("...");
        }
        self.active.push(id);
        let result = self.container(value, kind, depth + 1);
        self.active.pop();
        match result {
            Ok(()) => Ok(()),
            Err(error) => self.failure(error),
        }
    }

    fn items(
        &self,
        value: &Bound<'py, PyAny>,
        kind: Kind,
        cls: &Bound<'py, PyType>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if value.is_exact_instance_of::<PyDict>() {
            // A view, not PyDict_Items: snapshot only max_items + 1 entries.
            return self
                .py
                .get_type::<PyDict>()
                .call_method1(pyo3::intern!(self.py, "items"), (value,));
        }
        if value.is_exact_instance_of::<PyList>()
            || value.is_exact_instance_of::<PyTuple>()
            || value.is_exact_instance_of::<PySet>()
            || value.is_exact_instance_of::<PyFrozenSet>()
        {
            return Ok(value.clone());
        }
        self.hooks[2].call1((value, kind.name(), cls))
    }

    fn shape(
        &self,
        value: &Bound<'py, PyAny>,
        kind: Kind,
        cls: &Bound<'py, PyType>,
    ) -> PyResult<(Bound<'py, PyString>, Bound<'py, PyString>)> {
        let (opening, closing) = if value.is_exact_instance_of::<PyDict>() {
            ("{", "}")
        } else if value.is_exact_instance_of::<PyList>() {
            ("[", "]")
        } else if value.is_exact_instance_of::<PyTuple>() {
            ("(", if value.len()? == 1 { ",)" } else { ")" })
        } else if value.is_exact_instance_of::<PySet>() {
            if value.is_truthy()? {
                ("{", "}")
            } else {
                ("set(", ")")
            }
        } else if value.is_exact_instance_of::<PyFrozenSet>() {
            if value.is_truthy()? {
                ("frozenset({", "})")
            } else {
                ("frozenset(", ")")
            }
        } else {
            return self.hooks[1].call1((value, kind.name(), cls))?.extract();
        };
        Ok((self.text(opening), self.text(closing)))
    }

    fn pair(&self, pair: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
        if let Ok(pair) = pair.cast_exact::<PyTuple>()
            && pair.len() == 2
        {
            return Ok((pair.get_item(0)?, pair.get_item(1)?));
        }
        let mut iter = pair.try_iter()?;
        let key = iter
            .next()
            .transpose()?
            .ok_or_else(|| PyValueError::new_err("not enough values to unpack"))?;
        let value = iter
            .next()
            .transpose()?
            .ok_or_else(|| PyValueError::new_err("not enough values to unpack"))?;
        if iter.next().transpose()?.is_some() {
            return Err(PyValueError::new_err("too many values to unpack"));
        }
        Ok((key, value))
    }

    fn container(&mut self, value: &Bound<'py, PyAny>, kind: Kind, depth: usize) -> PyResult<()> {
        // Match Python's operation order even when a property or iterator fails.
        let cls = value.get_type();
        let items_first = matches!(kind, Kind::Mapping | Kind::Dataclass);
        let early_items = if items_first {
            Some(self.items(value, kind, &cls)?)
        } else {
            None
        };
        // Dataclass spelling re-reads the type after selecting fields. Other
        // walks retain the type from before potentially mutating callbacks.
        let shape_cls = if matches!(kind, Kind::Dataclass) {
            value.get_type()
        } else {
            cls.clone()
        };
        let (opening, closing) = self.shape(value, kind, &shape_cls)?;
        self.write(opening)?;
        let items = match early_items {
            Some(items) => items,
            None => self.items(value, kind, &cls)?,
        };
        let mut snapshot = Vec::new();
        for item in items.try_iter()?.take(self.max_items.saturating_add(1)) {
            snapshot.push(item?);
        }
        let sequence = matches!(kind, Kind::Sequence);
        for (index, item) in snapshot.iter().enumerate() {
            let pair = if sequence {
                None
            } else {
                Some(self.pair(item)?)
            };
            if self.truncated {
                return Ok(());
            }
            if index != 0 {
                self.literal(", ")?;
            }
            if index == self.max_items {
                self.elided = true;
                self.literal("...")?;
                break;
            }
            if let Some((key, value)) = pair {
                if matches!(kind, Kind::Mapping) {
                    self.value(&key, depth)?;
                } else {
                    self.write(key.str()?)?;
                }
                self.literal(if matches!(kind, Kind::Mapping) {
                    ": "
                } else {
                    "="
                })?;
                if self.sensitive(&key)? {
                    self.redacted()?;
                } else {
                    self.value(&value, depth)?;
                }
            } else if item.is_instance_of::<PyTuple>()
                && item.len()? == 2
                && self.sensitive(&item.get_item(0)?)?
            {
                self.literal("(")?;
                self.value(&item.get_item(0)?, depth)?;
                self.literal(", ")?;
                self.redacted()?;
                self.literal(")")?;
            } else {
                self.value(item, depth)?;
            }
        }
        self.write(closing)
    }
}

/// Return (text, truncated, redacted, elided) without changing Python reprs.
#[pyfunction]
pub(crate) fn _render_local_repr<'py>(
    value: &Bound<'py, PyAny>,
    keys: &Bound<'py, PyFrozenSet>,
    budget: usize,
    max_items: usize,
    max_depth: usize,
    hooks: &Bound<'py, PyTuple>,
) -> PyResult<(Bound<'py, PyString>, bool, bool, bool)> {
    if max_depth > 64 {
        return Err(PyValueError::new_err(
            "representation depth must not exceed 64",
        ));
    }
    let py = value.py();
    let mut writer = LocalRepr {
        py,
        keys: keys.clone(),
        hooks: [
            hooks.get_item(0)?,
            hooks.get_item(1)?,
            hooks.get_item(2)?,
            hooks.get_item(3)?,
        ],
        budget: budget.min(isize::MAX as usize),
        max_items,
        max_depth,
        size: 0,
        parts: Vec::new(),
        active: Vec::new(),
        truncated: false,
        redacted: false,
        elided: false,
    };
    writer.value(value, 0)?;
    let parts = PyList::new(py, &writer.parts)?;
    let text = pyo3::intern!(py, "")
        .call_method1(pyo3::intern!(py, "join"), (parts,))?
        .cast_into::<PyString>()?;
    Ok((text, writer.truncated, writer.redacted, writer.elided))
}
