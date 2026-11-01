# Contributing to HeIMDALL DAQ Firmware

This firmware feeds real-time DSP consumers (direction finding, passive radar).
Two rules matter most: do not break wire contracts, and keep the default
behaviour byte-identical. Read this page before you open a pull request. The
[developer guide](Documentation/developer_guide.md) has the details.

## Building locally

The C core builds from `Firmware/_daq_core/` with copy-in dependencies. The
build lanes are reproducible with `ci/build.sh`:

```bash
# Dependency-light build: krakenrf librtlsdr only (cloned and built into .ci-deps/).
./ci/build.sh generic

# Full-engine build: also builds KFR from source (needs clang; slow the first time).
./ci/build.sh kfr

# Sanitizer build (-fsanitize=address,undefined).
./ci/build.sh asan

# Script, unit-file and config lint (no C build).
./ci/build.sh deploy-lint
```

`ci/build.sh` builds in place. It runs `make clean`, replaces the copied-in
libraries and leaves portable (not native-tuned) binaries. Run it in a
separate clone, not in a deployed tree. After a container run on a Linux host,
run `sudo chown -R "$(id -u):$(id -g)" .ci-deps Firmware/_daq_core`, then
`make clean && make` natively before you start the chain. The C build lanes
need Linux. See
[Container lanes](Documentation/developer_guide.md#container-lanes).

Or drive the Makefile directly once the dependencies are copied into
`Firmware/_daq_core/`:

```bash
cd Firmware/_daq_core
make -j4                            # native build, per-arch default engine
make ENGINE=generic MARCH=portable  # dependency-free portable build
make BUILD=asan ENGINE=generic      # sanitizer build
```

`ENGINE=auto` resolves x86_64 to kfr, aarch64/arm64 to kfr, 32-bit ARM to
ne10, and anything else to generic. `librtlsdr` must be the
[krakenrf fork](https://github.com/krakenrf/librtlsdr). The stock distribution
package lacks `rtlsdr_set_dithering` and `rtlsdr_set_sample_freq_correction_f`,
which `rtl_daq.c` calls. See
[the fork contract](Documentation/developer_guide.md#the-librtlsdr-fork-contract).

## Testing

```bash
cd Firmware

# Lane 1: hardware-free suite. No sudo, no SDR, no C binaries.
# Runs on Linux and macOS. CI runs it on both; it must be green.
./unit_test.sh --ci

# Lane 2: pipeline suites. Linux + root + built rebuffer.out, decimate.out,
# iq_server.out + the unit_test_k4 config values, no chain running. The
# suites work on a temp-dir copy of daq_chain_config.ini. `sudo env` keeps
# the conda PATH and interpreter that plain sudo drops.
sudo env "PATH=$PATH" PYTHON="$(command -v python3)" ./unit_test.sh --sudo

# Which modules are registered, and which are still pending.
./unit_test.sh --list
```

Missing optional dependencies (C compiler, `make`, `berkeleydb`, `pyzmq`,
`numba`) make the affected tests skip with a printed reason. They are not
failures. A single module runs with `python3 -m unittest -v
_testing/unit_test/<module>.py`.

A registered module whose file does not exist yet prints `PEND <module> (not
present yet)` and does not fail the run. `HEIMDALL_STRICT_MODULES=1` turns it
into a failure. CI reads that value from a repository variable (default `0`).

If you touch the C pipeline or the scripts, run lane 2 on a Linux VM or host
before you submit. Follow
[Running the pipeline lane locally](Documentation/developer_guide.md#running-the-pipeline-lane-locally):
stop any chain, install the `unit_test_k4` config, run, then restore your
config. Never run lane 2 while a chain is running: the suites use the
instance-0 shm names in `/dev/shm`. Do not run it in a container. The stage
suites need root, FIFOs and POSIX shm; the start-script suites also need
`chrt -f`.

With KrakenSDR hardware and no chain running, `sudo env "PATH=$PATH" python3
-W ignore -m unittest -v _testing/unit_test/test_sys.py` exercises the full
chain. It launches `./daq_start_sm.sh` itself and leaves the chain running,
so run `sudo ./daq_stop.sh` afterwards.

## Adding a test module

1. Create `Firmware/_testing/unit_test/test_<name>.py` with `unittest`.
2. Register it in `Firmware/unit_test.sh`, one line in `CI_MODULES` or
   `SUDO_MODULES`.
3. For a sudo module, add an `--ignore=` line to `addopts` in `pyproject.toml`.
4. Skip with `self.skipTest("SKIP reason: ...")` when an optional dependency
   is missing.
5. Never modify the live `Firmware/daq_chain_config.ini` or `_data_control/`.

## Invariants you must not break

The full list lives in the
[developer guide](Documentation/developer_guide.md#coding-invariants-do-not-break).
The headline items:

- **IQ header ABI.** `iq_header.h` and `iq_header.py` describe the same
  1024-byte, natively aligned struct. Edit them in lockstep. Sync word
  `0x2bf7b95a`, `header_version` 8, fixed reserved-slot indices 0-113.
  `test_iq_header_abi.py` fails your PR if they drift.
- **FIFO signaling protocol.** Byte values `A_BUFF_READY=1`, `B_BUFF_READY=2`,
  `INIT_READY=10`, `TERMINATE=255`. Forward/backward FIFO discipline in
  `_data_control/`. Drop mode is `O_NONBLOCK` on the backward FIFO.
- **Wire formats.** Port 5000 `streaming`/`IQDownload` framing. Port 5001
  exact 128-byte frames (4-byte verb + 124-byte payload, `FNSD` replies,
  little-endian payloads). Port 5002 line commands with JSON replies. ZMQ 1130
  128-byte messages with command chars `r c g a s n b h`. Downstream DoA and
  passive-radar software depends on all of them.
- **Opt-in rule.** New behaviour rides on a new INI key or flag. With the key
  absent, behaviour stays byte-identical. Existing verbs keep their grammar and
  reply keys; replies may only gain additive JSON keys.
- **Default-path ledger.** A default-path change is allowed only as a ledger
  row with a reason, listed in `CHANGELOG.md`. The current rows are L1-L16 in
  [upgrade_and_rollback.md](Documentation/upgrade_and_rollback.md#4-default-path-changes-in-this-release-ledger-l1-l16).
- **Default-off compatibility.** Every optional INI section and every optional
  transport/offload driver is disabled by default. The default transport stays
  `TRANSPORT_SHM`.
- **Federation naming.** Instance 0 is unprefixed. Instance N uses the `instN_`
  prefix and ports `base + instance_id * port_stride`.
- **Gain-lock rules.** External LNA gain is continuous dB in a separate array.
  It never passes through the `valid_gains` quantizer and is never written into
  `self.gains` or header `if_gains`. Compression flags are advisory only.
- **GPIO discipline.** GPIO 0 is reserved for the noise source. Per-channel
  bias tee uses GPIO m+1. Bias changes defer during calibration bursts.
- **Frozen quirks.** The `destory_sm_buffer` misspelling is public API in C
  and Python; do not rename it. `daq_stop.sh --legacy` (SIGRT 64) must keep
  working. Binary names `*.out` and their in-tree paths are contracts.
- **Persistence ABI.** `daq_db` record formats are versioned. Changing a
  KEY_FORMAT or VALUE_FORMAT requires bumping the record version and the schema
  generation (currently 2), and handling or discarding old records on read.

## Opt-in feature checklist

Before you submit a new feature, check each item:

- [ ] It is enabled only by a new INI key or a new command-line flag.
- [ ] With the key absent, a test asserts the default-path bytes are unchanged.
- [ ] `ini_checker.py` validates the key. Every shipped preset still passes
      `ini_checker.py <preset> no_hw` with no error and no `WARNING:` line.
- [ ] The key appears as a commented `;key = default` line in
      `Firmware/daq_chain_config.ini` and in
      [configuration.md](Documentation/configuration.md).
- [ ] Any new header data uses a `reserved[]` slot, defined in both header
      files, stamped only under its own key.
- [ ] Any unavoidable default-path change is a new ledger row and a
      `CHANGELOG.md` entry.

The [developer guide](Documentation/developer_guide.md#adding-an-opt-in-feature)
explains each item.

## Pull request expectations

- **CI must be green.** Every PR runs the C build matrix (generic and kfr,
  x86_64 and arm64), the asan job, the Python suite on Ubuntu and macOS, the
  `pipeline` sudo job (skipped when the PR changes only `Documentation/`,
  `*.md` or `LICENSE`), and `deploy-lint`.
- **Changed-file rule.** A deploy-lint finding fails your PR when any of its
  attributed files is in your PR's diff. Findings in untouched files are
  reported but do not fail it. Every file you touch must be lint-clean.
  Preset, generator and `federation.ini` findings are also attributed to
  `Firmware/ini_checker.py` (generator findings to the generator too), so
  changing `ini_checker.py` makes every such finding fatal.
- Keep changes scoped. Do not mix refactors with behaviour changes.
- New behaviour needs a test in the hardware-free lane whenever possible. Stub
  optional dependencies the way the existing suites do.
- Anything that touches a wire format, the IQ header or the shared-memory
  protocol needs the matching change on the other side (C and Python) in the
  same PR, plus the lockstep tests.
- Update `CHANGELOG.md` (Unreleased section) for user-visible changes.

## Code style

**C.** C99 with GNU/POSIX extensions (`-std=gnu99`, Linux only: POSIX shm,
FIFOs, `-lrt`). Keep `-Wall` clean. Match the style of the file you edit. Use
the `log_*` functions from the vendored rxi logger (`log.c`/`log.h`).
`rtl_daq`, `rebuffer` and `iq_server` call `log_use_default_lock()` at
startup. Call it in `main` of any new multi-threaded binary, so log lines
from several threads do not interleave. Check every `shm_open`, `mmap` and `recv` result, and
handle `EINTR` and partial I/O on the hot paths. Gate new optional drivers with
`HAS_*` defines; they must compile away cleanly by default.

**Python.** `Firmware/_daq_core` has no package structure and no
`__init__.py`. Modules inside `_daq_core` import their siblings directly (for
example `from iq_header import IQHeader`), because the launched script's
directory is `sys.path[0]`. Code outside `_daq_core` (Firmware-level tools,
`_testing`, tests) adds the directory with `sys.path.insert(0,
os.path.join(<dir of os.path.realpath(__file__)>, ...))`, as
`daq_preflight.py` and `_testing/test_data_synthesizer.py` do. Do not "fix"
this. Packaging the pipeline breaks the
deployment layout. The single packaged exception is `util/heimdall_ctl` (the
`heimdall-ctl` console script). Target Python 3.8+. The hard dependencies are
numpy, scipy and pyzmq. Everything else (`berkeleydb`, `numba`, `scikit-rf`,
serial and I2C libraries) must stay optional with graceful degradation.

**Shell.** The dialect depends on the script:

| Script | Shebang | Rule |
|---|---|---|
| `daq_start_sm.sh`, `daq_synthetic_start.sh`, `daq_supervise.sh`, `benchmark_workload.sh` | `#!/bin/bash` | bash; `bash -n` clean |
| `daq_stop.sh`, `unit_test.sh`, `federation_start.sh`, `federation_stop.sh` | `#!/bin/sh` | POSIX sh (dash on Pi OS). Keep bash-only syntax out. `local` is the only extension used, and dash and ash support it. |
| `daq_common.sh` | none (`# shellcheck shell=sh`) | sourced, never executed. POSIX sh plus `local`. The bash start scripts source it too, so it must stay dash-safe. |
| `util/*.sh` | `#!/bin/bash` | bash |
| `ci/build.sh` | `#!/usr/bin/env bash` | bash; also runs on macOS bash 3.2 |

<!-- verify-after: WP-12 -->
`Firmware/daq_common.sh` is a sourced POSIX sh library with no shebang
(`# shellcheck shell=sh`). The start, stop, supervise and federation scripts
source it from `Firmware/`. The launch helpers (such as `hd_launch`) and the
port check live there.

CI lints every script in the `deploy-lint` job: `bash -n`, `shellcheck -S
warning -x -e SC3043` on `Firmware/*.sh util/*.sh ci/build.sh`, and a
`shellcheck -s dash` pass on `daq_stop.sh` and `daq_common.sh` only. Nothing
dash-checks `federation_start.sh` or `federation_stop.sh`, so keep them
POSIX by hand. Run the lint locally with `./ci/build.sh deploy-lint`. The
scripts run as root on minimal Pi images. Avoid non-standard tools, or fall
back when they are missing (see the `ss`-then-`lsof` fallback in
`hd_port_in_use` in `daq_common.sh`). <!-- verify-after: WP-12 -->

## License

GPL-3.0. Every new file carries the project's GPL header. Vendored code (inih,
rxi log) and copy-in libraries (KFR, Ne10, librtlsdr) keep their own licenses.
Respect them in any packaging work.
