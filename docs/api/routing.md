# Level-aware processing

Use `sample_max_level` to restrict sampling to less-severe records. Level
filtering is configured with `level` and can be adjusted at runtime with
`set_native_level()`.

The v1 runtime does not expose a general processor-chain routing API.
