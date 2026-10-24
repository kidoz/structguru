//! Native extension registration and shared metadata.

use pyo3::prelude::*;

mod conversion;
mod dispatch;
mod exception;
mod filter;
mod local_repr;
mod rendering;
mod submission;
mod template;
mod writer;

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

/// The core's level table: `(method name, numeric level)` pairs in display
/// order, with the canonical method first for each number.
#[pyfunction]
fn level_table() -> Vec<(&'static str, u8)> {
    structguru_core::LEVEL_TABLE.to_vec()
}

/// The core's default sensitive keys; `structguru.redaction` builds
/// `DEFAULT_SENSITIVE_KEYS` from this list at import.
#[pyfunction]
fn _default_sensitive_keys() -> Vec<&'static str> {
    structguru_core::DEFAULT_SENSITIVE_KEYS.to_vec()
}

/// The core's redaction marker key; `structguru.redaction` builds
/// `REDACTED_MARKER_KEY` from it at import.
#[pyfunction]
fn _redacted_marker_key() -> &'static str {
    structguru_core::REDACTED_MARKER_KEY
}

// The module stores no borrowed Python objects or unsynchronized native globals.
// Exported classes own Send/Sync Rust state, and Python object traversal remains
// scoped to an attached `Python` token, so importing this extension must not
// re-enable the GIL on a free-threaded CPython build.
#[pymodule(name = "_rust", gil_used = false)]
fn rust_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(normalize_level, module)?)?;
    module.add_function(wrap_pyfunction!(syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(normalized_syslog_severity, module)?)?;
    module.add_function(wrap_pyfunction!(level_table, module)?)?;
    module.add_function(wrap_pyfunction!(_default_sensitive_keys, module)?)?;
    module.add_function(wrap_pyfunction!(_redacted_marker_key, module)?)?;
    module.add_function(wrap_pyfunction!(conversion::_convert_value_debug, module)?)?;
    module.add_function(wrap_pyfunction!(conversion::_conversion_stats, module)?)?;
    module.add_function(wrap_pyfunction!(conversion::_render_json_debug, module)?)?;
    module.add_function(wrap_pyfunction!(rendering::validate_patterns, module)?)?;
    module.add_function(wrap_pyfunction!(rendering::render_line, module)?)?;
    module.add_function(wrap_pyfunction!(
        rendering::render_line_with_config,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rendering::render_line_console, module)?)?;
    module.add_function(wrap_pyfunction!(
        rendering::render_console_with_config,
        module
    )?)?;
    module.add_class::<writer::NativeStringWriter>()?;
    module.add_class::<filter::NativeFilter>()?;
    module.add_class::<rendering::RedactionConfig>()?;
    module.add_class::<rendering::RuntimeConfig>()?;
    module.add_function(wrap_pyfunction!(conversion::merge_fields, module)?)?;
    module.add_function(wrap_pyfunction!(local_repr::_render_local_repr, module)?)?;
    module.add_function(wrap_pyfunction!(exception::_traceback_line, module)?)?;
    module.add_function(wrap_pyfunction!(template::_template_field_path, module)?)?;
    module.add_function(wrap_pyfunction!(template::_analyze_template, module)?)?;
    module.add_class::<dispatch::CallableDispatcher>()?;
    module.add_class::<dispatch::DispatchChannel>()?;
    module.add_function(wrap_pyfunction!(dispatch::in_callback, module)?)?;
    module.add_function(wrap_pyfunction!(dispatch::enter_callback, module)?)?;
    module.add_function(wrap_pyfunction!(dispatch::exit_callback, module)?)?;
    module.add_function(wrap_pyfunction!(dispatch::report_unraisable, module)?)?;
    Ok(())
}
