# HeIMDALL DAQ Firmware Documentation

This directory documents the HeIMDALL DAQ firmware. The firmware is the coherent data acquisition and signal processing chain for KrakenSDR and KerberosSDR multichannel RTL-SDR receivers. Start with the reading path for your role below, or pick a document from the groups.

The code is the ground truth. When a document and the source disagree, the source wins. Please report the discrepancy.

## Reading paths

| Role | Read in this order |
|---|---|
| New user: one station, one receiver set | [hardware_setup.md](hardware_setup.md) → [deployment_recipes.md](deployment_recipes.md) (Recipe A or E) → [operations_runbook.md](operations_runbook.md) sections 1-4 → [tutorial_directional_df.md](tutorial_directional_df.md) if you use LNAs or a rotator → [glossary_faq.md](glossary_faq.md) as needed |
| Integrator: a DSP, DoA or recording program that reads the IQ stream | [consumer_guide.md](consumer_guide.md) → [protocols.md](protocols.md) (IQ frames, ports 5000, 5001, 5002, 7000) → [theory_coherent_acquisition.md](theory_coherent_acquisition.md) sections 5-9 → [configuration.md](configuration.md) `[data_interface]` and `[federation]` |
| Operator of a cluster: several instances on one host or several hosts | [scaling_and_clustering.md](scaling_and_clustering.md) → [capacity_planning.md](capacity_planning.md) → [hardware_setup.md](hardware_setup.md) section 3 → [deployment_recipes.md](deployment_recipes.md) (Recipes C and D; Recipe F needs units that are not shipped yet) → [networking_and_time_sync.md](networking_and_time_sync.md) → [operations_runbook.md](operations_runbook.md) → [upgrade_and_rollback.md](upgrade_and_rollback.md) |
| Developer: changes to the code, tests or CI | [architecture.md](architecture.md) → [developer_guide.md](developer_guide.md) → [../CONTRIBUTING.md](../CONTRIBUTING.md) → [protocols.md](protocols.md) and [configuration.md](configuration.md) as references → [upgrade_and_rollback.md](upgrade_and_rollback.md) section 4 (the default-path ledger you must not extend silently) |

## Background

| Document | Purpose |
|---|---|
| [theory_coherent_acquisition.md](theory_coherent_acquisition.md) | Explains coherence, the receiver impairments that break it, what each pipeline stage does to the samples, the calibration theory, and why two receiver units are never coherent with each other |
| [architecture.md](architecture.md) | Describes the process pipeline, shared-memory and FIFO IPC, startup and real-time layout, the sync state machine, instance namespacing, supervision, federation components and the capacity model |

## Getting started and practical application

| Document | Purpose |
|---|---|
| [hardware_setup.md](hardware_setup.md) | Takes receivers from the box to an openable chain: serial numbers, programming serials for a second unit, USB topology, antennas, LNAs and bias tees, rotators, Raspberry Pi 4 notes and a bring-up checklist |
| [deployment_recipes.md](deployment_recipes.md) | Gives end-to-end recipes with exact commands and config: a DoA station on a Pi 4, passive radar, two units on one x86 host, multi-site networked DF, a hardware-free rehearsal and systemd supervision. [Recipe F](deployment_recipes.md#recipe-f-systemd-supervised-deployment) depends on systemd units that are specified but not in this tree yet. Until they land, run `daq_supervise.sh` and `federation_start.sh --foreground` from your own units |
| [tutorial_directional_df.md](tutorial_directional_df.md) | Walks through the `directional_df` preset: amplified receivers, a high-gain antenna, rotator control, scan-and-peak and the v8 header telemetry |
| [consumer_guide.md](consumer_guide.md) | Tells a downstream program how to attach to `:5000`, shared memory or the router `:7000`, which header fields and slots 102-113 to read, how to accept frames and how to detect loss |

## Scaling and clustering

All features in this group are opt-in. With the shipped configuration a single instance runs exactly as before.

| Document | Purpose |
|---|---|
| [scaling_and_clustering.md](scaling_and_clustering.md) | The single reference for more than one instance: the coherent-island model, per-instance and host-global resources, several instances per host, multi-host federation, election, scheduling, the IQ router, failure modes and checklists |
| [capacity_planning.md](capacity_planning.md) | Sizes a host and its network: the per-instance resource model, worked numbers, host budgets, the `daq_preflight.py` check and the safe throughput levers |
| [networking_and_time_sync.md](networking_and_time_sync.md) | Lists every port and bind address, the trust model (`auth_token`, `allow_cidrs`), firewall and WireGuard examples, bandwidth per instance and the NTP/PTP requirements for comparing timestamps across hosts |

## Operations

| Document | Purpose |
|---|---|
| [operations_runbook.md](operations_runbook.md) | Start, stop and restart per instance and per cluster, health checks, the log and PID file legend, symptom → cause → fix tables, procedures and what to collect for a bug report |
| [upgrade_and_rollback.md](upgrade_and_rollback.md) | Version identifiers, INI compatibility, the complete list of default-path changes (ledger L1-L16), rebuild rules, rolling a fleet, mixed-version clusters and rollback |

## Reference

| Document | Purpose |
|---|---|
| [protocols.md](protocols.md) | The normative wire reference: the 1024-byte IQ header v8 with slots 102-113, ZMQ `:1130`, control `:5001` (including `XACK`), IQ `:5000`, status `:5002`, events `:5003`, coordinator `:6000`, router `:7000`/`:7001` and control files |
| [configuration.md](configuration.md) | Every `daq_chain_config.ini` and `federation.ini` key with type, default, valid values, the process that reads it and its effect, plus the preset catalog |
| [glossary_faq.md](glossary_faq.md) | Defines the terms (instance_id, unit_id, node key, CPI, track lock, term, lease and more) and answers common operator questions |

## Development

| Document | Purpose |
|---|---|
| [developer_guide.md](developer_guide.md) | Building the C core (Makefile variables, PGO), the Python environment, the test lanes (`--ci`, `--sudo`, pending modules), CI jobs, adding a module or an opt-in feature, packaging and the coding invariants |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | How to contribute: local builds, the two test lanes, adding a test module, invariants, the opt-in feature checklist, pull request expectations and code style |
| [../CHANGELOG.md](../CHANGELOG.md) | Release notes, including the `[Unreleased]` scaling and clustering program |

## Other documents in the tree

| Path | Purpose |
|---|---|
| [`../packaging/systemd/README.md`](../packaging/systemd/README.md) | Installing the shipped systemd units (`heimdall-daq.service`, `heimdall-daq@.service`) |
| [`../config_files/performance/README.md`](../config_files/performance/README.md) | The `--profile` snippets (`minimal.ini`, `balanced.ini`, `maximum.ini`) and why the `.conf` files are documentation only |

## Where things live in the repository

The table lists the main paths. It is not exhaustive.

| Path | Contents |
|---|---|
| `Firmware/_daq_core/` | Pipeline sources: C stages (`rtl_daq.c`, `rebuffer.c`, `fir_decimate.c`, `iq_server.c`), Python stages (`delay_sync.py`, `hw_controller.py`), transport and offload layers, monitoring, database, federation (coordinator, election, health, scheduler, IQ router), RF front-end and orientation modules, `capacity_model.py`, `version.py` |
| `Firmware/daq_chain_config.ini` | The live configuration of instance 0 (see [configuration.md](configuration.md)) |
| `Firmware/daq_start_sm.sh`, `daq_synthetic_start.sh`, `daq_stop.sh`, `daq_common.sh` | Chain start (hardware or synthetic), stop, and the shared helpers they source |
| `Firmware/daq_supervise.sh` | Per-instance supervisor: starts the chain, watches every stage and the status server, and on the first failure stops the instance and exits with a code that names the failed stage |
| `Firmware/federation_start.sh`, `federation_stop.sh` | Launch and stop the coordinator and the IQ router |
| `Firmware/daq_preflight.py`, `ini_checker.py` | Capacity pre-flight check and config validator |
| `Firmware/unit_test.sh` | Test runner (`--ci`, `--sudo`, `--list`) |
| `Firmware/_testing/` | Unit tests, test data generators, the synthetic source, IQ recorder and analyzer |
| `Firmware/_data_control/` | Runtime FIFOs, FIR coefficients, hardware capability cache, relay and control files |
| `Firmware/_db/` | Default BerkeleyDB directory (`[database] db_dir = _db`). Provisioning gives each instance N > 0 its own private `_db/` |
| `Firmware/_calibration/` | Touchstone cable files `cable_ch<i>.s1p`, read by delay_sync only with `[calibration] iq_adjust_source = touchstone` (needs scikit-rf) |
| `Firmware/_logs/` | Instance 0 logs (`*.log`), instance N logs under `inst<N>/`, PID files under `inst<N>/pids/`, federation services under `federation/` |
| `Firmware/_fpga_gateware/` | Experimental FPGA gateware (ECP5/iCE40, Yosys + nextpnr) |
| `config_files/` | Presets, including `directional_df/`, `multi_instance_x86/`, `cluster_site/` and `performance/` |
| `util/heimdall_ctl/` | The `heimdall-ctl` client, the only pip-installable package. In a source tree run it as `PYTHONPATH=util python3 -m heimdall_ctl` from the repository root |
| `util/provision_instance.py`, `util/cluster_cfg_gen.py`, `util/set_unit_serials.sh`, `util/host_tuning.sh` | Instance-directory provisioning, cluster config generation, EEPROM serial programming and host-global tuning |
| `util/install.sh` | Dependency build and copy-in installer (librtlsdr fork, KFR/Ne10) |
| `util/irq_tuning.sh`, `util/kernel_tuning.conf`, `util/system_tuning.py`, `util/kerberos_eeprom_init.sh` | Legacy IRQ and kernel tuning helpers, and KerberosSDR EEPROM initialisation |
| `ci/build.sh`, `.github/workflows/ci.yml` | Reproducible CI lanes (generic, kfr, asan, deploy-lint) and the GitHub Actions workflow |
| `packaging/` | systemd units and logrotate policy |

## Historical material

`HDAQ_firmware_ver1.0.20201130.pdf` in this directory is the original 2020 firmware documentation. It is historical. It predates the transport and offload layers, the v8 IQ header, federation, monitoring, the RF front-end and orientation subsystems, the scaling program and the current build system. Do not use it as a wire or configuration reference.
