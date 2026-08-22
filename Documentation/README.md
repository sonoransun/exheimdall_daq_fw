# HeIMDALL DAQ Firmware — Documentation

Reference documentation for the HeIMDALL DAQ firmware: the coherent data
acquisition and signal processing chain for KrakenSDR / KerberosSDR
multichannel RTL-SDR receivers.

Ground truth is always the code. Where this documentation and the source
disagree, the source wins — and please file the discrepancy.

## Documents

| Document | Contents |
|---|---|
| [architecture.md](architecture.md) | Process pipeline, inter-process communication (shared memory + FIFO signaling), startup orchestration and real-time priorities, sync state machine, transport/offload abstractions, federation, RF front-end and antenna orientation subsystems |
| [protocols.md](protocols.md) | The wire reference: IQ frame format and the full 1024-byte header v8 layout, ZMQ tuner control (:1130), TCP control frames (:5001), IQ streaming (:5000), status/metrics (:5002), event PUB (:5003), federation coordinator (:6000) and IQ router (:7000), net-transport framing |
| [configuration.md](configuration.md) | Every `daq_chain_config.ini` section and key — type, default, behavior, mandatory vs. optional — plus the preset catalog |
| [developer_guide.md](developer_guide.md) | Building (dependencies, Makefile variables, PGO), testing (`unit_test.sh --ci` / `--sudo`), CI layout, packaging/systemd, extending transports and offload engines, coding invariants |
| [tutorial_directional_df.md](tutorial_directional_df.md) | Worked walkthrough of the `directional_df` preset: amplified receivers, high-gain antenna, rotator control, scan-and-peak, and reading the v8 telemetry |

## Where things live in the repository

| Path | Contents |
|---|---|
| `Firmware/_daq_core/` | All pipeline sources: C stages (`rtl_daq.c`, `rebuffer.c`, `fir_decimate.c`, `iq_server.c`), Python stages (`delay_sync.py`, `hw_controller.py`), transport/offload abstraction, monitoring, database, federation, RF front-end and orientation modules |
| `Firmware/daq_chain_config.ini` | The live configuration (see [configuration.md](configuration.md)) |
| `Firmware/daq_start_sm.sh`, `daq_synthetic_start.sh`, `daq_stop.sh` | Chain start (hardware / synthetic) and stop scripts |
| `Firmware/unit_test.sh` | Canonical test runner (`--ci` hardware-free lane, `--sudo` pipeline lane) |
| `Firmware/_testing/` | Unit tests, test data generators, IQ recorder/analyzer |
| `Firmware/_data_control/` | Runtime FIFOs, FIR coefficients, hardware capability cache, relay control files |
| `Firmware/_logs/` | Instance 0 logs (`*.log`); federation instance N logs under `instN/`; PID files under `inst<N>/pids/` |
| `Firmware/_fpga_gateware/` | Optional FPGA gateware (ECP5/iCE40, Yosys + nextpnr) |
| `config_files/` | Configuration presets, including `directional_df/` and `performance/` profiles |
| `util/heimdall_ctl/` | The `heimdall-ctl` control client (the only pip-installable package) |
| `util/install.sh` | Dependency build + copy-in installer (librtlsdr fork, KFR/Ne10) |
| `ci/build.sh` | Locally reproducible CI build lanes (generic / kfr / asan) |
| `.github/workflows/ci.yml` | GitHub Actions CI |
| `packaging/` | systemd units and logrotate policy |

## Historical material

`HDAQ_firmware_ver1.0.20201130.pdf` in this directory is the original 2020
firmware documentation. It is **historical**: it predates the transport/offload
abstraction, the v8 IQ header, federation, monitoring, the RF front-end and
orientation subsystems, and the current build system. It is kept for reference
only — do not use it as a wire or configuration reference; use the documents
above instead.
