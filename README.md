# HeIMDALL DAQ Firmware

Coherent data acquisition and signal processing chain for KrakenSDR multichannel RTL-SDR receivers. Designed for Raspberry Pi 4 (ARM64) and x86_64 Linux systems.

HeIMDALL captures raw IQ samples from multiple synchronized RTL-SDR tuners, performs sample-level and IQ-level calibration across all channels, and delivers coherent multichannel data to downstream DSP applications such as direction finding, passive radar, and GNU Radio.

## Architecture Overview

```mermaid
graph LR
    subgraph SDR Hardware
        RTL1[RTL-SDR Ch 0]
        RTL2[RTL-SDR Ch 1]
        RTL3[RTL-SDR Ch N]
    end

    subgraph HeIMDALL DAQ Pipeline
        RD[rtl_daq.out<br/>Multi-tuner Reader]
        RB[rebuffer.out<br/>CPI Reshaper]
        DEC[decimate.out<br/>FIR Decimator]
        DS[delay_sync.py<br/>Delay & IQ Sync]
        HWC[hw_controller.py<br/>Hardware Control]
    end

    subgraph Output
        IQS[iq_server.out<br/>TCP :5000]
        SHM[Shared Memory<br/>IPC]
    end

    subgraph Downstream DSP
        DOA[Direction Finding]
        PR[Passive Radar]
        GR[GNU Radio]
    end

    RTL1 --> RD
    RTL2 --> RD
    RTL3 --> RD
    RD -->|pipe| RB
    RB -->|shmem| DEC
    DEC -->|shmem| DS
    DS -->|shmem| HWC
    DS -->|shmem| IQS
    DS -->|shmem| SHM
    IQS --> DOA
    IQS --> PR
    SHM --> GR
    HWC -.->|ZMQ :1130| RD
```

## Core Capabilities

### Real-Time Coherent Data Acquisition

The pipeline transforms raw ADC samples from multiple unsynchronized RTL-SDR tuners into a coherent multichannel IQ stream:

```mermaid
flowchart TD
    A[Raw IQ from N tuners] --> B[Rebuffer into CPI blocks]
    B --> C[FIR filter + decimation]
    C --> D{Calibration<br/>state?}
    D -->|Uncalibrated| E[Sample delay estimation<br/>via cross-correlation]
    E --> F[Fractional delay compensation<br/>via sampling freq tuning]
    F --> G[IQ amplitude & phase calibration<br/>via eigendecomposition]
    G --> H[Track lock achieved]
    D -->|Calibrated| I[Continuous sync tracking]
    I -->|Sync lost| E
    H --> I
    I --> J[Coherent multichannel output]
```

- **Sample-level synchronization** -- Cross-correlation-based delay estimation and compensation across all channels
- **Fractional sample delay correction** -- Phase-frequency curve fitting with sampling frequency PPM tuning
- **IQ calibration** -- Amplitude and phase correction via spatial correlation matrix eigendecomposition
- **Continuous tracking** -- Real-time monitoring of sync quality with automatic recalibration on drift or frequency change
- **Noise source calibration** -- Internal noise source switching for controlled calibration bursts (continuous or burst mode)

### Hardware Control

The hardware controller manages the RF front-end and calibration hardware:

- **Gain control** -- Per-channel or unified IF gain management with overdrive protection and automatic tuning
- **Frequency tuning** -- RF center frequency changes via ZMQ to the receiver module
- **Noise source control** -- Programmable internal noise source for calibration (with automatic gain preset during cal)
- **AGC support** -- Optional automatic gain control mode
- **External control interface** -- TCP server on port 5001 accepting FREQ, GAIN, AGC, and INIT commands

### Dynamic Signal Scheduling

Automated frequency hopping and scanning with calibration-aware timing:

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> ACTIVE_WAITING_CAL : load_schedule()
    ACTIVE_WAITING_CAL --> ACTIVE_DWELLING : sync_state >= 6<br/>(cal complete)
    ACTIVE_WAITING_CAL --> ACTIVE_WAITING_CAL : cal in progress
    ACTIVE_WAITING_CAL --> ACTIVE_WAITING_CAL : timeout → skip entry
    ACTIVE_DWELLING --> ACTIVE_WAITING_CAL : dwell complete → FREQ cmd
    ACTIVE_DWELLING --> ACTIVE_DWELLING : counting DATA frames
    ACTIVE_WAITING_CAL --> IDLE : schedule complete (once mode)
    IDLE --> IDLE : clear_schedule()
```

- **Schedule definitions** -- INI config (inline frequencies/gains/dwell) or JSON files
- **Repeat modes** -- Loop (continuous), once (single pass), pingpong (forward-reverse)
- **Calibration-aware** -- Waits for sync lock before counting dwell frames; defers transitions during calibration bursts
- **Runtime control** -- Load (SCHD), stop (SCHS), query (SCHQ), and skip-next (SCHN) commands via TCP
- **Per-entry gains** -- Optional per-frequency gain presets applied automatically on hop

### Persistent Signal Analysis (BerkeleyDB)

Optional persistent storage for operational metrics and calibration history:

```mermaid
graph TD
    subgraph "Write Path (non-blocking)"
        DS2[delay_sync.py] -->|frame metrics<br/>cal events| Q[Write Queue]
        HWC2[hw_controller.py] -->|HW snapshots<br/>cal events| Q
        Q --> WT[Background Writer<br/>Thread]
        WT --> DB[(BerkeleyDB<br/>Environment)]
    end

    subgraph "Databases"
        DB --> FM[frame_metrics.db<br/>Per-frame telemetry]
        DB --> CH[cal_history.db<br/>Calibration events]
        DB --> FS[freq_scan.db<br/>Per-frequency aggregates]
        DB --> HW[hw_snapshots.db<br/>Hardware state]
        DB --> SS[schedule_state.db<br/>Schedule persistence]
    end

    subgraph "Secondary Indices"
        FM -.-> IF[idx_freq — by frequency]
        FM -.-> IT[idx_time — by timestamp]
        FM -.-> ISS[idx_sync_state — sync lost]
        FM -.-> IFT[idx_frame_type — by type]
        CH -.-> ICF[idx_cal_freq — cal by freq]
        CH -.-> ICE[idx_cal_event — by event type]
    end
```

- **Non-blocking writes** -- All writes queued to a daemon background thread; zero impact on real-time pipeline
- **Frame metrics** -- Per-frame: frequency, sync state, gains, channel powers, SNR, cal quality
- **Calibration history** -- State transition events with IQ corrections, delays, and sync counters
- **Frequency scan aggregates** -- Running averages of SNR and cal quality per visited frequency
- **Hardware snapshots** -- Periodic capture of gain values, overdrive flags, noise source state
- **Automatic rotation** -- Configurable max age (default 7 days) with hourly cleanup and compaction
- **Rich query API** -- Time range, frequency, sync state, and event type queries via secondary indices

### Inter-Process Communication

```mermaid
graph LR
    subgraph Control Plane
        TCP[TCP :5001<br/>External Control]
        ZMQ[ZMQ :1130<br/>Tuner Control]
        FIFO[Control FIFOs<br/>_data_control/]
    end

    subgraph Data Plane
        SM1[shmem: rtl_daq_out]
        SM2[shmem: decimator_out]
        SM3[shmem: delay_sync_iq]
        SM4[shmem: delay_sync_hwc]
        PIPE[stdout pipe]
    end

    TCP --> HWC3[hw_controller]
    HWC3 --> ZMQ
    ZMQ --> RD2[rtl_daq]
    RD2 -->|PIPE| RB2[rebuffer]
    RB2 -->|SM1| DEC2[decimate]
    DEC2 -->|SM2| DS3[delay_sync]
    DS3 -->|SM3| IQS2[iq_server]
    DS3 -->|SM4| HWC3
    FIFO --> DS3
```

- **Shared memory ring buffers** -- Double-buffered IQ data transfer between pipeline stages
- **ZMQ REQ/REP** -- 128-byte message protocol for frequency, gain, noise source, and sampling frequency control
- **TCP control** -- 128-byte command frames (4-byte command + 124-byte payload) for external integration
- **1024-byte IQ headers** -- Binary frame headers with sync word, metadata, calibration state, and per-channel gains

The shared memory transport is the default backend of the pluggable [Transport Abstraction Layer](#transport-abstraction-layer) described below.

## Platform Architecture

### Transport Abstraction Layer

The data plane between pipeline stages uses a pluggable transport layer, allowing the same pipeline binaries to communicate over shared memory, SPI, PCIe, USB 3.0, or Ethernet depending on the deployment topology.

```mermaid
graph TD
    PS[Pipeline Stage] --> TP[Transport Producer<br/>vtable dispatch]
    TP --> SHM["Shared Memory<br/>(POSIX shmem — default)"]
    TP --> SPI["SPI + DMA<br/>(FPGA HAT, CRC-32 framing)"]
    TP --> PCIE["PCIe / XDMA<br/>(UIO BAR + DMA channels)"]
    TP --> USB["USB 3.0<br/>(libusb async bulk)"]
    TP --> NET["TCP / Ethernet<br/>(LEN_32 + PAYLOAD framing)"]

    SHM --> TC[Transport Consumer]
    SPI --> TC
    PCIE --> TC
    USB --> TC
    NET --> TC
```

- **Backend selection** -- Configured per pipeline link via `[offload]` keys: `rebuffer_transport`, `decimator_transport`, `delay_sync_transport`
- **C vtable interface** -- `transport.h` defines `struct transport_ops` with `init/destroy/get_write_buf/submit_write/get_read_buf/release_read/send_terminate` operations; `transport_create()` factory returns a handle for the requested type
- **Python wrapper** -- `transportIface.py` provides `TransportProducer` / `TransportConsumer` as drop-in replacements for `shmemIface.py`
- **Flow control modes** -- Backpressure (block until buffer available) or drop (skip frame on contention, via `O_NONBLOCK`)
- **Instance-aware naming** -- Channel names incorporate `instance_id` for multi-instance federation (e.g., `inst1_decimator_out`)
- **Performance counters** -- Each handle tracks `total_bytes`, `total_frames`, `dropped_frames`
- **Compile-time driver selection** -- Optional backends enabled via Makefile flags: `HAS_SPI_TRANSPORT`, `HAS_PCIE_TRANSPORT`, `HAS_USB3_TRANSPORT`, `HAS_NET_TRANSPORT`

### Offload Engine Abstraction

Signal processing operations (FIR decimation, FFT, cross-correlation, data conversion) use pluggable compute engines selected at runtime, enabling transparent acceleration on different hardware.

```mermaid
graph LR
    subgraph "C Layer — fir_engine vtable"
        FE[fir_engine_create<br/>auto-detect] --> NEON["CPU NEON<br/>(ARM)"]
        FE --> KFR["CPU KFR<br/>(x86_64)"]
        FE --> FPGA_E["FPGA<br/>(SPI triple-buffer)"]
        FE --> GPU_E["GPU<br/>(VideoCore VI)"]
    end

    subgraph "Python Layer"
        FFT[FFTEngine] --> SP["SciPy<br/>(configurable workers)"]
        FFT --> GP["GPU OpenCL"]
        FFT --> FP["FPGA stub"]
        COR[CorrelationEngine] --> NP["NumPy FFT-based"]
        COR --> GP2["GPU batch"]
        COR --> FP2["FPGA stub"]
    end
```

**Auto-detect priority:** FPGA > GPU > CPU NEON (ARM) / CPU KFR (x86)

- **FIR engine** (`offload.h`) -- vtable with `init/decimate/reset/destroy` per channel; supports NEON, KFR, FPGA, and GPU backends
- **Data conversion engine** -- U8-to-F32 deinterleave with SIMD-optimized paths: `(sample - 127.5) / 127.5`
- **Python FFT engine** (`offload_engines.py`) -- SciPy (configurable worker threads), GPU (OpenCL radix-2), FPGA (stub for future expansion)
- **Python correlation engine** (`offload_engines.py`) -- FFT-based cross-correlation with GPU batch mode support
- **Auto-detection** -- `fir_engine_create()` with `OFFLOAD_AUTO` probes hardware at startup and falls back gracefully to the platform default
- **Configuration** -- `[offload] fir_engine = auto` and `[offload] fft_engine = auto` in config INI; `auto` triggers runtime detection

### FPGA Gateware

An optional FPGA HAT can offload the FIR decimation and cross-correlation stages to dedicated hardware, freeing CPU cycles for calibration and control.

```mermaid
graph LR
    RX[SPI RX] --> DI[iq_deinterleave<br/>I/Q separation]
    DI --> CONV[u8_to_f32<br/>normalization]
    CONV --> FIR[fir_decimate<br/>polyphase FIR]
    FIR --> RI[output_reinterleave] --> TX[SPI TX]
    FIR --> XC[xcorr_engine<br/>FFT-based]
    XC --> TX2[SPI TX<br/>mode 1/2]

    style RX fill:#f9f,stroke:#333
    style TX fill:#f9f,stroke:#333
    style TX2 fill:#f9f,stroke:#333
```

- **Target devices** -- Lattice ECP5 (ULX3S dev board) and iCE40 (OrangeCrab)
- **Open-source toolchain** -- Yosys (synthesis) + nextpnr (place-and-route) + ecppack (bitstream generation)
- **RTL modules** -- SPI slave with CRC-32, IQ deinterleave, U8-to-F32, polyphase FIR (configurable taps up to 128, decimation ratio), radix-2 FFT (1024-point), cross-correlation engine (multi-channel, max 5), config registers with device ID
- **Processing modes** -- FIR-only (mode 0), FIR + cross-correlation (mode 1), cross-correlation only (mode 2), selected via SPI config register
- **Clock architecture** -- 100 MHz system clock from 25 MHz oscillator via ECP5 PLL; separate SPI clock domain for host interface
- **Simulation and verification** -- Icarus Verilog testbenches (`make sim`, `make sim_fir`, `make sim_xcorr`), Python test vector generation and output comparison (`make gen_vectors && make verify`)
- **Integration** -- Bitstream loaded at startup by `fpga_loader.py` when `[fpga] enable = 1`; host communicates via the SPI transport driver

### GPU Compute

On Raspberry Pi 4, the VideoCore VI GPU can accelerate FFT and cross-correlation via OpenCL, reducing CPU load during calibration-intensive phases.

- **Engine** -- `offload_gpu.py` implements radix-2 Cooley-Tukey FFT as OpenCL kernels (bit-reverse permutation + butterfly stages)
- **Operations** -- Forward FFT, inverse FFT, batch cross-correlation (`xcorr_batch`)
- **Self-test** -- Built-in verification against NumPy FFT with configurable tolerance at initialization
- **Graceful fallback** -- Module remains importable without `pyopencl`; `GPUFFTEngine.available` property guards all operations
- **Configuration** -- `[gpu] enable = 1`, `backend = vc4cl`, `offload_fft = 1`, `fft_batch_size = 4`

### Hardware Discovery & Auto-Configuration

At startup, the DAQ chain probes the system for available accelerators and peripherals, then automatically configures optimal transport and compute engine settings.

```mermaid
flowchart LR
    HW[hw_discover.py] -->|probes| HAT["HAT<br/>(device tree + EEPROM)"]
    HW --> GPU_D["GPU<br/>(DRI + OpenCL)"]
    HW --> PCIE_D["PCIe<br/>(sysfs vendor scan)"]
    HW --> USB_D["USB3<br/>(VID:PID table)"]
    HW --> CPU_D["CPU features<br/>(NEON / AVX)"]
    HW --> DMA_D["DMA engines"]
    HW -->|writes| JSON["hw_caps.json"]
    JSON --> AC[auto_config.py]
    AC -->|"updates only<br/>fields set to 'auto'"| INI["daq_chain_config.ini"]
```

- **Probe targets** -- HAT (device tree `/proc/device-tree/hat/` + I2C EEPROM fallback), GPU (DRI devices + pyopencl enumeration), PCIe (sysfs scan for Xilinx/Intel/Lattice/Amazon vendor IDs), USB3 (known VID:PID table), CPU features (NEON/ASIMD from `/proc/cpuinfo`), DMA engines
- **Priority-ordered recommendations** -- PCIe FPGA > GPU OpenCL > CPU NEON > CPU KFR
- **Non-destructive updates** -- `auto_config.py` only modifies fields whose current value is the string `auto` (case-insensitive); pre-configured values are preserved
- **Standalone usage** -- `python3 hw_discover.py` outputs JSON to stdout; `python3 auto_config.py hw_caps.json daq_chain_config.ini`
- **Automatic at startup** -- The start scripts run both tools before launching pipeline processes

## Distributed Operation

### Federation System

Multiple HeIMDALL DAQ instances can operate as a federation, with coordinated frequency scheduling, health monitoring, and IQ stream aggregation across hosts.

```mermaid
graph TD
    subgraph "Federation Control Plane"
        COORD["Coordinator<br/>TCP :6000"]
        HEALTH["Health Monitor<br/>(peer polling)"]
        ELECT["Coordinator Election<br/>(lowest healthy instance_id)"]
        HEALTH --> ELECT
    end

    subgraph "Instance 0 — Host A"
        HWC0["hw_controller :5001"]
        ST0["status_server :5002"]
        IQ0["iq_server :5000"]
    end

    subgraph "Instance 1 — Host B"
        HWC1["hw_controller :5101"]
        ST1["status_server :5102"]
        IQ1["iq_server :5100"]
    end

    subgraph "Federation Data Plane"
        SCHED["FederationScheduler<br/>partition master schedule"]
        ROUTER["IQ Router :7000<br/>aggregates all streams"]
    end

    COORD -->|"fan-out FREQ,<br/>GAIN, STATUS"| HWC0
    COORD --> HWC1
    HEALTH -->|poll| ST0
    HEALTH -->|poll| ST1
    SCHED -->|sub-schedule| HWC0
    SCHED -->|sub-schedule| HWC1
    IQ0 --> ROUTER
    IQ1 --> ROUTER
```

- **Coordinator** (`federation_coordinator.py`) -- Single control endpoint on port 6000; fans out FREQ, GAIN, STATUS, and REBALANCE commands to all registered instances
- **Port formula** -- `base_port + instance_id × port_stride` (default stride=100). Instance 0: ports 5000/5001/5002; Instance 1: 5100/5101/5102; etc.
- **Schedule partitioning** (`federation_scheduler.py`) -- Master frequency schedule divided across healthy instances using `round_robin` (alternate frequency assignment) or `range` (contiguous frequency blocks) strategies
- **Health monitoring** (`federation_health.py`) -- Background thread polls peer StatusServers every 5 seconds; emits `peer_up`, `peer_down`, `peer_degraded` events via EventBus
- **Coordinator election** -- Lowest `instance_id` among healthy peers automatically becomes coordinator; transparent failover on coordinator loss
- **IQ stream aggregation** (`federation_iq_router.py`) -- Connects to each instance's IQ server, tags frames with `unit_id`, and forwards to a unified TCP output on port 7000
- **Instance-aware FIFOs** -- Start script prefixes control FIFO and shared memory names with `inst{N}_` for multi-instance co-location on the same host
- **Configuration** -- `[federation]` section: `instance_id`, `port_stride`, `en_federation`, `coordinator_host`, `coordinator_port`, `peer_list`

```bash
# Start federation coordinator
python3 _daq_core/federation_coordinator.py \
    --port 6000 \
    --instances "localhost:0,192.168.1.10:1,192.168.1.11:2"
```

## Monitoring & Observability

The DAQ pipeline includes a built-in observability stack: structured events, rolling performance metrics, and a JSON status endpoint.

```mermaid
graph LR
    subgraph "Pipeline Modules"
        DS4[delay_sync]
        HWC4[hw_controller]
        SCHED2[scheduler]
    end

    subgraph "Event Bus (non-blocking)"
        EB[EventBus<br/>emit → queue → dispatch]
    end

    subgraph "Handlers"
        LOG[LoggingHandler<br/>Python logging]
        SYS[SysLogHandler<br/>RFC3164 syslog]
        ZMQ2["ZMQ PUB :5003<br/>(topic-filtered)"]
        RING["Ring Buffer<br/>(last 500 events)"]
    end

    subgraph "Metrics"
        MC["MetricsCollector<br/>circular numpy buffers"]
    end

    subgraph "Status API"
        SS["StatusServer<br/>TCP :5002<br/>JSON responses"]
    end

    DS4 --> EB
    HWC4 --> EB
    SCHED2 --> EB
    EB --> LOG
    EB --> SYS
    EB --> ZMQ2
    EB --> RING

    DS4 --> MC
    HWC4 --> MC
    MC --> SS
    RING --> SS
```

### Status Server

TCP endpoint on port 5002 (+ instance offset) accepting line-based commands with JSON responses:

- **PING** -- Health check: `{"ok": true, "ts": <timestamp>}`
- **STATUS** -- Pipeline state: sync state, current frequency, per-channel gains, pipeline health, latency statistics, throughput, uptime
- **METRICS** -- Performance statistics: `{min, max, avg, p95}` for frame processing latency and throughput
- **EVENTS** -- Recent event log from ring buffer

**Health derivation:** `ok` when sync_state >= 5 and zero recent drops; `degraded` when sync_state >= 2; `error` otherwise.

### Metrics Collector

- **O(1) recording** into pre-allocated circular numpy buffers (configurable window, default 1000 samples)
- **Tracked metrics** -- `frame_processing_latency_ms`, `frame_throughput_fps`, and arbitrary named metrics
- **Statistics** -- min, max, avg, p95, count, last value

### Event Bus

- **24+ event types** covering the full pipeline lifecycle: `process_start/stop`, `sync_lock/lost`, `freq_change`, `gain_change`, `overdrive`, `cal_start/sample_done/iq_done/timeout`, `noise_source_on/off`, `schedule_loaded/transition/complete`, `db_error/queue_full`, `frame_drop`, `heartbeat`, `peer_up/down/degraded`, `coordinator_elected`
- **Non-blocking dispatch** -- `emit()` enqueues to background thread; drops silently on queue full
- **Three handler types** -- Python logging (always enabled), syslog (configurable facility and severity threshold), ZMQ PUB (topic = event_type, for remote subscribers)
- **Configuration** -- `[monitoring]` section enables each component independently: `en_monitoring`, `en_syslog`, `en_metrics`, `en_status_server`, `en_zmq_pub`

## Performance Optimization

The build system and start scripts include multiple layers of performance optimization for real-time operation on resource-constrained platforms.

### Compiler Optimization

- **Architecture auto-detection** -- `x86_64` gets `-march=native -Ofast -flto`; ARM gets `-mcpu=cortex-a72` (Pi 4) or `-mcpu=native` with NEON intrinsics
- **Aggressive math flags** -- `-ffast-math -fno-signed-zeros -fno-trapping-math -fassociative-math -freciprocal-math -funroll-loops`
- **Profile-Guided Optimization** -- Two-phase build: `make pgo-profile` (instrumented), run benchmark workload, then `make pgo-optimize` (feedback-optimized)

### Real-Time Scheduling

The start script launches pipeline stages with tiered SCHED_FIFO priorities and CPU core affinity:

| RT Priority | Process | CPU Core | Role |
|-------------|---------|----------|------|
| 99 | rtl_daq.out | 0 | USB I/O critical path |
| 94 | rebuffer.out | 1 | Shared memory handoff |
| 92 | decimate.out | 1 | Cache locality with rebuffer |
| 90 | delay_sync.py | 2 | FFT / cross-correlation |
| 88 | iq_server.out | 2–3 | TCP output |
| 82 | hw_controller.py | 3 | Control plane |

RT throttling is disabled at startup: `sysctl kernel.sched_rt_runtime_us=-1`.

### Performance Profiles

Three presets in `config_files/performance/`:

| Profile | Use Case | Key Settings |
|---------|----------|-------------|
| **minimal** | Low overhead, debugging | Single-thread, no affinity, `-O2` |
| **balanced** | Production (recommended) | CPU affinity, memory locking, IRQ tuning, `-Ofast` + LTO, performance governor |
| **maximum** | Maximum throughput | All of balanced + huge pages, PGO, CPU isolation, core pinning |

## Configuration

All configuration is in `Firmware/daq_chain_config.ini`:

| Section | Key Parameters |
|---------|----------------|
| `[hw]` | `num_ch` (channels), `en_bias_tee` |
| `[daq]` | `center_freq`, `sample_rate`, `gain`, `en_noise_source_ctr` |
| `[pre_processing]` | `cpi_size`, `decimation_ratio`, `fir_tap_size`, `fir_window` |
| `[calibration]` | `cal_track_mode` (0/1/2), `corr_size`, `en_iq_cal`, tolerances |
| `[schedule]` | `en_schedule`, `frequencies`, `dwell_frames`, `repeat_mode` |
| `[database]` | `en_db`, `db_dir`, `rotation_max_age_hours`, `write_batch_size` |
| `[data_interface]` | `out_data_iface_type` (shmem/eth) |
| `[offload]` | `rebuffer_transport`, `decimator_transport`, `fir_engine`, `fft_engine` (all support `auto`) |
| `[monitoring]` | `en_monitoring`, `en_syslog`, `en_metrics`, `en_status_server` (port 5002), `en_zmq_pub` (port 5003) |
| `[federation]` | `instance_id`, `port_stride`, `en_federation`, `coordinator_port`, `peer_list` |
| `[fpga]` | `enable`, `spi_device`, `spi_speed_hz`, `bitstream`, `offload_fir`, `offload_xcorr` |
| `[gpu]` | `enable`, `backend`, `offload_fft`, `fft_batch_size` |
| `[pcie]` | `enable`, `device`, `driver` |
| `[usb3]` | `enable`, `vid`, `pid`, `transfer_size` |

All optional sections (`[schedule]`, `[database]`, `[offload]`, `[monitoring]`, `[federation]`, `[fpga]`, `[gpu]`, `[pcie]`, `[usb3]`) are **disabled by default** for full backward compatibility.

Use `util/cfg_gen.py` for automatic configuration generation from signal parameters:
```bash
python3 util/cfg_gen.py --bri 100 --burst_length 10 --bw 100
```

## Usage

### Quick Start

```bash
# Start with real SDR hardware
cd Firmware
sudo ./daq_start_sm.sh

# Or start in simulation mode (no hardware required)
sudo ./daq_synthetic_start.sh

# Stop the DAQ chain
sudo ./daq_stop.sh
```

### Frequency Scanning Example

Set in `daq_chain_config.ini`:
```ini
[schedule]
en_schedule = 1
schedule_mode = inline
frequencies = 433000000, 868000000, 915000000
dwell_frames = 200, 200, 200
repeat_mode = loop
require_cal_on_hop = 1
```

Or load at runtime via TCP:
```python
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(("localhost", 5001))
payload = json.dumps({"name": "scan", "repeat_mode": "loop", "entries": [
    {"frequency": 433000000, "dwell_frames": 200},
    {"frequency": 868000000, "dwell_frames": 200}
]})
msg = b"SCHD" + payload.encode().ljust(124, b'\x00')
s.send(msg)
```

### Database Queries

```python
from _daq_core.daq_db import DAQDatabase
db = DAQDatabase(db_dir='_db')
# Recent frames
frames = db.get_frame_metrics_by_time_range(start_ts_ms, end_ts_ms)
# Calibration events at a frequency
cal = db.get_cal_history(rf_center_freq=433000000)
# Per-frequency summary
for freq in db.get_freq_scan_summary():
    print(f"{freq.rf_center_freq/1e6:.1f} MHz: {freq.total_frames} frames, SNR {freq.avg_snr:.1f}")
db.close()
```

## Testing

```bash
cd Firmware
# Signal scheduler tests
python3 -m unittest -v _testing/unit_test/test_signal_scheduler.py

# Database record + integration tests
python3 -m unittest -v _testing/unit_test/test_daq_db.py

# Monitoring and federation tests (no sudo required)
python3 -m unittest -v _testing/unit_test/test_monitoring.py
python3 -m unittest -v _testing/unit_test/test_federation.py

# Existing pipeline tests (require unit_test_k4 config)
sudo python3 -W ignore -m unittest -v _testing/unit_test/test_decimator.py
sudo python3 -W ignore -m unittest -v _testing/unit_test/test_delay_sync.py
```

## Build

```bash
cd Firmware/_daq_core
make          # builds rtl_daq.out, rebuffer.out, decimate.out, iq_server.out
make clean
```

Architecture auto-detected: x86_64 links KFR (`libkfr_capi`), ARM links Ne10 (`libNE10.a`) with NEON intrinsics. External libraries must be in `_daq_core/` before building.

Python modules require Python 3.8+ with: numpy, scipy, numba, configparser, pyzmq, scikit-rf. Optional: `berkeleydb` (for persistent storage).

## Manual Installation

Manual install is only required if you are not using the premade images, and are setting up the software from a clean system. If you just want to run the DoA or PR software using a premade image please take a look at our Wiki https://github.com/krakenrf/krakensdr_docs/wiki, specifically the "Direction Finding Quickstart Guide", and the "VirtualBox, Docker Images and Install Scripts" sections.

### Install script

If a premade image does not exist for your computing device, you can use one of our install scripts to automate a fresh install. The script will install heimdall, and the DoA and PR DSP software automatically. Details on the Wiki at https://github.com/krakenrf/krakensdr_docs/wiki/10.-VirtualBox,-Docker-Images-and-Install-Scripts#install-scripts

### Manual Step by Step Install

We recommend using the install script if you are installing to a fresh system instead of doing this step by step install. However, if you are having problems doing the step by step install may help you figure out what is going wrong.

This code should run on any Linux system running on a aarch64(ARM64) or x86_64 systems.

It been tested on [RaspiOS Lite 64-bit](https://downloads.raspberrypi.org/raspios_lite_arm64/images), Ubuntu 64-bit and Armbian 64-bit.

Note that due to the use of conda, the install will only work on 64-bit systems. If you do not wish to use conda, it is possible to install to 32-bit systems. However, the reason conda is used is because the Python repo's don't appear to support numba on several ARM devices without conda.

Steps prefixed with [ARM] should only be run on ARM systems. Steps prefixed with [x86_64] should only be run on x86_64 systems.

1. Install build dependencies
```
sudo apt update
sudo apt install build-essential git cmake libusb-1.0-0-dev lsof libzmq3-dev
```

If you are using a KerberosSDR on a Raspberry Pi 4 with the third party switches by Corey Koval, or an equivalent switch board:

```
sudo apt install pigpio
```

2. Install custom KrakenRF RTL-SDR kernel driver
```
cd
git clone https://github.com/krakenrf/librtlsdr
cd librtlsdr
sudo cp rtl-sdr.rules /etc/udev/rules.d/rtl-sdr.rules
mkdir build
cd build
cmake ../ -DINSTALL_UDEV_RULES=ON
make
sudo ln -s ~/librtlsdr/build/src/rtl_test /usr/local/bin/kraken_test

echo 'blacklist dvb_usb_rtl28xxu' | sudo tee --append /etc/modprobe.d/blacklist-dvb_usb_rtl28xxu.conf
```

Restart the system
```
sudo reboot
```

3. [ARM]  Install the Ne10 DSP library for ARM devices

For ARM 64-bit (e.g. Running 64-Bit Raspbian OS on Pi 4) *More info on building Ne10: https://github.com/projectNe10/Ne10/blob/master/doc/building.md#building-ne10*

```
cd
git clone https://github.com/krakenrf/Ne10
cd Ne10
mkdir build
cd build
cmake -DNE10_LINUX_TARGET_ARCH=aarch64 -DGNULINUX_PLATFORM=ON -DCMAKE_C_FLAGS="-mcpu=native -Ofast -funsafe-math-optimizations" ..
make
 ```

3. [X86_64] Install the KFR DSP library
```bash
sudo apt-get install clang
```
Build and install the library
```bash
cd
git clone https://github.com/krakenrf/kfr
cd kfr
mkdir build
cd build
cmake -DENABLE_CAPI_BUILD=ON -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_BUILD_TYPE=Release ..
make
```

Copy the built library over to the system library folder:

```
sudo cp ~/kfr/build/lib/* /usr/local/lib
```

Copy the include file over to the system includes folder:

```
sudo mkdir /usr/include/kfr
sudo cp ~/kfr/include/kfr/capi.h /usr/include/kfr
```

Run ldconfig to reset library cache:

```
sudo ldconfig
```

4. Install Miniforge

Install via the appropriate script for the system you are using (ARM aarch64 / x86_64)

[ARM]
```
cd
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
chmod ug+x Miniforge3-Linux-aarch64.sh
./Miniforge3-Linux-aarch64.sh
```
Read the license agreement and select ENTER or [yes] for all questions and wait a few minutes for the installation to complete.

[x86_64]
```
cd
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
chmod ug+x Miniforge3-Linux-x86_64.sh
./Miniforge3-Linux-x86_64.sh
```

Restart the Pi, or logout, then log on again.

```
sudo reboot
```

Disable the default base environment.

```
conda config --set auto_activate_base false
```

Restart the Pi, or logout, then log on again.

```
sudo reboot
```

5. Setup the Miniconda Environment

```
conda create -n kraken python=3.9.7
conda activate kraken

conda install scipy==1.9.3
conda install numba==0.56.4
conda install configparser
conda install pyzmq
conda install scikit-rf
```

For persistent database support (optional):
```
conda install -c conda-forge python-berkeleydb
```

6. Create a root folder and clone the Heimdall DAQ Firmware

```
cd
mkdir krakensdr
cd krakensdr

git clone https://github.com/krakenrf/heimdall_daq_fw
cd heimdall_daq_fw
```

7. Build Heimdall C files

Browse to the _daq_core folder

```
cd ~/krakensdr/heimdall_daq_fw/Firmware/_daq_core/
```

Copy librtlsdr library and includes to the _daq_core folder

```
cp ~/librtlsdr/build/src/librtlsdr.a .
cp ~/librtlsdr/include/rtl-sdr.h .
cp ~/librtlsdr/include/rtl-sdr_export.h .
```

[ARM] If you are on an ARM device, copy the libNe10.a library over to _daq_core
```
cp ~/Ne10/build/modules/libNE10.a .
```

[PI 4 ONLY] If you are using a KerberosSDR with third party switches by Corey Koval, or equivalent, make sure you uncomment the line `PIGPIO=-lpigpio -DUSEPIGPIO` in the Makefile. If not, leave it commented out.

```
nano Makefile
```

Make your changes, then Ctrl+X, Y to save and exit nano.

[ALL] Now build Heimdall

```
make
```

## Intel Optimizations:
If you are running a machine with an Intel x86_64 CPU, you can install the highly optimized Intel MKL BLAS and Intel SVML libraries for a significant speed boost. Installing on AMD CPUs can also help.

```
conda activate kraken
conda install "blas=*=mkl"
conda install -c numba icc_rt
```

## Next Steps:

Now you will probably want to install the direction of arrival DSP code found in https://github.com/krakenrf/krakensdr_doa.

## Project Structure

```
heimdall_daq_fw/
├── Firmware/
│   ├── _daq_core/                    # Core pipeline modules
│   │   ├── rtl_daq.c/h               # Multi-tuner SDR reader
│   │   ├── rebuffer.c                # CPI block reshaper
│   │   ├── fir_decimate.c            # FIR filter + decimation
│   │   ├── iq_server.c               # TCP IQ data server
│   │   ├── iq_header.h/c/py          # 1024-byte IQ frame header (C + Python)
│   │   ├── delay_sync.py             # Delay & IQ synchronizer
│   │   ├── hw_controller.py          # Hardware control + scheduling
│   │   ├── signal_scheduler.py       # Dynamic frequency scheduler
│   │   ├── shmemIface.py             # Shared memory interface
│   │   ├── transportIface.py         # Pluggable transport abstraction (Python)
│   │   ├── transport.c/h             # Transport vtable dispatch (C)
│   │   ├── transport_shm.c           # Shared memory transport driver
│   │   ├── transport_spi.c           # SPI+DMA transport driver
│   │   ├── transport_pcie.c          # PCIe/XDMA transport driver
│   │   ├── transport_usb3.c          # USB 3.0 transport driver
│   │   ├── transport_net.c           # TCP/Ethernet transport driver
│   │   ├── offload.c/h               # Offload engine vtable dispatch (C)
│   │   ├── offload_cpu_neon.c        # ARM NEON FIR/convert engine
│   │   ├── offload_cpu_kfr.c         # x86 KFR FIR/convert engine
│   │   ├── offload_fpga.c            # FPGA FIR engine (SPI-based)
│   │   ├── offload_gpu.c             # GPU compute engine (C)
│   │   ├── offload_engines.py        # Python FFT/correlation engines
│   │   ├── offload_gpu.py            # GPU FFT via OpenCL (Python)
│   │   ├── daq_db.py                 # BerkeleyDB persistent storage
│   │   ├── daq_db_records.py         # Binary record definitions
│   │   ├── daq_status_server.py      # JSON status endpoint (TCP :5002)
│   │   ├── daq_metrics.py            # Rolling performance metrics
│   │   ├── daq_events.py             # Structured event bus
│   │   ├── federation_coordinator.py # Multi-instance coordinator
│   │   ├── federation_scheduler.py   # Distributed schedule partitioning
│   │   ├── federation_health.py      # Peer health monitoring
│   │   ├── federation_iq_router.py   # IQ stream aggregation
│   │   ├── hw_discover.py            # Hardware capability discovery
│   │   ├── auto_config.py            # Auto-configuration from capabilities
│   │   └── inter_module_messages.py  # ZMQ message protocol
│   ├── _fpga_gateware/               # FPGA acceleration
│   │   ├── rtl/                      # Verilog RTL source
│   │   │   ├── top.v                 # Top-level (PLL, SPI, orchestration)
│   │   │   ├── spi_slave.v           # SPI interface + CRC-32
│   │   │   ├── fir_decimate.v        # Polyphase FIR filter
│   │   │   ├── fft_radix2.v          # Radix-2 FFT engine
│   │   │   ├── xcorr_engine.v        # Cross-correlation engine
│   │   │   └── ...                   # Supporting modules
│   │   ├── tb/                       # Testbenches (Icarus Verilog)
│   │   ├── constraints/              # Pin constraints (ECP5, iCE40)
│   │   └── Makefile                  # Yosys + nextpnr build
│   ├── _testing/                     # Test suite
│   ├── _data_control/                # Runtime control FIFOs
│   ├── _db/                          # Database files (runtime)
│   ├── _logs/                        # Log files (runtime)
│   ├── daq_chain_config.ini          # Main configuration
│   ├── daq_start_sm.sh               # Start with real hardware
│   ├── daq_synthetic_start.sh        # Start in simulation mode
│   ├── daq_stop.sh                   # Stop all processes
│   └── ini_checker.py                # Configuration validator
├── config_files/
│   ├── kraken_default/               # 5-channel KrakenSDR preset
│   ├── kerberos_default/             # KerberosSDR preset
│   ├── unit_test_k4/                 # Test configuration
│   └── performance/                  # Performance profiles
│       ├── minimal.conf              # Low overhead
│       ├── balanced.conf             # Production (recommended)
│       └── maximum.conf              # Maximum throughput
├── util/
│   ├── cfg_gen.py                    # Auto config generator
│   ├── system_tuning.py              # RT system optimization
│   └── performance_monitor.py        # Per-process perf tracking
└── Documentation/
```

## License

GNU General Public License v3.0

Authors: Tamas Peto, Carl Laufer
