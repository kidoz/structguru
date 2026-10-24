//! Source line selection for structured tracebacks, without column positions.
//!
//! Python still owns frame traversal and source loading. Code objects are
//! immutable, and each call consumes its own iterator through the public
//! co_lines() API, so this needs neither raw interpreter layouts nor a cache.

use pyo3::exceptions::PyStopIteration;
use pyo3::prelude::*;
use pyo3::types::PyCode;

#[pyfunction]
pub(crate) fn _traceback_line<'py>(
    code: &Bound<'py, PyCode>,
    instruction: i32,
) -> PyResult<Bound<'py, PyAny>> {
    let py = code.py();
    if instruction < 0 {
        return Ok(py.None().into_bound(py));
    }
    let instruction = instruction as usize;
    for entry in code
        .call_method0(pyo3::intern!(py, "co_lines"))?
        .try_iter()?
    {
        let (start, end, line): (usize, usize, Bound<'py, PyAny>) = entry?.extract()?;
        if start <= instruction && instruction < end {
            return Ok(line);
        }
    }
    // Match the exhausted co_positions() iterator used by extract_tb() for
    // a synthetic traceback whose tb_lasti lies beyond the bytecode.
    Err(PyStopIteration::new_err(()))
}
