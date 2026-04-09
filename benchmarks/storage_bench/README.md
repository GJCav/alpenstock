# Storage Microbenchmark

Run the benchmark with:

```bash
pixi run python -m benchmarks.storage_bench.runner --backend all --scenario all
```

Useful options:

- `--iterations 3`
- `--warmups 1`
- `--blob-size-mib 256`
- `--output playground/storage-bench-report.json`
- `--keep-artifacts`

The benchmark compares:

- `FilesystemBackend`
- `SqliteBackend`
- `rawfs_direct`, a direct-I/O no-transaction lower bound
- `rawfs_staged`, a staged-I/O lower bound that writes through fresh staged files and publishes with `os.replace`

It measures mixed small-file and `256 MiB` blob workloads across commit and rollback flows, and reports both lifecycle phase timings and total wall time.

`rawfs_direct` and `rawfs_staged` are not transactional or recoverable. They are separate baselines so filesystem transactional results can be compared against both direct overwrite I/O and staged-write I/O.
