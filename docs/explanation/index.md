# Explanation

These pages explain why structguru works the way it does: the design of the
native runtime, the guarantees it makes about delivery and redaction, and how
it relates to the libraries it borrows from. They are background reading, not
instructions. To get a task done, see the [how-to guides](../how-to/index.md).
For exact signatures, options, and field lists, see the
[reference](../reference/index.md).

- [Architecture](architecture.md): how one record travels from a logging call
  to its destinations, which parts run in Python and which in Rust, and why
  there is no processor chain.
- [Delivery and lifecycle](delivery-and-lifecycle.md): what it means for a
  record to be accepted, dropped, or rejected, and what happens at flush,
  reconfiguration, shutdown, interpreter exit, and `fork()`.
- [Context propagation](context-propagation.md): the single copy-on-write
  context variable behind request-scoped fields, how it follows tasks and
  threads, and why lazy results split a scope in two.
- [Redaction design](redaction.md): where redaction sits in the record path,
  why keys and patterns are separate mechanisms, why patterns run on a
  linear-time engine, and where the protection ends.
- [Compatibility with loguru and structlog](compatibility.md): which parts of
  the API follow loguru, what the output inherits from the structlog pipeline
  that structguru replaced, and what intentionally differs from both.
