# Networking, Ports, Security and Time Synchronisation

This document lists every socket the DAQ chain and the federation services open, where each one binds, who must reach it, and how to protect it. It ends with the clock requirements for comparing timestamps across hosts.

Every cluster feature here is **opt-in**. With the shipped `daq_chain_config.ini`, no coordinator or router runs and no new key is set. The legacy ports keep their wire formats byte for byte.

Related documents: [protocols.md](protocols.md) (wire formats), [configuration.md](configuration.md) (all keys), [scaling_and_clustering.md](scaling_and_clustering.md) (instance and cluster model), [deployment_recipes.md](deployment_recipes.md) (worked setups), [operations_runbook.md](operations_runbook.md) (start, stop, symptoms), [capacity_planning.md](capacity_planning.md) (host sizing), [theory_coherent_acquisition.md](theory_coherent_acquisition.md) (why alignment is wall-clock only).

## 1. Port map

### 1.1 Per-instance ports

Each instance offsets its ports by `instance_id × port_stride`. Both keys live in `[federation]` of the instance INI. The defaults are `instance_id = 0` and `port_stride = 100`. The formula is `compute_port()` in `sh_mem_util.c` and `compute_ports()` in `federation_common.py`.

| Base port | Process | Protocol | Runs when | Bind key |
|---|---|---|---|---|
| 1130 | `rtl_daq.out` (or the synthesizer) | ZMQ REP, 128-byte tuner commands | always | `[daq] zmq_listen_address` |
| 5000 | `iq_server.out` | TCP, `streaming` + per-frame `IQDownload` | `[data_interface] out_data_iface_type = eth` | `[daq] listen_address` |
| 5001 | `hw_controller.py` | TCP, 128-byte control frames, `FNSD` replies | always | `[daq] listen_address` |
| 5002 (`[monitoring] status_server_port`) | `delay_sync.py` status server | TCP, one command line, JSON + `\n` | `en_monitoring = 1` and `en_status_server = 1` | `[daq] listen_address` |
| 5003 (`[monitoring] zmq_pub_port`) | `delay_sync.py` event publisher | ZMQ PUB | `en_monitoring = 1` and `en_zmq_pub = 1` | `[daq] listen_address` |

The shipped INI sets `out_data_iface_type = shmem`, so port 5000 is closed by default. The start script falls back to `eth` only when the key is missing. The shipped INI also sets `en_monitoring = 0`, so 5002 and 5003 are closed by default.

`heimdall-ctl ports` prints the map of one instance. It reads no network. This is real output against the shipped INI:

```console
$ heimdall-ctl --config Firmware/daq_chain_config.ini --instance 1 ports
instance 1 (offset 100)
service                port  note
zmq (rtl_daq control)  1230
iq (iq_server)         5100
ctl (hw_controller)    5101
status                 5102  disabled in config
events                 5103  disabled in config
coordinator            6000  cluster service
router                 7000  cluster service
router stats           7001  cluster service
```

Worked rows for instance ids 0 to 3 with the default stride:

| Instance | ZMQ | IQ | Control | Status | Events |
|---|---|---|---|---|---|
| 0 | 1130 | 5000 | 5001 | 5002 | 5003 |
| 1 | 1230 | 5100 | 5101 | 5102 | 5103 |
| 2 | 1330 | 5200 | 5201 | 5202 | 5203 |
| 3 | 1430 | 5300 | 5301 | 5302 | 5303 |

### 1.2 Cluster service ports

The coordinator and the router are single services per process. Their ports are **not** offset by `instance_id`. Neither process is started by `daq_start_sm.sh`. Start them with `federation_start.sh` or directly with the commands in section 2.3.

| Port | Process | Protocol | Config key (`[federation]` of its `federation.ini`) | CLI flag |
|---|---|---|---|---|
| 6000 | `federation_coordinator.py` | TCP, one request per connection, JSON reply | `coordinator_port` | `--port` |
| 7000 | `federation_iq_router.py` output | TCP, raw `[header][payload]` stream, no handshake | `router_port` | `--output-port` |
| 7001 | `federation_iq_router.py` stats | TCP, one command line, JSON + `\n` | `router_stats_port` (0 = off) | `--stats-port` |

<!-- verify-after: WP-08 -->
<!-- verify-after: WP-09 -->

### 1.3 Safe instance-id range

An instance's 5000-5003 window moves by 100 per id. With stride 100, instance 10 lands on 6000-6003 and instance 20 lands on 7000-7003. Pick ids that keep clear of the service ports on the same host.

| Services on this host | Safe ids with stride 100 |
|---|---|
| none (instances only) | 0 to 9 recommended, so a coordinator can be added later |
| router (7000, 7001) | 0 to 19 |
| coordinator (6000) | 0 to 9 |
| coordinator and router | 0 to 9 |

The stride must be at least 4 for any instance with id > 0. A smaller stride overlaps the 5000-5003 windows of neighbouring instances.

`ini_checker.py` reports these collisions. On an existing INI they are warnings and the exit code stays 0. In new-key mode they are errors. New-key mode means the INI sets `cluster_members`, `coordinator_candidates`, `register_with_coordinator = 1`, any `router_*` key, `[host] instance_registry = 1` or `[host] instance_guard = refuse`. A coordinator counts as local when `coordinator_host` or a `coordinator_candidates` entry is this host. A router counts as local when any `router_*` key is present.

Real output, instance 10 with `coordinator_host = localhost` (no new keys, warning, exit 0):

```console
$ python3 ini_checker.py i10.ini no_hw
WARNING: port 6000 of instance 10 collides with the coordinator port; with stride 100 use instance ids 0-9
```

Instance 20 with `router_port = 7000` set (new-key mode, error, exit 1):

```console
$ python3 ini_checker.py i20.ini no_hw
ERROR:root:port 7000 of instance 20 collides with the router port; with stride 100 use instance ids 0-19
ERROR:root:port 7001 of instance 20 collides with the router stats port; with stride 100 use instance ids 0-19
```

Instance 1 with `port_stride = 3` (warning, exit 0):

```console
$ python3 ini_checker.py s3.ini no_hw
WARNING: [federation] port_stride = 3 is below 4 on instance 1: its ports overlap another instance's 5000-5003 window
```

The checker also warns when `1130 + id × stride` falls inside another instance's 5000-5003 window, evaluated for ids 0 to 15. Stride 100 never triggers this.

### 1.4 Other listeners in optional builds

These sockets exist only in builds that enable them. None is in the default build.

| Component | Port | Bind | Note |
|---|---|---|---|
| `hat_usb3_bridge.c`, `hat_pcie_bridge.c` | `1130 + offset` | `tcp://*`, fixed | replaces `rtl_daq` as the ZMQ responder; `zmq_listen_address` is not read |
| `transport_net.c` (`-DHAS_NET_TRANSPORT`) | 5100 when the channel name gives no port | `INADDR_ANY`, fixed | 5100 equals instance 1's IQ port; give an explicit port |

## 2. Bind addresses

### 2.1 Which socket honours which key

| Socket | Key or flag | Default | Accepted values |
|---|---|---|---|
| 1130 + offset (rtl_daq, synthesizer) | `[daq] zmq_listen_address` | absent = `tcp://*` | `*`, `0.0.0.0`, `127.0.0.1`, `localhost` (mapped to `127.0.0.1`) |
| 5000 + offset (iq_server) | `[daq] listen_address` | `0.0.0.0` | one IPv4 address |
| 5001 + offset (hw_controller) | `[daq] listen_address` | `0.0.0.0` | one IPv4 address |
| 5002 + offset (status server) | `[daq] listen_address` | `0.0.0.0` | one IPv4 address |
| 5003 + offset (event PUB) | `[daq] listen_address` | `0.0.0.0` | one IPv4 address |
| 6000 (coordinator) | `--listen-address`, else `[daq] listen_address` of its `--config` file | `0.0.0.0` | IPv4 or IPv6 (an address with `:` opens an IPv6 socket) |
| 7000, 7001 (router) | `--listen-address`, else `[daq] listen_address` of its `--config` file | `0.0.0.0` | IPv4 or IPv6, both ports share it |

`ini_checker.py` rejects a `[daq] listen_address` that is not an IPv4 address. It rejects any other `zmq_listen_address` value with this text: `hw_controller and delay_sync connect to tcp://localhost, so binding another address would cut the local chain`.

`zmq_listen_address` is deliberately **not** derived from `listen_address`. Set `zmq_listen_address = 127.0.0.1` on every host that is reachable from an untrusted network. Port 1130 has no authentication and accepts the `h` halt command.

### 2.2 Binding to one interface address

`[daq] listen_address` takes exactly one address. If you set it to a LAN or WireGuard address, the loopback address stops working for 5000 to 5003. Three local clients connect to `127.0.0.1` by default:

| Local client | Connects to | Effect of a non-loopback `listen_address` |
|---|---|---|
| `heimdall-ctl` | `--host`, default `127.0.0.1` | pass `--host <that address>` |
| router self entry (`router_include_self`, default 1; added only when membership comes from `peer_list` in `--config`) | `127.0.0.1:5000+offset` | the self source never connects; list the instance explicitly instead |
| coordinator self entry (`coordinator_include_self = 1`, default 0; `peer_list`/`--instances` mode only) | `127.0.0.1` ports of the instance | same; list the instance explicitly |

With `--sources`, `router_sources` or `cluster_members` the router has no self entry. With `cluster_members` the coordinator has none either.

The simpler pattern is to keep `listen_address = 0.0.0.0` and restrict access with a firewall (section 5) or an overlay interface (section 6).

### 2.3 Starting the cluster services

Both services are opt-in. They run unprivileged: no sudo, no RT priority, no core pinning. Run the commands from `Firmware/`.

The launcher script starts either or both in the background. It writes logs to `_logs/federation/` and PID files to `_logs/federation/pids/`:

```bash
cd Firmware
./federation_start.sh all --config /etc/heimdall/federation.ini   # coordinator + router
./federation_stop.sh all
```

`--foreground` runs one service in the foreground, for a systemd unit. Launcher exit codes: 0 started, 1 the service exited immediately, 2 usage error, 3 already running.

The direct commands:

```bash
cd Firmware
python3 _daq_core/federation_coordinator.py --config /etc/heimdall/federation.ini \
    --log-file _logs/coordinator.log
python3 _daq_core/federation_iq_router.py --config /etc/heimdall/federation.ini \
    --log-file _logs/router.log
```

For supervised operation see [deployment_recipes.md](deployment_recipes.md#5-coordinator-and-routers).

The coordinator CLI (real `--help` text):

```console
$ python3 _daq_core/federation_coordinator.py --help
usage: federation_coordinator.py [-h] [--config CONFIG] [--port PORT]
                                 [--listen-address LISTEN_ADDRESS]
                                 [--instances INSTANCES] [--self SELF_EP]
                                 [--state-dir STATE_DIR]
                                 [--auth-token AUTH_TOKEN]
                                 [--pid-file PID_FILE] [--log-file LOG_FILE]
                                 [--log-level LOG_LEVEL]
```

Precedence is CLI flag, then the `--config` value, then the built-in default. The effective values are logged at start, with the token shown only as `auth=on` or `auth=off`. Exit codes: 0 normal stop, 2 bind failure, 3 configuration error. A bad command-line flag is a configuration error (exit 3), not argparse's usual 2:

```console
$ python3 _daq_core/federation_coordinator.py --bogus; echo "exit $?"
2026-11-01 01:08:16,070 [federation_coordinator] ERROR: configuration error: federation_coordinator.py: unrecognized arguments: --bogus
configuration error: federation_coordinator.py: unrecognized arguments: --bogus
exit 3
```

The router CLI (abridged real `--help` text, network flags only):

```console
$ python3 _daq_core/federation_iq_router.py --help
  --config PATH         INI file; reads [federation] router_* / membership
                        keys and [daq] listen_address
  --sources LIST        comma-separated host:iid[@node_id] (iq_server) and
                        router://host:port (upstream router) entries
  --output-port PORT    unified output port (7000)
  --listen-address ADDR
                        bind address of the output and stats ports (0.0.0.0)
  --stats-port PORT     stats port, 0 = off (7001)
  --connect-on-demand   connect to sources only while a client is attached
                        (default)
  --always-connect      keep every source connected
  --upstream-stats LIST
                        host:port stats endpoints of upstream routers (node
                        key names)
  --auth-token T        token required on the stats port
  --allow-cidrs LIST    only accept output/stats clients from these networks
```

Flags override `--config` values. The router logs every effective setting at start, with the token printed as `***`. Exit codes: 0 normal stop, 2 output or stats port bind failure, 3 bad arguments or configuration. The router refuses a stats port equal to its output port.

<!-- verify-after: WP-08 -->
<!-- verify-after: WP-09 -->

## 3. Trust model and access control

### 3.1 What each port trusts

The legacy ports have frozen wire formats. They cannot carry a token, so they have **no authentication**. Anyone who reaches them can control the chain.

| Port | Authentication | Allow-list | What an unauthenticated client can do |
|---|---|---|---|
| 1130 + offset | none | none | retune, change gains, toggle the noise source and bias tees, halt `rtl_daq` (`h`) |
| 5000 + offset | none | none | read the full IQ stream; with one client slot (default), block other IQ clients |
| 5001 + offset | none | none | retune, change gains, AGC, schedules, orientation; hold the single connection slot (default) |
| 5002 + offset | none | none | read status, metrics, events and database summaries |
| 5003 + offset | none | none | subscribe to all events |
| 6000 | `auth_token` (opt-in) | `allow_cidrs` (opt-in) | without either: every coordinator verb, including fleet-wide `FREQ`, `GAIN` and schedules |
| 7000 | none | `allow_cidrs` (opt-in) | read the merged IQ stream |
| 7001 | `auth_token` (opt-in) | `allow_cidrs` (opt-in) | read router statistics |

Protect ports 1130 and 5000 to 5003 with binding (section 2), a firewall (section 5) or an overlay network (section 6).

### 3.2 `auth_token`

`[federation] auth_token` is empty by default, which means no authentication. When set, every request to 6000 and 7001 must start with `AUTH <token> `. The servers compare tokens with `hmac.compare_digest`. A missing or wrong token gets `{"ok": false, "error": "auth_required"}`.

The token must be printable ASCII, contain no spaces, and be at most 128 characters long. Use one token for the whole cluster, because coordinators and routers present their own token to each other.

| Component | Reads the token from | Sends it to |
|---|---|---|
| coordinator (server) | `[federation] auth_token` of its `--config`, or `--auth-token` | checks it on 6000 |
| coordinator (client) | the same setting | other candidates' 6000 (`LEADER`, `PREVOTE`, `LEASE`, `SCHEDULE SYNC`, `SCHEDULE GET SYNC`), the router's 7001 (`STATS`, sent when a client asks the coordinator `ROUTER STATS`) |
| router (server) | `[federation] auth_token` of its `--config`, or `--auth-token` | checks it on 7001 only, never on 7000 |
| router (client) | the same setting | upstream routers' 7001 (`--upstream-stats`) |
| `delay_sync.py` registrar | `[federation] auth_token` of the instance INI | the coordinator's 6000 (`REGISTER`, with `register_with_coordinator = 1`) |
| `heimdall-ctl` | `--auth-token`, else `$HEIMDALL_AUTH_TOKEN`, else `[federation] auth_token` of the resolved config | 6000 (`fleet`) and 7001 (`router`) |

<!-- verify-after: WP-06 -->
<!-- verify-after: WP-08 -->
<!-- verify-after: WP-09 -->

Real exchange with a coordinator started with `--auth-token s3cret-token`. Legacy verbs keep their reply framing with no trailing newline:

```console
> PING
{"ok": false, "error": "auth_required"}
> AUTH s3cret-token PING
{"ok": true, "ts": 1793518313.90559}
```

The same against the router stats port. Stats replies end with `\n`:

```console
> PING
{"ok": false, "error": "auth_required"}
> AUTH s3cret-token PING
{"ok": true, "ts": 1793518313.906877}
```

`heimdall-ctl fleet` prints `Error: auth_required` and exits 1 when the coordinator rejects its token.

Keep the token out of command lines. `--auth-token` is visible to every local user in `ps` and `/proc/<pid>/cmdline`. Put `[federation] auth_token` in a `federation.ini` that only its owner can read (`chmod 600`), and give `heimdall-ctl` the token through `HEIMDALL_AUTH_TOKEN`. The instance INI also holds the token for the registrar, so protect `daq_chain_config.ini` the same way. The `--auth-token` example above is for illustration only.

The token travels in clear text. It stops accidental or casual access on a trusted LAN. It does not protect against anyone who can read the traffic. Use WireGuard (section 6) across untrusted links.

### 3.3 `allow_cidrs`

`[federation] allow_cidrs` is a comma list of IPv4 or IPv6 networks. Empty (the default) accepts every peer. When set, the coordinator (6000) and the router (7000 and 7001) close a connection from any other address at accept, **without a reply**. IPv4-mapped IPv6 peers are matched as IPv4.

Include loopback (`127.0.0.1/32`, and `::1/128` for an IPv6 listener) when local tools use the service. A local `heimdall-ctl` connects to `127.0.0.1` by default.

`allow_cidrs` in an instance INI has no effect on the instance's own ports. Only the coordinator and the router read it.

### 3.4 Validating a `federation.ini`

`python3 ini_checker.py federation.ini --federation` checks the coordinator and router keys. It warns when the listen address is not loopback and both access controls are empty. Real output:

```console
$ cat federation.ini
[federation]
cluster_id = site-a
cluster_members = hosta:0,hostb:0
coordinator_port = 6000
router_port = 7000
router_stats_port = 7001
$ python3 ini_checker.py federation.ini --federation
WARNING: auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not loopback: the coordinator accepts mutating commands from any host
```

The check reads `[daq] listen_address` of that file. It does not see a `--listen-address` flag. A malformed network is an error (exit 1):

```console
ERROR:root:[federation] allow_cidrs entry '10.20.0.0/33' is not an IPv4/IPv6 CIDR
```

### 3.5 Single-slot ports

Two legacy ports serve one client at a time by default. A resident DoA client therefore blocks every other client.

| Port | Key | Default | Opt-in value | Effect |
|---|---|---|---|---|
| 5001 + offset | `[daq] ctl_max_connections` | `1` (sequential accept, `listen(4)`) | e.g. `4` (1 to 64) | serves n connections at once, `listen(16)`; requests still run one at a time in the hw_controller main loop |
| 5000 + offset | `[data_interface] iq_server_max_clients` | `1` | 2 to 64 | sends every frame to each session; a session that misses `iq_server_frame_deadline_ms` (0 = 0.8 × frame period) is closed |

<!-- verify-after: WP-04 -->

`ini_checker` warns when a federation member keeps `ctl_max_connections = 1`: `a resident DoA client blocks every coordinator and heimdall-ctl command on this member (set [daq] ctl_max_connections = 4)`.

The router avoids holding the IQ slot when no one consumes its output. With `--connect-on-demand` (the CLI and INI default) it connects to a source's 5000 only while at least one output client is attached.

For idle and half-open control connections, `[daq] ctl_xack_idle_sec` (30) closes idle connections that sent `XACK`. `[daq] ctl_idle_timeout_sec` (> 0) closes any idle connection. Both enable TCP keepalive with 10 s idle, 5 s interval and 3 probes.

## 4. Who connects to what

| Port | Connected by |
|---|---|
| 1130 + offset | local `hw_controller.py` and `delay_sync.py` only (`tcp://localhost`) |
| 5000 + offset | DSP software (DoA, passive radar, GNU Radio), the IQ router |
| 5001 + offset | DSP software that sends control frames, `heimdall-ctl`, the coordinator |
| 5002 + offset | `heimdall-ctl`, monitoring, the coordinator (`STATUS`, `PEERS`, `EVENTS`, `DB_STATS`), peer instances in `health_mode = mesh` |
| 5003 + offset | event subscribers such as `heimdall-ctl events --tail` |
| 6000 | `heimdall-ctl fleet`, instances with `register_with_coordinator = 1`, the other `coordinator_candidates` |
| 7000 | IQ consumers, downstream routers (`router://host:7000` sources) |
| 7001 | `heimdall-ctl router`, the coordinator (`STATS`, to answer `ROUTER STATS`, via `router_stats_endpoint`), downstream routers (`--upstream-stats`) |

Typical reachability for a multi-host cluster:

| From \ To | 1130 | 5000 | 5001 | 5002 | 5003 | 6000 | 7000 | 7001 |
|---|---|---|---|---|---|---|---|---|
| same host | yes | yes | yes | yes | yes | yes | yes | yes |
| peer DAQ host | no | no | no | mesh health | no | register | no | no |
| coordinator host | no | no | yes | yes | no | election | no | yes |
| router host | no | yes | no | no | no | no | chaining | chaining |
| DSP host | no | direct mode | if it sends control | optional | optional | no | via router | no |
| operator workstation | no | no | yes | yes | yes | yes | optional | yes |

## 5. Firewall examples

### 5.1 nftables

This table allows HeIMDALL ports for instances 0 to 3 from trusted networks only. It drops 1130 from every non-loopback source. It uses `policy accept`, so it loads beside an existing ruleset and only drops HeIMDALL ports.

```nft
#!/usr/sbin/nft -f
# HeIMDALL DAQ: instances 0-3 (port_stride 100), coordinator 6000, router 7000/7001.
# Policy accept: this table only drops HeIMDALL ports, so it loads beside an
# existing ruleset. A drop here is final; an accept here does not override a
# drop in another table.
table inet heimdall
delete table inet heimdall

table inet heimdall {
    set trusted4 {
        type ipv4_addr
        flags interval
        # cluster hosts, operator workstations, DSP hosts
        elements = { 10.20.0.0/24, 192.168.10.0/24 }
    }

    set trusted6 {
        type ipv6_addr
        flags interval
        # IPv6 clients of a coordinator or router with an IPv6 listener
        elements = { fd00:20::/64 }
    }

    set zmq_ports {
        type inet_service
        # rtl_daq ZMQ control 1130 + N*100: loopback only, never remote
        elements = { 1130, 1230, 1330, 1430 }
    }

    set heimdall_ports {
        type inet_service
        flags interval
        elements = { 5000-5003, 5100-5103, 5200-5203, 5300-5303, 6000, 7000, 7001 }
    }

    chain input {
        type filter hook input priority filter; policy accept;
        iif "lo" accept
        tcp dport @zmq_ports drop
        ip saddr @trusted4 tcp dport @heimdall_ports accept
        ip6 saddr @trusted6 tcp dport @heimdall_ports accept
        tcp dport @heimdall_ports drop
    }
}
```

The legacy ports 5000 to 5003 listen on IPv4 only. The `trusted6` set matters only for a coordinator or router bound to an IPv6 address (section 2.1). Without a matching `trusted6` entry, IPv6 clients of 6000, 7000 and 7001 are dropped with no reply.

Check and load it:

```console
$ sudo nft -c -f /etc/nftables.d/heimdall.nft   # syntax check, no change
$ sudo nft -f /etc/nftables.d/heimdall.nft
$ sudo nft list table inet heimdall
```

**`nft -f` changes only the running kernel. The table is gone after a reboot or `systemctl restart nftables`, and ports 1130 and 5000 to 5003 are open again.** Make it persistent. Add this line at the end of `/etc/nftables.conf`, after its `flush ruleset`:

```nft
include "/etc/nftables.d/*.nft"
```

Then enable the service and verify after the next reboot:

```console
$ sudo systemctl enable --now nftables
$ sudo nft list table inet heimdall
```

The table file uses `delete table`, so loading it twice is safe. The file above passes `nft -c -f` with nftables 1.0.6. If the host already has a default-deny input policy in another table, add the accepts to that table instead. An accept in this table does not override a drop elsewhere.

Narrow `heimdall_ports` per host. A DAQ host that runs neither the coordinator nor the router does not need 6000, 7000 or 7001. A host without `eth` output does not need 5000.

### 5.2 ufw

ufw denies incoming traffic by default and always allows loopback. Allow only what remote clients need. Port 1130 then stays closed without a rule.

**Allow SSH before `ufw enable`.** Default-deny drops every inbound connection that no rule matches, including the SSH session you are typing in. On a headless host you lose access until someone reaches the console.

```console
$ sudo ufw default deny incoming
$ sudo ufw allow from 192.168.10.0/24 to any port 22 proto tcp   # admin network: SSH
$ sudo ufw allow from 10.20.0.0/24 to any port 5000:5003,5100:5103,5200:5203,5300:5303 proto tcp
$ sudo ufw allow from 10.20.0.0/24 to any port 6000,7000,7001 proto tcp
$ sudo ufw enable
```

Also allow every other local service the host offers before you enable ufw, for example the web UI of DSP software on the same host. A time server host also needs NTP (section 8.4).

The HeIMDALL rules pass `ufw --dry-run` with ufw 0.36.2.

## 6. WireGuard for multi-site clusters

Ports 1130 and 5000 to 5003 cannot gain authentication or encryption without breaking existing clients. Run cluster traffic between sites through a WireGuard overlay instead. Expose the HeIMDALL ports only on the tunnel interface.

### 6.1 Tunnel configuration

Generate a key pair on each host:

```console
$ sudo sh -c 'umask 077; wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey'
```

Put the content of `privatekey` in the host's own `[Interface]`. Copy `publickey` into the `[Peer]` section of the other host.

One `/etc/wireguard/wg0.conf` per host. This is host A of a two-site cluster:

```ini
[Interface]
Address = 10.20.0.11/24
ListenPort = 51820
PrivateKey = <host A private key>

[Peer]
# host B
PublicKey = <host B public key>
AllowedIPs = 10.20.0.12/32
Endpoint = hostb.example.net:51820
PersistentKeepalive = 25
```

Host B mirrors it: `Address = 10.20.0.12/24`, its own private key, host A's public key, `AllowedIPs = 10.20.0.11/32` and `Endpoint = hosta.example.net:51820`.

Bring the tunnel up and keep it up across reboots. `wg-quick up` alone does not survive a reboot.

```console
$ sudo systemctl enable --now wg-quick@wg0
$ sudo wg show
```

`wg show` lists the peer with a recent `latest handshake` once both hosts are up.

### 6.2 HeIMDALL settings over the tunnel

| Setting | Value | Why |
|---|---|---|
| `[daq] listen_address` | `0.0.0.0` | loopback clients keep working (section 2.2); the firewall limits remote access to `wg0` |
| `[daq] zmq_listen_address` | `127.0.0.1` | 1130 never leaves the host |
| `[federation] cluster_members`, router `--sources` | `host:instance_id` with tunnel names or addresses, e.g. `10.20.0.11:0,10.20.0.12:0` | members connect over the tunnel |
| `[federation] coordinator_candidates` | `host:port` with tunnel names or addresses, e.g. `10.20.0.11:6000,10.20.0.12:6000` | candidates elect over the tunnel; a port of 0 is refused (`expected host:port`) |
| `[federation] advertise_host` | the host's tunnel name | the registrar and self entries publish a reachable host |
| `[federation] allow_cidrs` | `10.20.0.0/24,127.0.0.1/32` | the coordinator and router refuse non-tunnel peers |
| `[federation] auth_token` | one shared token | defence in depth on 6000 and 7001 |

Node keys use the host part exactly as written in a membership entry, lower-cased and never DNS-resolved. Write the same host form everywhere a member appears.

### 6.3 Firewall on the tunnel interface

```nft
#!/usr/sbin/nft -f
# HeIMDALL ports reachable only through the WireGuard interface wg0.
table inet heimdall
delete table inet heimdall

table inet heimdall {
    set zmq_ports {
        type inet_service
        elements = { 1130, 1230, 1330, 1430 }
    }

    set heimdall_ports {
        type inet_service
        flags interval
        elements = { 5000-5003, 5100-5103, 5200-5203, 5300-5303, 6000, 7000, 7001 }
    }

    chain input {
        type filter hook input priority filter; policy accept;
        iif "lo" accept
        tcp dport @zmq_ports drop
        iifname "wg0" tcp dport @heimdall_ports accept
        tcp dport @heimdall_ports drop
    }
}
```

The ufw equivalent. It assumes the default-deny state of section 5.2, so allow SSH first, before `ufw enable`:

```console
$ sudo ufw allow from 192.168.10.0/24 to any port 22 proto tcp   # admin network: SSH
$ sudo ufw allow 51820/udp
$ sudo ufw allow in on wg0 to any port 5000:5003,5100:5103,5200:5203,5300:5303 proto tcp
$ sudo ufw allow in on wg0 to any port 6000,7000,7001 proto tcp
```

Encryption costs CPU on hosts that also run the DSP chain. Measure the tunnel with `iperf3` before you route IQ streams through it (section 7).

## 7. Bandwidth per instance

Port 5000 carries complex float32 samples: 8 bytes per sample per channel, plus a 1024-byte header per frame. The USB side carries 2 bytes per sample, so the network stream is 4 times the USB rate before decimation.

| Quantity | Formula |
|---|---|
| frame payload | `cpi_size × num_ch × 8` bytes |
| frame period `T_fp` | `cpi_size × decimation_ratio / sample_rate` |
| IQ stream rate | `num_ch × sample_rate × 8 / decimation_ratio` bytes/s |

Worked rows (`cpi_size = 1048576`):

| `num_ch` | `sample_rate` | `decimation_ratio` | Frame | `T_fp` | Rate |
|---|---|---|---|---|---|
| 5 | 2.4 MS/s | 1 (shipped) | 40 MiB | 0.437 s | 96 MB/s = 768 Mbit/s |
| 5 | 2.4 MS/s | 2 | 40 MiB | 0.874 s | 48 MB/s = 384 Mbit/s |
| 5 | 2.4 MS/s | 4 | 40 MiB | 1.748 s | 24 MB/s = 192 Mbit/s |
| 4 | 2.4 MS/s | 1 | 32 MiB | 0.437 s | 76.8 MB/s = 614 Mbit/s |

One shipped-rate instance fills most of a gigabit link. Two of them do not fit on one gigabit link to one DSP host.

Multiply for fan-out:

| Path | Rate |
|---|---|
| iq_server with n sessions (`iq_server_max_clients`) | n × instance rate |
| router input | sum of its sources' rates |
| router output | number of output clients × sum of source rates |
| downstream router reading `router://` | the upstream router's full output |

Ports 5001, 5002, 5003, 6000 and 7001 carry small messages. Their traffic is negligible next to the IQ streams.

`iq_server` is stop-and-wait: it sends the next frame only after the client's `IQDownload` acknowledgement. A link that cannot sustain the stream rate slows the whole chain. Raise `decimation_ratio`, reduce `num_ch` or `sample_rate`, or run the DSP on the DAQ host with `out_data_iface_type = shmem`. See [capacity_planning.md](capacity_planning.md).

## 8. Time synchronisation

### 8.1 The rule

No process needs synchronised clocks to run. The election, health, staleness and age computations use each observer's own monotonic clock. You need disciplined clocks (chrony/NTP, optionally PTP) on every host **only** when you:

* enable router alignment (`--align-window-ms` > 0), or
* compare `time_stamp` values, event timestamps or database records across hosts.

Both cases are preconditions, not features: the code does not check clock discipline. The router's `skew_warning` is the only built-in hint.

### 8.2 What each timestamp means

| Value | Clock | Unit | Set by |
|---|---|---|---|
| IQ header `time_stamp` | acquiring host wall clock (`gettimeofday`) | ms since the Unix epoch | `rtl_daq` when it completes the header after USB completion; `rebuffer` back-dates it by the samples still waiting in its buffer |
| IQ header `cpi_index` | none (counter) | frames | per instance; use it, not `time_stamp`, to detect lost frames |
| `:5002` `STATUS`/`PING` `timestamp`, `ts` | status host wall clock (`time.time()`) | seconds, float | status server |
| event `timestamp` | emitting host wall clock | seconds, float | `EventBus` |
| database records (`timestamp_ms`) | recording host wall clock | ms, integer | `daq_db` |
| coordinator `EVENTS` `rx_ts` and the `age:<sec>` filter | coordinator wall clock | seconds | coordinator, at receipt |
| router `last_frame_age_sec`, health `seen_age_sec` | observer monotonic clock | seconds | the observer |

`time_stamp` marks the arrival of the frame's last sample plus USB and scheduling latency. The first sample is about `T_fp` earlier (437 ms at the defaults). Its resolution is 1 ms, which is 2400 samples at 2.4 MS/s. NTP stepping the clock moves it. See [theory_coherent_acquisition.md](theory_coherent_acquisition.md).

The election uses `time.monotonic()`. `election_clock_margin` (default 0.05) bounds the difference in clock **rate** between candidates, not their wall-clock offset.

### 8.3 Router alignment and clock offset

<!-- verify-after: WP-09 -->

The router measures, per source, `clock_offset_ms` = the median over the last 64 frames of `rx_wall_ms − header.time_stamp`. `rx_wall_ms` is the router's wall clock when it receives the frame. The value therefore contains the source's clock offset **plus** the pipeline and transfer latency, so it is normally large and positive. Only the differences between sources matter.

The router compares each source's `clock_offset_ms` with the median over all sources. When the difference exceeds `--max-skew-ms` (`router_max_skew_ms`, default 500, 0 = off), the source shows `skew_warning: true` in `STATS` and `SOURCES`, and the router logs a warning at most once per minute per source.

With `--align-window-ms W` > 0, the router releases frames in `time_stamp` order. A frame is released when every streaming source has a frame with an equal or later `time_stamp`, when it has waited `W` ms, or when its per-source FIFO (`--align-max-frames`, default 2) overflows. Alignment groups frames by wall-clock time. It does not align samples and does not make independently clocked units coherent.

What accuracy alignment needs:

| Requirement | Bound at the defaults |
|---|---|
| Frames from different hosts for the same instant must sort into the same group | total time error well below `T_fp / 2` = 218 ms |
| No `skew_warning` | offset differences below 500 ms |
| No benefit beyond | 1 ms, the `time_stamp` resolution |

The total time error is the clock difference between hosts plus the difference in their USB and scheduling latency. Keep host clocks within a few milliseconds of each other. chrony on a LAN with a local server usually achieves this. PTP is not required by any code path.

### 8.4 chrony

Point every DAQ, coordinator and router host at the same servers. A local server on the cluster network keeps relative offsets small when the internet is unreachable.

Install chrony on every host. Raspberry Pi OS and Debian ship systemd-timesyncd. Installing chrony replaces it:

```console
$ sudo apt install chrony
```

Configure each cluster host:

```conf
# /etc/chrony/chrony.conf on each cluster host
server ntp.site-a.example iburst
makestep 1.0 3
rtcsync
```

`makestep 1.0 3` steps the clock only during the first three updates. Afterwards chrony slews. `time_stamp` therefore does not jump during operation, **provided the chain starts after the first sync**.

A Raspberry Pi 4 has no RTC. It boots with a stale clock, and the first step can be hours or days. The shipped `heimdall-daq.service` and `heimdall-daq@.service` order only `After=network.target`, so the chain can start before chrony steps the clock. Make the units wait for time sync:

```console
$ sudo systemctl enable chrony-wait.service
$ sudo systemctl edit heimdall-daq.service
```

```ini
# drop-in for heimdall-daq.service (repeat for heimdall-daq@.service
# and any coordinator or router unit you add)
[Unit]
Wants=time-sync.target
After=time-sync.target
```

For a manual start, wait for sync first. `chronyc waitsync 30 0.01` waits up to 30 tries for a correction below 10 ms:

```console
$ chronyc waitsync 30 0.01 && sudo ./daq_start_sm.sh
```

On the host that serves time to the cluster:

```conf
# /etc/chrony/chrony.conf on the site time server
pool 2.pool.ntp.org iburst
allow 10.20.0.0/24
local stratum 10
```

Open NTP (UDP 123) to the cluster network on the time server. A default-deny firewall (section 5.2) otherwise blocks it, and the client clocks drift apart with no error:

```console
$ sudo ufw allow from 10.20.0.0/24 to any port 123 proto udp
```

With nftables and a default-deny table, add `ip saddr 10.20.0.0/24 udp dport 123 accept` to that table. On each client, `chronyc sources` must mark the site server `^*` (selected) or `^+` (combined).

Check the offset on each host:

```console
$ chronyc tracking
$ chronyc sources -v
```

The `System time` line of `chronyc tracking` gives the current offset. Compare it across hosts before you trust cross-host timestamps.

### 8.5 PTP

PTP (linuxptp) helps only when you correlate HeIMDALL timestamps with other PTP-timed equipment.

**Only one daemon may steer the system clock.** chrony (section 8.4) and `phc2sys` both set `CLOCK_REALTIME` and fight if both run. Choose one setup per host:

| Setup | Run | System clock set by |
|---|---|---|
| PTP only | stop and disable chrony; run `ptp4l` and `phc2sys` as services (linuxptp `ptp4l@eth0` and `phc2sys@eth0` units where your distribution ships them) | `phc2sys` |
| chrony with a PTP reference | keep chrony; run `ptp4l` only; add `refclock PHC /dev/ptp0 poll 0 dpoll -2 offset 0` to `chrony.conf` | chrony |

To try PTP in the foreground with a NIC that has hardware timestamping (PTP-only setup, chrony stopped):

```console
$ sudo ptp4l -i eth0 -m
$ sudo phc2sys -s eth0 -w -m
```

`-m` prints to the terminal. The commands stop when the shell closes. Use the service units for operation.

Without hardware timestamping, `ptp4l -i eth0 -S` uses software timestamps. A software-timestamping NIC has no PHC, so the chrony refclock setup needs hardware timestamping. Neither changes the 1 ms resolution of `time_stamp`. Neither makes separate KrakenSDR units coherent.

## 9. Troubleshooting connectivity

| Symptom | Likely cause | Check or fix |
|---|---|---|
| A client cannot connect to 5000 | `out_data_iface_type = shmem` (shipped) | set `eth` and restart; check `ss -ltn` |
| A client cannot connect to 5002 or 5003 | monitoring disabled (shipped) | set `en_monitoring = 1` and `en_status_server = 1` / `en_zmq_pub = 1` in `[monitoring]`, then restart the chain |
| `heimdall-ctl` fails on a host with a non-loopback `listen_address` | it connects to `127.0.0.1` | pass `--host <listen_address>` |
| Connection to 6000, 7000 or 7001 closes with no reply | the client address is outside `allow_cidrs` | add the network, including `127.0.0.1/32` for local tools |
| `{"ok": false, "error": "auth_required"}` | missing or wrong token | `--auth-token`, `HEIMDALL_AUTH_TOKEN` or `[federation] auth_token` |
| `peer_down` event on a member (`heimdall-ctl events`) | the peer's 5002 + offset is blocked by a firewall, unreachable, or monitoring is off on the peer | `heimdall-ctl --host <peer> --instance N status` from this host; check that this host is in the peer's trusted set (section 5) |
| Coordinator replies `{"error": "busy", "retry_in_sec": 1}` | all handler threads are in use (`max_handlers`, default 64), or another mutating command holds the fan-out lock | retry after `retry_in_sec`; long `busy` streaks point to slow or unreachable members |
| Coordinator exits with code 2 | port 6000 already bound | another coordinator, or an instance with id 10 on stride 100. A bad flag exits 3, not 2 |
| Router exits with code 2 | 7000 or 7001 already bound | another router, or an instance with id 20 on stride 100 |
| Router source stays in `waiting_for_slot` | iq_server's single slot is held by another client | raise `iq_server_max_clients`, or let consumers read the router's 7000 |
| `skew_warning: true` on one source | clock or latency differs from the other sources | `chronyc tracking` on that host; compare link load |
| `rtl_daq` log: `ZMQ bind to ... failed ([daq] zmq_listen_address)` | bad or unavailable bind address | use one of `*`, `0.0.0.0`, `127.0.0.1`, `localhost` |
| Start script: `Ports (...) still busy after 10 attempts` | a listener holds 5000, 5001 or the status port + offset | the error lists the listeners; find the holder with `ss -ltnp 'sport = :<port>'` |

<!-- verify-after: WP-12 -->

**The start script's busy-port loop can stop every instance on the host.** The script probes for listeners only (`ss -ltn`, else `lsof -sTCP:LISTEN`), so outbound client sockets no longer count as busy. When a port is busy, each of the 10 retries runs `./daq_stop.sh`. By default that is the no-argument form, which stops **all** DAQ instances on the host. It runs `./daq_stop.sh <id>` (this instance only) when `[host] instance_guard = refuse` is set (it needs `[host] instance_registry = 1`; see [configuration.md](configuration.md)) or the environment has `HEIMDALL_SCOPED_STOP=1`.

On a multi-instance host, set one of those two before you start an instance. Before you retry a failed start, find the holder:

```console
$ ss -ltnp 'sport = :5101'        # listener on the port: the real conflict
$ lsof -nP -iTCP:5101             # every socket that uses the port, including outbound clients
```

List the HeIMDALL listeners on a host:

```console
$ ss -ltnp | grep -E ':(1[1-4]30|5[0-3]0[0-3]|6000|700[01])\b'   # instance ids 0-3
```

Compare the result with `heimdall-ctl ports` for each instance.
