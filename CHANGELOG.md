# Changelog

All notable changes to the HeIMDALL DAQ Firmware are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Large correctness, performance, tooling, and test overhaul across the C core,
the Python control plane, the build system, and the docs (~84 files over three
rounds). Wire protocols, the IQ header size, and default runtime behavior with
all optional sections disabled are unchanged.

### Breaking / notable

- **aarch64/arm64 default FIR engine is now KFR.** The historic aarch64 Ne10
  build branch never linked (`libNE10.a` is armv7-only); `ENGINE=auto` now
  resolves aarch64 → KFR. Ne10 remains the default on 32-bit ARM only.
- **`daq_db` schema generation 2.** Keys are now big-endian (correct BTree
  ordering), records are `=`-packed version-2 formats with derived index
  offsets (the old extractors read wrong offsets). A generation-1 database
  directory is **auto-discarded and recreated** on the first read-write open;
  read-only opens raise on mismatch.
- **x86_64 decimation now actually decimates.** The KFR FIR engine previously
  heap-overflowed with `decimation_ratio > 1` and emitted full-rate data;
  downstream consumers on x86 now receive correctly decimated output.
- **Start scripts enforce config validation.** `daq_start_sm.sh` and
  `daq_synthetic_start.sh` abort on a nonzero `ini_checker.py` exit code, and
  the C binaries fail fast on missing/invalid mandatory keys (previously:
  uninitialized values). Valid existing configs are unaffected.
- **Per-instance log layout.** Federation instance N>0 now logs to
  `_logs/instN/*.log`; instance 0 paths are unchanged. PID files live in
  `_logs/inst<N>/pids/` (new: `rtl_daq.pid`, `synthesizer.pid`).
- **net transport pairs must be upgraded together** (driver is compile-time
  optional and off by default): frame length now reflects the populated frame
  and a zero-length frame means terminate on both sides.

### Added

- **Query replies with payloads**: the port-5001 query verbs `RFQ`, `OQRY`,
  `SCHQ` now reply `FNSD` + NUL-padded UTF-8 JSON (link budget, orientation
  state, schedule state) and are answered in any pipeline state.
- **Buffer-overrun telemetry**: USB ring-buffer overruns are detected in
  `rtl_daq` and stamped as a cumulative counter into new IQ header v8
  reserved slot 102 (`IQH_RSV_BUFFER_OVERRUN_CNT`, mirrored in
  `iq_header.py`). Header size/version unchanged.
- **Generic FIR engine** (`offload_cpu_generic.c`): dependency-free plain-C
  FIR/convert engine — `make ENGINE=generic` builds the whole core without
  KFR/Ne10, and `[offload] fir_engine = generic` selects it at runtime on
  kfr/ne10 builds.
- **Persistence wiring**: `delay_sync` now records freq-scan results,
  per-frame channel powers, and cal quality into the DB; the scheduler
  persists/resumes its position across restarts (`[schedule]` + `[database]`);
  the port-5002 status server proxies DB reads via new `DB_STATS`,
  `SCAN_SUMMARY`, `CAL_HISTORY` commands; `STATUS` replies gained
  `instance_id`, `recent_drops`, `drop_window_sec`.
- **heimdall-ctl**: new subcommands `lna-gain`, `bias-tee`, `bearing`, `park`,
  `scan start|stop`, `orientation`, `rf-budget`; installable as a pip console
  script (`pip install .` — only `util/heimdall_ctl` is packaged).
- **New optional INI keys** (absent = old behavior): `[daq] listen_address`
  (bind address for the IQ/control/status/event servers), `[monitoring]
  drop_window_sec` (windowed drop-health, default 60 s), `[schedule]
  dwell_time_sec` (per-entry dwell in seconds), `[offload]
  fir_engine = generic`.
- **Build system**: `ENGINE=auto|kfr|ne10|generic`, `BUILD=release|debug|asan`,
  and `MARCH` (incl. `portable`) Makefile variables; header dependency
  tracking; parallel-safe builds; `distclean`; `-rpath $ORIGIN` so the
  copied-in `libkfr_capi.so` works without a system install.
- **CI**: `.github/workflows/ci.yml` — C build matrix (ubuntu x86_64 + arm64,
  generic + kfr lanes via the locally-reproducible `ci/build.sh`), an
  asan compile job, and the hardware-free Python suite on Ubuntu and macOS.
  The FPGA gateware job is deliberately omitted until lint/verify pass.
- **Packaging**: systemd units (`heimdall-daq.service`, per-instance
  `heimdall-daq@.service`) preserving the script-managed RT priorities, and a
  corrected logrotate policy covering the per-instance log layout.
- **Test suites**: `unit_test.sh --ci` (canonical 15-module hardware-free
  suite with per-module summary and a real exit code; runs on Linux and
  macOS) and `--sudo` (pipeline lane). New suites: C-vs-Python IQ-header ABI
  cross-check, real shared-memory/FIFO loopback, ZMQ message round-trips,
  port-5001 framing/reply contract, delay-sync DSP on synthetic data, FIR
  designer and ini_checker contracts, heimdall-ctl.
- **Docs**: restructured `README.md`, corrected `CLAUDE.md`, new
  `CONTRIBUTING.md`, this changelog, and the `Documentation/` set.

### Fixed

- **C core correctness**: overdrive scan read the wrong ring slot (flags were
  always one frame stale); condition-variable use on a never-locked mutex and
  unsynchronized 64-bit buffer indices (UB/race) in `rtl_daq`; heap
  corruption in `rebuffer`'s shutdown free loop; wrong `shm_open`/`mmap`
  error sentinels; short USB transfers silently republishing stale bytes (now
  zero-filled + warned); out-of-bounds writes in both TCP command receive
  paths; NULL-deref crash in `iq_server` on pipeline TERMINATE (clean
  shutdown instead of a segfault); unbounded FIR-coefficient file reads;
  malformed-INI lines were silently ignored (now warned; handlers follow the
  inih convention).
- **KFR engine**: rewritten with per-channel/per-component filter plans
  (previously one plan shared across all channels and I/Q — state leakage)
  and correct output sizing; reset is an exact zero-flush.
- **Python control plane**: `delay_sync` FSM bugs (stale loop variable in the
  IQ-tolerance check, correlation-peak over-indexing, divide-by-zero);
  ZMQ operations gained timeouts + socket rebuild so a hung `rtl_daq` no
  longer wedges calibration; control frames are received exactly (split TCP
  frames no longer desync the 5001 server); hardware mutations only execute
  in the safe FSM state while queries answer immediately; SIGTERM handlers
  and atomic control-file writes (no more torn bearing reads); federation
  coordinator/scheduler sent malformed text commands (now real 128-byte
  frames) and the IQ router now speaks the `IQDownload` protocol (it hung
  after the first frame); federation coordinator election actually works;
  status health uses windowed drop deltas (one historical drop no longer
  makes `ok` unreachable forever); `daq_db.rotate()` deleted nothing on real
  BerkeleyDB (missing write cursor); PCA9685 pan-tilt prescaler was never
  programmed (servo pulses were out of range); scan-and-peak averages dwell
  frames instead of trusting a single loudest frame; `heimdall-ctl` sent a
  nonexistent `RECL` verb and read status keys the server never published;
  `auto_config` no longer destroys config comments.
- **Startup/stop scripts**: port-check no longer spins forever without
  `lsof` (falls back to `ss`); stale process names purged from `daq_stop.sh`
  (PID-file-first stop, `--legacy` preserved); `install.sh` no longer aborts
  on aarch64 and actually installs the FIR library it configures.

### Changed / performance

- `rtl_daq` → `rebuffer` hop: single coalesced `writev` + enlarged pipe;
  `memchr`-based overdrive scan; per-transfer debug logging compiled out of
  release builds; `mlockall` across all four C processes (best-effort).
- `iq_server`: persistent listener (clients can reconnect between sessions
  without connection-refused), `TCP_NODELAY` + single-syscall `sendmsg`
  frame output.
- NEON u8→f32 conversion uses real AArch64 intrinsics, proven bit-identical
  to the scalar expression.
- `delay_sync` hot path: Hermitian `eigh` + `einsum` for IQ calibration,
  batched FFTs, precompiled header codec, and a header-only (1024-byte)
  delay_sync→hw_controller ring instead of two full-size frame buffers
  (~34 MB shared memory saved at defaults).
- Thread-safe, rate-limited logging in the C core.

### Removed

- `test_squelch.py` and `gen_burst.py` (dead: `squelch.out` has no source, no
  Makefile target, no config section; `delay_sync` has no squelch support).
- Dead batched-control FIFO API in `sh_mem_util`.
- Checked-in build artifact `_daq_core/serial_test` (ELF) and `__pycache__`
  directories; root `.gitignore` added.
- `Firmware/BringUpInstructions.txt` content (stale Kerberos-era steps)
  replaced with a pointer to the current docs.
