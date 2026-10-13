//! Python bindings for the native writer and fused record submission.

use crate::conversion::{convert_fields, text_arguments};
use crate::dispatch;
use crate::rendering::{RedactionConfig, RuntimeConfig, render_record};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyString};
use structguru_core::StringWriter;

#[pyclass(name = "_NativeStringWriter")]
pub(crate) struct NativeStringWriter {
    writer: StringWriter,
}

/// Result of handing one line to the queue, observed under the queue lock.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Outcome {
    Accepted = 0,
    Full = 1,
    Closed = 2,
}

impl Outcome {
    /// Text form returned by the `*_outcome` writer methods.
    fn as_str(self) -> &'static str {
        match self {
            Outcome::Accepted => "accepted",
            Outcome::Full => "full",
            Outcome::Closed => "closed",
        }
    }

    /// Numeric form returned by the fused `log` entry points: 0 accepted,
    /// 1 full (dropped), 2 closed (rejected by a retired writer).
    fn code(self) -> u8 {
        self as u8
    }
}

impl NativeStringWriter {
    fn enqueue_owned(&self, py: Python<'_>, message: String, blocking: bool) -> Outcome {
        if !blocking {
            return match self.writer.try_enqueue_with_reason(message) {
                Ok(()) => Outcome::Accepted,
                Err(structguru_core::EnqueueError::Full(_)) => Outcome::Full,
                Err(structguru_core::EnqueueError::Closed(_)) => Outcome::Closed,
            };
        }
        // Keep the uncontended path attached; only a full/closed queue needs
        // the blocking path, whose sole rejection reason is closure.
        match self.writer.enqueue_if_space(message) {
            Ok(()) => Outcome::Accepted,
            Err(message) => {
                if py.detach(|| self.writer.enqueue_blocking(message).is_ok()) {
                    Outcome::Accepted
                } else {
                    Outcome::Closed
                }
            }
        }
    }
}

#[pymethods]
impl NativeStringWriter {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        maxsize,
        paused=false,
        fail_after=None,
        target="memory",
        file_path=None,
        file_max_bytes=0,
        file_backup_count=0,
        also_stdout=false,
    ))]
    fn new(
        py: Python<'_>,
        maxsize: usize,
        paused: bool,
        fail_after: Option<usize>,
        target: &str,
        file_path: Option<String>,
        file_max_bytes: usize,
        file_backup_count: usize,
        also_stdout: bool,
    ) -> PyResult<Self> {
        use structguru_core::{MultiSink, RotatingFileSink, SinkError, StdoutSink, StringSink};

        // File creation and sidecar lock acquisition can block. Keep only
        // Rust-owned arguments across detachment so other Python threads can
        // keep running (and logging through the current runtime) during setup.
        let target = target.to_owned();
        let writer = py
            .detach(move || -> Result<StringWriter, SinkError> {
                // file_path drives real output; also_stdout mirrors to stdout.
                // Otherwise target selects stdout or a memory/test sink.
                let mut sinks: Vec<Box<dyn StringSink>> = Vec::new();

                if let Some(path) = &file_path {
                    let file = RotatingFileSink::new(path, file_max_bytes, file_backup_count)?;
                    sinks.push(Box::new(file));
                }
                if also_stdout {
                    sinks.push(Box::new(StdoutSink));
                }

                let writer = if !sinks.is_empty() {
                    if sinks.len() == 1 {
                        StringWriter::with_boxed_sink(maxsize, sinks.pop().unwrap())
                    } else {
                        StringWriter::with_boxed_sink(maxsize, Box::new(MultiSink::new(sinks)))
                    }
                } else {
                    match target.as_str() {
                        "stdout" => StringWriter::new_stdout(maxsize),
                        "null" => StringWriter::new_null(maxsize),
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
                            return Err(SinkError::new(format!("unknown writer target: {other}")));
                        }
                    }
                };
                Ok(writer)
            })
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
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
        // The queue normally has room, and the push is a mutex plus a
        // VecDeque append: detaching from the GIL for it costs more than the
        // push itself and forces a GIL hand-off per record when other threads
        // are runnable. Only give the GIL up for the wait on a full queue.
        match self.writer.enqueue_if_space(message) {
            Ok(()) => true,
            Err(message) => py.detach(|| self.writer.enqueue_blocking(message).is_ok()),
        }
    }

    /// Return the enqueue outcome without racing a later close or reconfigure.
    fn enqueue_outcome(&self, py: Python<'_>, message: &str, blocking: bool) -> &'static str {
        self.enqueue_owned(py, message.to_owned(), blocking)
            .as_str()
    }

    /// Render one JSON record and hand it to the writer in a single call.
    ///
    /// Renders exactly like `render_line_with_config` (when `config` is given)
    /// or `render_line` (otherwise), appends the newline, and pushes the line
    /// into the queue without ever materialising a Python string. `blocking`
    /// selects `enqueue_blocking` semantics (wait for space with the GIL
    /// released) over `try_enqueue` (a full queue counts as a drop). Returns
    /// whether the line was accepted.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (fields, logger, level, service, message, blocking, config=None, sensitive_keys=None, stack=None, timestamp=None))]
    fn render_enqueue_json(
        &self,
        py: Python<'_>,
        fields: &Bound<'_, PyDict>,
        logger: &Bound<'_, PyString>,
        level: &str,
        service: &Bound<'_, PyString>,
        message: &Bound<'_, PyString>,
        blocking: bool,
        config: Option<&RedactionConfig>,
        sensitive_keys: Option<Vec<String>>,
        stack: Option<&Bound<'_, PyString>>,
        timestamp: Option<&str>,
    ) -> PyResult<bool> {
        Ok(self.render_enqueue_json_outcome(
            py,
            fields,
            logger,
            level,
            service,
            message,
            blocking,
            config,
            sensitive_keys,
            stack,
            timestamp,
        )? == "accepted")
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (fields, logger, level, service, message, blocking, config=None, sensitive_keys=None, stack=None, timestamp=None))]
    fn render_enqueue_json_outcome(
        &self,
        py: Python<'_>,
        fields: &Bound<'_, PyDict>,
        logger: &Bound<'_, PyString>,
        level: &str,
        service: &Bound<'_, PyString>,
        message: &Bound<'_, PyString>,
        blocking: bool,
        config: Option<&RedactionConfig>,
        sensitive_keys: Option<Vec<String>>,
        stack: Option<&Bound<'_, PyString>>,
        timestamp: Option<&str>,
    ) -> PyResult<&'static str> {
        let entries = convert_fields(fields)?;
        let (logger, service, message, stack) = text_arguments(logger, service, message, stack);
        let (logger, service, message, stack) = (
            logger.as_str(),
            service.as_str(),
            message.as_str(),
            stack.as_deref(),
        );
        let generated;
        let timestamp = match timestamp {
            Some(value) => value,
            None => {
                generated = structguru_core::now_iso8601();
                &generated
            }
        };
        let (patterns, replacement) = match config {
            Some(config) if !config.patterns.is_empty() => (
                Some(config.patterns.as_slice()),
                Some(config.replacement.as_str()),
            ),
            Some(config) => (None, Some(config.replacement.as_str())),
            None => (None, None),
        };
        let mut line = structguru_core::render_line(
            entries,
            logger,
            level,
            service,
            message,
            timestamp,
            stack,
            sensitive_keys.as_deref(),
            patterns,
            replacement,
        )
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
        line.push('\n');
        Ok(self.enqueue_owned(py, line, blocking).as_str())
    }

    /// Merge, render, and enqueue one record in a single call.
    ///
    /// Fields merge with the facade's semantics: bound fields first, then call
    /// kwargs (overriding a bound key in place; `exc_info` and `stack_info`
    /// are never fields), then contextvars for keys still absent, then
    /// `exception` as the `exception` field. The record renders in `config`'s
    /// format and is pushed without ever becoming a Python string. `created`
    /// (Unix seconds, as `LogRecord.created`) sets the `timestamp` field; by
    /// default, or when it is not a representable number (any object is
    /// accepted), the current time does.
    /// Returns the outcome code: 0 accepted, 1 full, 2 closed.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (config, logger, level, message, bound, kwargs, contextvars, exception, stack, created=None))]
    fn log(
        &self,
        py: Python<'_>,
        config: &Bound<'_, RuntimeConfig>,
        logger: &Bound<'_, PyString>,
        level: &str,
        message: &Bound<'_, PyString>,
        bound: &Bound<'_, PyDict>,
        kwargs: &Bound<'_, PyDict>,
        contextvars: &Bound<'_, PyDict>,
        exception: Option<&Bound<'_, PyAny>>,
        stack: Option<&Bound<'_, PyString>>,
        created: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<u8> {
        let config = config.get();
        let (line, _) = render_record(
            config,
            logger,
            level,
            message,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            false,
            created,
        )?;
        Ok(self.enqueue_owned(py, line, config.blocking).code())
    }

    /// Like [`log`](Self::log), returning the rendered line as well.
    ///
    /// Returns `(outcome, line, json)`: `line` keeps its trailing newline and
    /// is what stream and callable sinks receive; `json` is the redacted JSON
    /// record for the Sentry hook when `json_copy` is set, rendered separately
    /// in console mode.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (config, logger, level, message, bound, kwargs, contextvars, exception, stack, json_copy, created=None))]
    fn log_capture(
        &self,
        py: Python<'_>,
        config: &Bound<'_, RuntimeConfig>,
        logger: &Bound<'_, PyString>,
        level: &str,
        message: &Bound<'_, PyString>,
        bound: &Bound<'_, PyDict>,
        kwargs: &Bound<'_, PyDict>,
        contextvars: &Bound<'_, PyDict>,
        exception: Option<&Bound<'_, PyAny>>,
        stack: Option<&Bound<'_, PyString>>,
        json_copy: bool,
        created: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<(u8, String, Option<String>)> {
        let config = config.get();
        let (line, json) = render_record(
            config,
            logger,
            level,
            message,
            bound,
            kwargs,
            contextvars,
            exception,
            stack,
            json_copy,
            created,
        )?;
        let outcome = self.enqueue_owned(py, line.clone(), config.blocking);
        Ok((outcome.code(), line, json))
    }

    /// Block until every record enqueued before this call has been written
    /// and the sink flushed.
    ///
    /// `stall_timeout` bounds the wait for a sink that stops making progress
    /// (an unbuffered stdout write to a full pipe with a stopped reader never
    /// returns); the writer is then abandoned — its stuck thread is leaked,
    /// never killed — and the number of undrained records is returned, as the
    /// callable dispatcher's abandonment path does. `None` waits without
    /// limit.
    #[pyo3(signature = (stall_timeout=None))]
    fn flush(&self, py: Python<'_>, stall_timeout: Option<f64>) -> usize {
        let abandoned = py.detach(|| self.writer.flush(dispatch::stall_duration(stall_timeout)));
        usize::try_from(abandoned).unwrap_or(usize::MAX)
    }

    /// Drain and stop the writer; see `flush` for `stall_timeout` and the
    /// return value. Idempotent.
    #[pyo3(signature = (stall_timeout=None))]
    fn close(&self, py: Python<'_>, stall_timeout: Option<f64>) -> usize {
        let abandoned = py.detach(|| self.writer.close(dispatch::stall_duration(stall_timeout)));
        usize::try_from(abandoned).unwrap_or(usize::MAX)
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
