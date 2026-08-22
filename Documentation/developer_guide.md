# Developer Guide

## Building the C core

All C components build from `Firmware/_daq_core/`:

```bash
cd Firmware/_daq_core
make -j4
# -> rtl_daq.out  rebuffer.out  decimate.out  iq_server.out
```

### Dependencies (copy-in layout)

External libraries are **copied into `_daq_core/` before building** and
resolved via `-I. -L.`:

| Files | Needed for | Source |
|---|---|---|
| `librtlsdr.a`, `rtl-sdr.h`, `rtl-sdr_export.h` | always (`rtl_daq.out`) | **the krakenrf fork** — https://github.com/krakenrf/librtlsdr |
| `libkfr_capi.so` + `kfr/` headers | `ENGINE=kfr` (default on x86_64 and aarch64) | KFR, built from source (pinned ref 7.1.0 in `ci/build.sh`, requires clang) |
| `libNE10.a` (the `NE10*.h` headers are vendored) | `ENGINE=ne10` (32-bit ARM only) | project-ne10 |

> **Warning:** the stock distribution `librtlsdr` does **not** work. rtl_daq
> depends on krakenrf-fork-only APIs: `rtlsdr_set_dithering`,
> `rtlsdr_set_sample_freq_correction_f`, `rtlsdr_set_bias_tee_gpio`,
> `rtlsdr_set_gpio`. Build the fork and copy its static library + headers in.

System packages (Ubuntu): `build-essential cmake git libusb-1.0-0-dev
libzmq3-dev` (+ `clang` for the KFR lane). `util/install.sh` automates the
full dependency build and copy-in for x86_64, aarch64 (both KFR), and armv7
(Ne10). `ENGINE=generic` needs **no** DSP library — only librtlsdr.

### Makefile variables

| Variable | Values | Effect |
|---|---|---|
| `ENGINE` | `auto` (default) \| `kfr` \| `ne10` \| `generic` | FIR/convert engine. `auto` resolves: x86_64 → kfr, **aarch64/arm64 → kfr** (the old aarch64 Ne10 branch never linked — libNE10.a is armv7-only), 32-bit arm → ne10, unknown → generic. The kfr/ne10 lanes also compile the generic engine (`-DHAS_GENERIC_OFFLOAD`) so `[offload] fir_engine=generic` is runtime-selectable; `ENGINE=generic` builds with `-DOFFLOAD_GENERIC_ONLY` (generic becomes the auto-detect default, KFR/Ne10 compiled out) |
| `BUILD` | `release` (default) \| `debug` \| `asan` | release = the historical aggressive per-arch flags (`-Ofast -ffast-math -flto ...`, `-DNDEBUG`); debug = `-Og -g`; asan = `-O1 -g -fsanitize=address,undefined` (also on link) |
| `MARCH` | unset \| `portable` \| explicit flags | unset keeps native tuning (`-march=native` / `-mcpu=native`); `portable` strips arch flags (CI/distributable binaries); anything else is used verbatim, e.g. `MARCH='-mcpu=cortex-a72'` |
| `CC` | default `gcc` | Compiler override |
| `EXTRA_CFLAGS` | e.g. `-DHAS_NET_TRANSPORT` | Appended defines for optional drivers |
| `PGO` | `generate` \| `use` | Used internally by `pgo-profile` / `pgo-optimize` |

Examples:

```bash
make -j4                                  # platform default (kfr on x86_64/aarch64)
make ENGINE=generic MARCH=portable        # dependency-light, portable binaries
make BUILD=asan ENGINE=generic            # sanitizer build
make BUILD=debug                          # -Og -g
make CC=clang ENGINE=kfr
```

The KFR link uses `-Wl,-rpath,'$ORIGIN'`, so the copied-in `libkfr_capi.so`
resolves next to `decimate.out` without `LD_LIBRARY_PATH`; a system-installed
KFR also works.

### Targets

| Target | Builds |
|---|---|
| `all` (default) | `rtl_daq.out rebuffer.out decimate.out iq_server.out` |
| `all-offload` | core + all optional transport/offload objects |
| `libs` | `libfpga_hal.so`, `libdma_hal.so` for Python ctypes |
| `clean` | removes objects/binaries but **keeps `*.gcda`** (PGO profiles) and never touches the copied-in vendor libs |
| `distclean` | `clean` + removes PGO profile data |
| `pgo-profile` / `pgo-optimize` | PGO flow, see below |
| legacy aliases | `rtl_daq`, `rebuffer`, `decimator`, `decimate`, `iq_server`, `daq_util`, `transport_objs`, `offload_objs` |

Optional transport/offload drivers stay commented out in the Makefile
(`#TRANSPORT_OBJS += transport_spi.o`, `#EXTRA_CFLAGS += -DHAS_SPI_TRANSPORT
-DHAS_FPGA_OFFLOAD`, etc.) — uncomment the block for the driver you need.
Object builds use `-MMD -MP` dependency tracking, so `make -j` and
header-triggered rebuilds work.

### PGO flow

```bash
make pgo-profile            # clean + instrumented build (-fprofile-generate)
cd ../.. && sudo ./Firmware/benchmark_workload.sh   # or any representative run
cd Firmware/_daq_core
make pgo-optimize           # clean + rebuild with -fprofile-use
```

`clean` deliberately preserves the `.gcda` profiles between the two steps.

## Python environment

Python 3.8+; the project convention is a conda/miniforge env named `kraken`.
Required: `numpy scipy numba pyzmq` (+ `configparser` on old versions).
Optional: `scikit-rf` (only for `iq_adjust_source=touchstone`), `berkeleydb`
(persistence). There is **no package layout** in `Firmware/_daq_core` — no
`__init__.py`; modules import each other directly via `sys.path.insert`.
The only pip-installable component is the control client:

```bash
pip install .        # installs heimdall_ctl + the `heimdall-ctl` console script
```

## Testing

Canonical entry point:

```bash
cd Firmware
./unit_test.sh --ci      # entire hardware-free suite, no sudo
./unit_test.sh --sudo    # pipeline suites (Linux + root + built C binaries)
./unit_test.sh           # legacy behavior: the sudo decimator suite
```

### `--ci` lane (hardware-free)

Runs 15 modules, one interpreter per module, per-module PASS/FAIL summary,
exit 0 only when everything passes. Works on macOS and Ubuntu with just
Python 3 + numpy/scipy. Optional extras enable more coverage — without them
the affected tests **skip with a printed reason** (missing deps are never
failures): a C compiler (the `test_iq_header_abi` C↔Python ABI cross-check),
`berkeleydb` (DB integration tests), `pyzmq`, `numba`.

Modules: `test_iq_header_v8`, `test_iq_header_abi`,
`test_inter_module_messages`, `test_shmem_iface`, `test_control_iface`,
`test_delay_sync_dsp`, `test_fir_designer`, `test_ini_checker`,
`test_gain_budget`, `test_signal_scheduler`, `test_orientation_controller`,
`test_daq_db`, `test_monitoring`, `test_federation`, `test_heimdall_ctl`.

`pytest` also works: `[tool.pytest.ini_options]` points at
`Firmware/_testing/unit_test` and `--ignore`s the sudo/hardware pipeline
modules, so a bare `pytest` runs only the hardware-free suite.

### `--sudo` lane (pipeline integration)

Runs `test_rebuffer`, `test_decimator`, `test_delay_sync`. Requirements:
Linux, root, built `rebuffer.out`/`decimate.out`, and the `unit_test_k4`
preset values in `daq_chain_config.ini`. The suites copy the config into a
per-test temp directory with private FIFOs — **the live config is never
modified**. `test_sys` (full-chain end-to-end) and `test_iq_server` are
manual. The production `delay_sync.py` hard-imports `numba`; on minimal
containers provide numba or a passthrough jit/njit stub on `PYTHONPATH`.

Test logs land in `Firmware/_testing/test_logs/`. Synthetic data generators:
`_testing/gen_cw.py`, `gen_ramp.py`, `gen_std_frame.py` (+ shared
`gen_utils.py`); `_testing/test_data_synthesizer.py` feeds live simulation
mode (`daq_synthetic_start.sh`). The old `test_squelch`/`gen_burst` suite was
deleted (no `squelch.out` source, no `[squelch]` config, no squelch support in
delay_sync).

### Container verification pattern

The CI build steps are locally reproducible:

```bash
./ci/build.sh generic    # krakenrf librtlsdr from source + ENGINE=generic MARCH=portable
./ci/build.sh kfr        # + KFR capi from source (KFR_REF=7.1.0, clang) + ENGINE=kfr
./ci/build.sh asan       # generic lane with BUILD=asan
```

Dependencies are cached in `DEPS_DIR` (default `.ci-deps/`, gitignored;
override refs with `LIBRTLSDR_REF` / `KFR_REF`). To verify like CI does, run
the same script inside a clean Ubuntu 24.04 container (x86_64 or arm64) after
`apt-get install build-essential cmake git libusb-1.0-0-dev libzmq3-dev
[clang]`, then `pip install numpy scipy pyzmq scikit-rf berkeleydb` and
`cd Firmware && ./unit_test.sh --ci`.

## CI (`.github/workflows/ci.yml`)

Three jobs:

1. **c-build** — matrix `{ubuntu-latest, ubuntu-24.04-arm} × {generic, kfr}`
   calling `ci/build.sh <lane>` with `actions/cache` on `.ci-deps` (the kfr
   lane is slow on a cold cache; the generic lane is the fast PR gate).
2. **asan** — `ci/build.sh asan` compile/link with
   `-fsanitize=address,undefined`.
3. **python** — matrix `{ubuntu-latest, macos-latest}`: pip installs
   numpy/scipy/pyzmq/scikit-rf (berkeleydb best-effort on Ubuntu), runs
   `python3 ini_checker.py no_hw`, then `cd Firmware && ./unit_test.sh --ci`,
   then `pip install .` + `heimdall-ctl --help` smoke.

There is deliberately **no FPGA gateware job**: `_fpga_gateware` `make lint`
fails (verilator, 92 warnings-as-errors) and `make verify` fails (`Xx`
undefined values in the top-level sim output) in a clean container. Fix the
RTL/testbenches before re-adding it (the reason is also documented in the
workflow header).

## Packaging

- `pyproject.toml` — installs **only** `util/heimdall_ctl` (package
  `heimdall_ctl` + `heimdall_ctl.client`) with the `heimdall-ctl` console
  script. `Firmware/_daq_core` is intentionally not packaged. Extras:
  `[db]` → berkeleydb, `[rf]` → scikit-rf, `[dev]` → pytest.
- `packaging/systemd/` — `heimdall-daq.service` (single instance, oneshot +
  `RemainAfterExit`, wraps `daq_start_sm.sh`/`daq_stop.sh`) and the optional
  per-instance template `heimdall-daq@.service`. The units grant
  `LimitRTPRIO=99` and `LimitMEMLOCK=infinity` and set **no** `CPUAffinity` —
  the tiered SCHED_FIFO priorities and affinity stay where they are defined,
  in the start script. Each federation instance needs its own working copy of
  `Firmware/` (the instance id comes from the ini, not the command line). See
  `packaging/systemd/README.md` for the `/opt/heimdall` layout.
- `packaging/logrotate/heimdall` — rotates `_logs/*.log` and
  `_logs/inst*/*.log` with `copytruncate` (the processes keep stderr fds
  open).

## Extending the abstractions

### Adding a transport driver

1. Implement the `struct transport_ops` vtable in a new `transport_xxx.c` and
   export `const struct transport_ops* transport_xxx_get_ops(void)`.
   Contract: `get_write_buf`/`get_read_buf` return buffer index 0/1, `3` =
   frame dropped, `255` = TERMINATE, negative = error; `buffer_size` includes
   the 1024-byte header; `num_buffers` is 2.
2. Guard the whole file with `#ifdef HAS_XXX_TRANSPORT`.
3. Add the enum value to `transport_type_t` (`transport.h`) and the dispatch
   case to `transport_get_ops()` in `transport.c` (unknown types must keep
   falling back to shm).
4. Add a commented-out Makefile block (`TRANSPORT_OBJS += transport_xxx.o`,
   `EXTRA_CFLAGS += -DHAS_XXX_TRANSPORT`) and an individual object target.
5. Teach the `[offload] *_transport` string to the stage config parsers, and
   `ini_checker.py` if it validates the value.

### Adding an offload engine

1. Implement the `struct fir_engine` (init/destroy/decimate/reset) and
   `struct convert_engine` vtables in `offload_cpu_xxx.c` /
   `offload_xxx.c`; export `fir_engine_xxx_create()` and
   `convert_engine_xxx_create()`.
2. Add the enum value in `offload.h`, the name strings in
   `offload_engine_from_string()`, and the factory cases (including the
   fallback chain) in `offload.c`.
3. Note the FIR init contract: the coefficients pointer must outlive the
   engine (the Ne10 path stores the pointer; `fir_decimate.c` keeps
   `fir_coeffs` allocated for the process lifetime).
4. Wire a Makefile `ENGINE=` lane or a `HAS_XXX_OFFLOAD` define, and add the
   `[offload] fir_engine` alias to `ini_checker.py`.
5. Prove bit-exactness against the generic engine over multi-frame streams
   (state continuity, channel isolation, reset, taps > frame).

## Coding invariants (do not break)

- **IQ header ABI**: `iq_header.h` and `iq_header.py` change **in lockstep**.
  Exactly 1024 bytes, **native** alignment (the little-endian no-padding
  variant is 1016 bytes — never add a `<` prefix without explicit padding),
  sync word `0x2bf7b95a`, `header_version` 8, `reserved` at offset 252,
  `header_version` at 1020. The named reserved-slot indices (0, 32, 64,
  96–102) are wire ABI. `_Static_assert`s in C and an import-time assert in
  Python enforce this — keep them.
- **FIFO signaling bytes** are frozen: `A_BUFF_READY=1`, `B_BUFF_READY=2`,
  `INIT_READY=10`, `TERMINATE=255`; producer signals on `fw_<name>`, consumer
  frees on `bw_<name>`; drop mode = `O_NONBLOCK` on the backward FIFO only,
  sentinel `3`.
- **Gain-lock rules**: `self.gains` hold *indices* into `valid_gains`; header
  `if_gains` hold actual tuner gains in tenths of dB; the FSM gates on exact
  equality `valid_gains[gains[m]] == if_gains[m]`. External LNA gain lives in
  the separate continuous-dB `ext_lna_gains_db` array — never route it through
  `valid_gains.index()` (ValueError) and never write it into `self.gains` or
  `if_gains`. Total system gain is computed **after** the
  `unified_gain_control` clamp; compression flags are advisory and must never
  auto-change gains.
- **The `destory_sm_buffer` typo** is a frozen public name in both
  `sh_mem_util.h` and `shmemIface.py` (and forwarded by the transport
  wrappers). Do not rename.
- **GPIO discipline**: GPIO 0 is reserved for the noise source; per-channel
  bias-tee uses GPIO m+1; bias changes defer during calibration bursts.
- **ini handlers return 1 on all paths** (unknown keys accepted); callers
  treat only `ini_parse() < 0` as fatal and log a warning for > 0. Keep
  `INI_STOP_ON_FIRST_ERROR` disabled in the vendored `ini.c`.
- **Frame types** (0–4) and **sync-state numbering** (0 none, 1–4 syncing,
  5 lock, 6 track) are consumed across modules and by external tooling —
  renumbering breaks hw_controller, orientation gating, health derivation,
  and DB queries.
- **`daq_block_index`** is monotonically continuous per session; overrun
  events are surfaced via reserved slot 102, never by skipping indices.
- **SIGRT(64)** must remain accepted by all C binaries for
  `daq_stop.sh --legacy`; binary names/paths (`_daq_core/*.out`) are contracts
  referenced by the scripts and pipeline tests.
- **Defaults are behavior-identical**: with all optional INI sections absent
  or disabled and all optional drivers compiled out, runtime behavior must
  match the pre-abstraction firmware. Instance 0 keeps unprefixed resources
  and base ports.
- **daq_db records are a persistence ABI**: every record carries a version
  byte; changing a KEY/VALUE format requires bumping the record version and
  the `DB_SCHEMA_VERSION` marker handling.
- **License**: GNU GPL v3 (headers in every file). Respect vendored
  inih/rxi-log/KFR/Ne10 licensing in packaging work.
