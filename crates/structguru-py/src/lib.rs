use pyo3::prelude::*;

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

#[pymodule(name = "_rust")]
fn rust_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(normalize_level, module)?)?;
    module.add_function(wrap_pyfunction!(syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(normalized_syslog_severity, module)?)?;
    Ok(())
}
