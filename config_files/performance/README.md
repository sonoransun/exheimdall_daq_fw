# Performance profiles

Two kinds of files live here:

| File | Status | Read by |
|---|---|---|
| `minimal.ini`, `balanced.ini`, `maximum.ini` | **active** snippets (`[host]` + `[offload]` keys) | `util/provision_instance.py --profile NAME` |
| `minimal.conf`, `balanced.conf`, `maximum.conf` | **documentation only** | nothing: no script, service or DAQ process parses them |

The legacy `.conf` files describe intent (`[performance]` keys such as
`rebuffer_priority = 98` or `enable_huge_pages = 1`), but no part of the firmware ever read
them. They are kept unchanged for reference. To apply a profile, provision the instance
with it:

```bash
sudo python3 util/provision_instance.py --instance 1 --profile balanced            # dry run
sudo python3 util/provision_instance.py --instance 1 --profile balanced --write
```

`provision_instance.py` writes its defaults first, then merges the profile's keys,
then applies the explicit flags, so `--rt-offset 2 --profile minimal` gives
`rt_priority_offset = 2`. With `--systemd`, `[host] host_tuning` is always `0`: the
`heimdall-host-tuning.service` unit does the host tuning.

## What each snippet sets

| Key | minimal | balanced | maximum |
|---|---|---|---|
| `[host] rt_priority_offset` | `4` (tiers 95/90/88/86/84/78) | `0` (99/94/92/90/88/82) | `0` |
| `[host] allow_core_sharing` | `warn` | `refuse` | `refuse` |
| `[host] preflight` | `warn` | `warn` | `strict` (a failed pre-flight blocks the start, exit 4) |
| `[offload] fft_workers`, `xcorr_workers` | `1` | `auto` | `auto` |

`auto` means one worker per `[host] delay_sync_cores` entry, else one per core of
delay_sync's CPU affinity. The affinity is the single T2 core when `delay_sync_cores` is
empty. With `maximum`, also pass `--delay-sync-cores` so the workers have cores of their
own. The worker count only splits independent transforms, so the results are
bit-identical whatever the count.

## Mapping from the legacy `.conf` keys

| Legacy key (`.conf`) | Where it is set now |
|---|---|
| `rtl_daq_priority` … `iq_server_priority` | The RT tiers are fixed at 99/94/92/90/88/82 in the start script. `[host] rt_priority_offset` lowers them all by the same amount. Per-process priorities cannot be set. |
| `enable_cpu_affinity` | Always on. `[host] cpu_cores` names each instance's 4 cores (default `4N..4N+3` from `provision_instance.py`), and `[host] delay_sync_cores` widens delay_sync's set. |
| `enable_irq_tuning`, `cpu_governor`, `enable_rt_throttling_disable` | `util/host_tuning.sh --apply` (sched_rt_runtime, IRQ pinning via `util/irq_tuning.sh`, usbfs, drop_caches), controlled by `[host] host_tuning` and `[host] usb_irq_core` / `usb_controller`. |
| `enable_memory_locking`, `enable_huge_pages`, `swap_disable`, `memory_overcommit_disable`, `enable_network_tuning` | `util/system_tuning.py` / `util/kernel_tuning.conf` (host-wide sysctl), applied once per host. They are not per instance. |
| `enable_cpu_isolation`, `enable_nohz_full`, `enable_rcu_nocbs` | Kernel command line (`isolcpus=`, `nohz_full=`, `rcu_nocbs=`). Set them by hand to match the `cpu_cores` plan. |
| `optimization_level`, `enable_lto`, `enable_pgo`, `enable_fast_math`, `enable_unsafe_math` | Build time: `make BUILD=release` (default `-Ofast -flto`), `make pgo-profile` / `make pgo-optimize` in `Firmware/_daq_core`. |
| `enable_simd_acceleration` | Build time: `make ENGINE=kfr\|ne10\|generic` and `MARCH=`. |
| `enable_gpu_offload`, `enable_fpga_offload`, `enable_transport_optimization` | `[offload]`, `[gpu]`, `[fpga]` sections (experimental). |
| `enable_batched_control`, `enable_federation_optimization` | No equivalent. Nothing ever implemented them. |
| — (new) | `[host] preflight` (capacity pre-flight, `Firmware/daq_preflight.py`) and `[host] allow_core_sharing` (the core-overlap guard between instances). |
