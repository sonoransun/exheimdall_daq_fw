# Contributing to HeIMDALL DAQ Firmware

Thanks for contributing. This firmware sits under real-time DSP consumers
(direction finding, passive radar), so most of what matters here is *not
breaking wire contracts* and *keeping the default behavior byte-identical*.
Read this page before opening a PR.

## Building locally

The C core builds from `Firmware/_daq_core/` with copy-in dependencies
(see the Build section of `README.md` for the full table):

```bash
# Dependency-light build — no SDR/DSP libraries beyond librtlsdr,
# reproduces the CI "generic" lane (clones/builds librtlsdr into .ci-deps/):
./ci/build.sh generic

# Full-engine build (also builds KFR from source, slow the first time):
./ci/build.sh kfr

# Sanitizer build (-fsanitize=address,undefined):
./ci/build.sh asan
```

Or drive the Makefile directly once the dependencies are copied into
`Firmware/_daq_core/`:

```bash
cd Firmware/_daq_core
make -j4                          # native build, per-arch default engine
make ENGINE=generic MARCH=portable  # dependency-free portable build
make BUILD=asan ENGINE=generic      # sanitizer build
```

`ENGINE=auto` resolves x86_64 → kfr, aarch64/arm64 → kfr, 32-bit ARM → ne10.
`librtlsdr` must be the [krakenrf fork](https://github.com/krakenrf/librtlsdr)
— the stock distro package lacks required API (`rtlsdr_set_dithering`,
`rtlsdr_set_bias_tee_gpio`, ...).

## Testing — two lanes

```bash
cd Firmware

# Lane 1: hardware-free suite. No sudo, no SDR, no C binaries required.
# Runs on Linux and macOS. This is what CI runs and what must be green.
./unit_test.sh --ci

# Lane 2: pipeline suites. Linux + root + built C binaries
# (rebuffer.out, decimate.out) + the unit_test_k4 config values.
# The suites work on a temp-dir copy of daq_chain_config.ini.
sudo ./unit_test.sh --sudo
```

Missing optional dependencies (C compiler, `berkeleydb`, `pyzmq`, `numba`) make
the affected tests **skip with a printed reason** — they are not failures.
A single module: `python3 -m unittest -v _testing/unit_test/<module>.py`.

If you touch the C pipeline, run lane 2 on Linux before submitting. If you have
KrakenSDR hardware, `sudo python3 -W ignore -m unittest -v
_testing/unit_test/test_sys.py` exercises the full chain.

## Invariants you must not break

The full list with file/line references lives in
`Documentation/developer_guide.md`. The headline items:

- **IQ header ABI** — `iq_header.h` and `iq_header.py` describe the same
  1024-byte, natively-aligned struct and must be edited **in lockstep**.
  Sync word `0x2bf7b95a`, `header_version` 8, fixed `RSV_*` reserved-slot
  indices (0–102). `test_iq_header_abi.py` fails your PR if they drift.
- **FIFO signaling protocol** — byte values `A_BUFF_READY=1`, `B_BUFF_READY=2`,
  `INIT_READY=10`, `TERMINATE=255`; forward/backward FIFO discipline in
  `_data_control/`; drop mode = `O_NONBLOCK` on the backward FIFO.
- **Wire formats** — port 5000 `streaming`/`IQDownload` framing; port 5001
  exact-128-byte frames (4-byte verb + 124-byte payload, `FNSD` replies,
  little-endian payloads); port 5002 line-command/JSON replies; ZMQ 1130
  128-byte messages with command chars `r c g a s n b h`. Downstream DoA/PR
  software depends on all of these.
- **Default-off compatibility** — every optional INI section and every
  optional transport/offload driver is disabled by default; with them absent,
  runtime behavior must remain identical to the classic firmware. The default
  transport stays `TRANSPORT_SHM`.
- **Federation naming** — instance 0 unprefixed, instance N `instN_` prefix,
  ports `base + instance_id * port_stride`.
- **Gain-lock rules** — external LNA gain is continuous dB in a separate
  array; it must never pass through the `valid_gains` quantizer or be written
  into `self.gains`/header `if_gains`. Compression flags are advisory only.
- **GPIO discipline** — GPIO 0 is reserved for the noise source; per-channel
  bias-tee uses GPIO m+1; bias changes defer during calibration bursts.
- **Frozen quirks** — the `destory_sm_buffer` misspelling is public API in
  both C and Python; do not rename. `daq_stop.sh --legacy` (SIGRT 64) must
  keep working. Binary names `*.out` and their in-tree paths are contracts.
- **Persistence ABI** — `daq_db` record formats are versioned; changing a
  KEY_FORMAT/VALUE_FORMAT requires bumping the record version and the schema
  generation (currently 2), and handling or discarding old records on read.

## Pull request expectations

- **CI must be green** — the C build matrix (generic + kfr, x86_64 + arm64),
  the asan job, and the Python suite on Ubuntu and macOS all run on every PR.
- Keep changes scoped; do not mix refactors with behavior changes.
- New behavior needs a test in the hardware-free lane whenever possible
  (stub optional deps the way the existing suites do).
- New INI keys must be optional with defaults that preserve existing
  behavior; document them in `README.md`/`CLAUDE.md` and teach
  `ini_checker.py` about them if they have constrained values.
- Anything touching a wire format, the IQ header, or the shared-memory
  protocol needs a matching update on the *other* side (C ↔ Python) in the
  same PR, plus the lockstep tests.
- Update `CHANGELOG.md` (Unreleased section) for user-visible changes.

## Code style

**C** — C99 with GNU/POSIX extensions (`-std=gnu99`, Linux-only: POSIX shm,
FIFOs, `-lrt`). Keep `-Wall` clean. Match the existing style of the file you
are editing; use the `log_*` functions from the vendored rxi logger (with the
mutex lock installed in `main`), check every `shm_open`/`mmap`/`recv` result,
and handle `EINTR`/partial I/O on the hot paths. New optional drivers are
gated by `HAS_*` defines and must compile away cleanly by default.

**Python** — there is **no package structure** in `Firmware/_daq_core`: no
`__init__.py`, imports are direct via `sys.path.insert(0, ...)` (e.g.
`from iq_header import IQHeader`). Do not "fix" this — packaging the pipeline
breaks the deployment layout. The single packaged exception is
`util/heimdall_ctl` (the `heimdall-ctl` console script). Target Python 3.8+;
hard dependencies are numpy/scipy/pyzmq — everything else (`berkeleydb`,
`numba`, `scikit-rf`, serial/I2C libraries) must stay optional with graceful
degradation.

**Shell** — start/stop scripts are bash (`bash -n` clean), `unit_test.sh` is
POSIX sh. They run as root on minimal Pi images: avoid non-standard tools, or
fall back when they are missing (see the `lsof`/`ss` pattern in
`daq_start_sm.sh`).

## License

GPL-3.0. All new files carry the project's GPL header. Vendored code
(inih, rxi log) and copy-in libraries (KFR, Ne10, librtlsdr) keep their own
licenses — respect them in any packaging work.
