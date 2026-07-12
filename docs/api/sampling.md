# Sampling and rate limiting

Configure probabilistic sampling and per-message rate limiting on the native
pre-render path:

```python
from structguru import configure

configure(
    sample_rate=0.1,
    sample_max_level="INFO",
    rate_limit_max=100,
    rate_limit_period=60,
)
```

Drops occur before rendering. `writer_metrics()` reports `sampled` and
`rate_limited` separately from output-queue drops.
