#!/usr/bin/env python3
"""
    Generate the configuration of a HeIMDALL DAQ cluster (design spec H.7).

        python3 util/cluster_cfg_gen.py --template Firmware/daq_chain_config.ini \\
            --hosts hosta:2 hostb:1 --coordinators hosta:6000,hostb:6000,hostc:6000 \\
            --register --out cluster/

    Writes, under --out:
      <host>/inst<N>/daq_chain_config.ini   one per instance (ids 0..k-1 per host)
      federation.ini                        coordinator (+ central router) keys
      <host>/federation.ini                 per-host router chained into the central
                                            router (only with --router, hosts other
                                            than the router host)
      ports.md                              per-host port plan
      provision.sh                          provision_instance.py --from-config lines

    Every instance INI is validated with ini_checker.check_ini() and
    check_ini_warnings(), every federation.ini with check_federation_ini(),
    plus the generator's own per-host port plan and core plan. Exit codes:
    0 ok, 1 validation failed (the files stay for inspection), 2 usage.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import os
import re
import sys

_UTIL_DIR = os.path.dirname(os.path.realpath(__file__))
_FIRMWARE_DIR = os.path.join(os.path.dirname(_UTIL_DIR), "Firmware")
sys.path.insert(0, _UTIL_DIR)
sys.path.insert(0, _FIRMWARE_DIR)

import provision_instance as pi  # noqa: E402

EXIT_OK, EXIT_INVALID, EXIT_USAGE = 0, 1, 2
CORES_PER_INSTANCE = 4
DEFAULT_ROUTER_PORT = 7000
DEFAULT_ROUTER_STATS_PORT = 7001
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_LOOPBACK = ("localhost", "localhost.localdomain", "ip6-localhost")


class UsageError(Exception):
    pass


def _load_checker():
    """ini_checker (WP-11) plus federation_common, imported from this tree."""
    import ini_checker
    fc = getattr(ini_checker, "_fc", None)
    if fc is None:
        raise UsageError("Firmware/_daq_core/federation_common.py is not importable")
    return ini_checker, fc


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="cluster_cfg_gen.py",
        description="Generate instance INIs, federation.ini, ports.md and provision.sh for a cluster.",
        epilog="Exit codes: 0 ok, 1 validation failed (files stay for inspection), 2 usage.")
    p.add_argument("--template", required=True, help="base instance INI (e.g. Firmware/daq_chain_config.ini)")
    p.add_argument("--hosts", required=True, nargs="+", metavar="HOST:COUNT",
                   help="hosts and their instance counts, e.g. hosta:2 hostb:1")
    p.add_argument("--stride", type=int, default=100, help="[federation] port_stride (default 100)")
    p.add_argument("--serial-base", type=int, default=1000, help="serial base of unit 0 (default 1000)")
    p.add_argument("--serial-stride", type=int, default=100,
                   help="serial distance between units (default 100)")
    p.add_argument("--cores-per-host", metavar="HOST=N,...",
                   help="CPU cores per host; the plan needs 4 per instance")
    p.add_argument("--coordinators", metavar="H:P,...",
                   help="coordinator endpoints (>= 2: election candidates; 1: coordinator_host/port)")
    p.add_argument("--router", metavar="H:P", help="central IQ router (per-host routers chain into it)")
    p.add_argument("--cluster-id", default="default", help="[federation] cluster_id (default 'default')")
    p.add_argument("--register", action="store_true", help="register_with_coordinator = 1")
    p.add_argument("--health-mode", choices=("mesh", "hub"), default="mesh",
                   help="[federation] health_mode (hub needs --register)")
    p.add_argument("--auth-token-file", metavar="PATH", help="file holding the [federation] auth_token")
    p.add_argument("--out", required=True, metavar="DIR", help="output directory")
    return p.parse_args(argv)


def parse_hosts(items, fc):
    hosts = []
    seen = set()
    for item in items:
        for part in [x.strip() for x in item.split(",") if x.strip()]:
            host, sep, count = part.rpartition(":")
            n = pi.strict_int(count) if sep else None
            if not host or n is None or n < 1:
                raise UsageError("bad --hosts entry '{0}' (expected host:count with count >= 1)".format(part))
            host = host.lower()
            if not _HOST_RE.match(host):
                raise UsageError("bad host name '{0}' in --hosts".format(host))
            if host in _LOOPBACK or fc.is_loopback(host):
                raise UsageError("--hosts entry '{0}' uses a loopback host; use the host's "
                                 "advertised name".format(part))
            if host in seen:
                raise UsageError("host '{0}' is listed twice in --hosts".format(host))
            seen.add(host)
            hosts.append((host, n))
    if not hosts:
        raise UsageError("--hosts lists no host")
    return hosts


def parse_cores(text, hosts):
    out = {}
    if not text:
        return out
    names = set(h for h, _ in hosts)
    for part in [x.strip() for x in text.split(",") if x.strip()]:
        host, sep, n = part.partition("=")
        v = pi.strict_int(n) if sep else None
        if not host or v is None or v < 1:
            raise UsageError("bad --cores-per-host entry '{0}' (expected host=N)".format(part))
        if host.lower() not in names:
            raise UsageError("--cores-per-host names '{0}', which is not in --hosts".format(host))
        out[host.lower()] = v
    return out


def parse_endpoints(text, flag, fc):
    eps = []
    for part in [x.strip() for x in text.split(",") if x.strip()]:
        try:
            host, port = pi.parse_endpoint(part)
        except ValueError as e:
            raise UsageError("{0}: {1}".format(flag, e))
        host = host.lower()
        if fc.is_loopback(host):
            raise UsageError("{0} entry '{1}' uses a loopback host".format(flag, part))
        if (host, port) in eps:
            raise UsageError("{0} lists '{1}' twice".format(flag, part))
        eps.append((host, port))
    return eps


def node_id_for(host, iid, fc):
    nid = "{0}:{1}".format(host, iid)
    if fc.is_valid_node_id(nid):
        return nid
    suffix = ":{0}".format(iid)
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", host)
    return (base[:48 - len(suffix)] or "host") + suffix


class Cluster:
    """The plan: instances, services and their ports."""

    def __init__(self, args, fc):
        self.args = args
        self.fc = fc
        self.hosts = parse_hosts(args.hosts, fc)
        if args.stride < 1:
            raise UsageError("--stride must be >= 1")
        if args.serial_base < 0 or args.serial_stride < 1:
            raise UsageError("--serial-base must be >= 0 and --serial-stride >= 1")
        if not fc.is_valid_cluster_id(args.cluster_id):
            raise UsageError("--cluster-id '{0}' does not match the cluster_id grammar "
                             "(1-31 of A-Z a-z 0-9 _ . : -, starting alphanumeric)".format(args.cluster_id))
        if args.health_mode == "hub" and not args.register:
            raise UsageError("--health-mode hub requires --register (the peer table comes from the "
                             "REGISTER reply)")
        self.cores = parse_cores(args.cores_per_host, self.hosts)
        self.coordinators = parse_endpoints(args.coordinators, "--coordinators", fc) if args.coordinators else []
        if len(set(p for _, p in self.coordinators)) > 1:
            raise UsageError("--coordinators must use one port (every candidate reads the same "
                             "federation.ini coordinator_port)")
        if args.register and not self.coordinators:
            raise UsageError("--register requires --coordinators (the endpoint to register with)")
        self.router = None
        if args.router:
            eps = parse_endpoints(args.router, "--router", fc)
            if len(eps) != 1:
                raise UsageError("--router takes exactly one host:port")
            self.router = eps[0]
            self.router_stats = DEFAULT_ROUTER_STATS_PORT if self.router[1] == DEFAULT_ROUTER_PORT \
                else self.router[1] + 1
            if self.router_stats > 65535:
                raise UsageError("--router port leaves no room for the stats port (port + 1)")
        self.token = None
        if args.auth_token_file:
            try:
                with open(args.auth_token_file, "r") as f:
                    self.token = f.read().strip()
            except (OSError, UnicodeDecodeError) as e:
                raise UsageError("cannot read --auth-token-file: {0}".format(e))
            if not fc.is_valid_auth_token(self.token):
                raise UsageError("the token in {0} must be printable ASCII without spaces, at most "
                                 "{1} characters".format(args.auth_token_file, fc.AUTH_TOKEN_MAX_LEN))
        try:
            with open(args.template, "r") as f:
                self.template_text = f.read()
            self.template = pi.params_of_text(self.template_text)
        except (OSError, UnicodeDecodeError) as e:
            raise UsageError("cannot read --template: {0}".format(e))
        except Exception as e:  # configparser.Error
            raise UsageError("cannot parse --template: {0}".format(e))
        self.num_ch = pi.strict_int(self.template.get("hw", {}).get("num_ch", "")) or 0
        if self.num_ch < 1:
            raise UsageError("--template has no valid [hw] num_ch")
        if args.serial_stride < self.num_ch:
            raise UsageError("--serial-stride {0} is below num_ch {1}: units would share serials".format(
                args.serial_stride, self.num_ch))
        self.instances = []  # [(host, iid, unit)]
        unit = 0
        for host, count in self.hosts:
            for iid in range(count):
                self.instances.append((host, iid, unit))
                unit += 1
        last = args.serial_base + args.serial_stride * (unit - 1) + self.num_ch - 1
        if last > 99999999:
            raise UsageError("serials of the last unit exceed the 8-digit EEPROM limit (up to {0})".format(last))
        self.members = ",".join(fc.node_key(h, i) for h, i, _ in self.instances)
        self.warnings = []  # template values the generator dropped (filled by instance_ini)

    # -- files -------------------------------------------------------------

    def instance_rel(self, host, iid):
        return os.path.join(host, "inst{0}".format(iid), pi.INI_NAME)

    def instance_ini(self, host, iid, unit):
        a = self.args
        doc = pi.IniDocument(self.template_text)
        ctr_index = pi.control_channel_index(self.template)
        fed = [("instance_id", str(iid)), ("port_stride", str(a.stride)), ("en_federation", "1"),
               ("cluster_members", self.members), ("node_id", node_id_for(host, iid, self.fc)),
               ("cluster_id", a.cluster_id), ("advertise_host", host), ("health_mode", a.health_mode)]
        tfed = self.template.get("federation", {})
        candidates = [pi.format_endpoint(h, p) for h, p in self.coordinators] \
            if len(self.coordinators) >= 2 else []
        if candidates:
            fed.append(("coordinator_candidates", ",".join(candidates)))
        elif self.coordinators:
            fed += [("coordinator_host", self.coordinators[0][0]),
                    ("coordinator_port", str(self.coordinators[0][1]))]
        # The plan alone names the coordinators: a template's other coordinator form would
        # win (delay_sync prefers coordinator_candidates over coordinator_host) or point at
        # coordinators that DIR/federation.ini does not run.
        if not candidates and tfed.get("coordinator_candidates", "").strip():
            fed.append(("coordinator_candidates", ""))
        if (candidates or not self.coordinators) and tfed.get("coordinator_host", "").strip():
            fed.append(("coordinator_host", ""))
        cself = tfed.get("coordinator_self", "").strip()
        if cself and cself not in candidates:
            fed.append(("coordinator_self", ""))
        if a.register:
            fed.append(("register_with_coordinator", "1"))
        elif "register_with_coordinator" in doc.keys("federation"):
            fed.append(("register_with_coordinator", "0"))
        if self.router:
            fed.append(("stamp_node_id", "1"))
        if self.token:
            fed.append(("auth_token", self.token))
        if "peer_list" in doc.keys("federation"):
            fed.append(("peer_list", ""))  # C.2: a template peer_list would override cluster_members
        for key, value in fed:
            doc.set("federation", key, value)
        serial_base = a.serial_base + a.serial_stride * unit
        doc.set("hw", "serial_base", str(serial_base))
        doc.set("hw", "unit_id", "auto")
        if "device_serials" in doc.keys("hw"):
            doc.set("hw", "device_serials", "")  # the template's explicit list would win over serial_base
        doc.set("daq", "ctr_channel_serial_no", str(serial_base + min(ctr_index, self.num_ch - 1)))
        doc.set("daq", "ctl_max_connections", "4")
        doc.set("host", "instance_registry", "1")
        doc.set("host", "instance_guard", "refuse")
        cores = [CORES_PER_INSTANCE * iid + k for k in range(CORES_PER_INSTANCE)]
        doc.set("host", "cpu_cores", ",".join(str(c) for c in cores))
        dsc = self.template.get("host", {}).get("delay_sync_cores", "").strip()
        if dsc and not pi.cores_within(dsc, cores):
            # an inherited pin would run this instance's delay_sync on another instance's cores
            doc.set("host", "delay_sync_cores", "")
            self.warnings.append("{0}: template [host] delay_sync_cores = {1} dropped: outside this "
                                 "instance's cpu_cores {2}".format(self.instance_rel(host, iid), dsc,
                                                                   ",".join(str(c) for c in cores)))
        for key, value in (("en_monitoring", "1"), ("en_status_server", "1"),
                           ("status_extended", "1"), ("status_update_interval", "10")):
            doc.set("monitoring", key, value)
        return doc.text()

    def _common_federation(self, header):
        a = self.args
        lines = [header, "[federation]",
                 "cluster_id = {0}".format(a.cluster_id),
                 "port_stride = {0}".format(a.stride),
                 "; every instance of the cluster (host:instance_id), incl. the coordinator hosts' own",
                 "cluster_members = {0}".format(self.members)]
        if self.coordinators:
            lines.append("coordinator_port = {0}".format(self.coordinators[0][1]))
            if len(self.coordinators) >= 2:
                lines.append("; election candidates; each coordinator finds itself by host + listen port")
                lines.append("coordinator_candidates = {0}".format(
                    ",".join(pi.format_endpoint(h, p) for h, p in self.coordinators)))
            else:
                lines.append("coordinator_host = {0}".format(self.coordinators[0][0]))
        if self.token:
            lines.append("auth_token = {0}".format(self.token))
        return lines

    def central_hosts(self):
        """Hosts that install DIR/federation.ini: every coordinator host without --router;
        with it the router host plus the coordinator hosts not in the plan (every planned
        host other than the router host gets DIR/<host>/federation.ini instead)."""
        coordinator_hosts = []
        for h, _ in self.coordinators:
            if h not in coordinator_hosts:
                coordinator_hosts.append(h)
        if not self.router:
            return coordinator_hosts
        planned = set(h for h, _ in self.hosts)
        return [self.router[0]] + [h for h in coordinator_hosts if h != self.router[0] and h not in planned]

    def central_federation(self):
        if self.router:
            header = ("; Generated by util/cluster_cfg_gen.py: coordinator and central IQ router "
                      "configuration of cluster '{0}'.\n"
                      "; Install as /etc/heimdall/federation.ini on {1} only. Every other host of the "
                      "plan has its own\n; <host>/federation.ini (per-host router + the same "
                      "coordinator keys) and uses that file instead.").format(
                          self.args.cluster_id, ", ".join(self.central_hosts()))
        else:
            header = ("; Generated by util/cluster_cfg_gen.py: coordinator configuration of cluster '{0}'.\n"
                      "; Install as /etc/heimdall/federation.ini on every coordinator host{1}.").format(
                          self.args.cluster_id,
                          " (" + ", ".join(self.central_hosts()) + ")" if self.coordinators else "")
        lines = self._common_federation(header)
        if self.router:
            host, port = self.router
            sources = ["{0}:{1}".format(h, i) for h, i, _ in self.instances if h == host]
            sources += ["router://{0}:{1}".format(h, DEFAULT_ROUTER_PORT)
                        for h, _ in self.hosts if h != host]
            lines += ["; central IQ router on {0}: local instances direct, other hosts through their "
                      "per-host routers".format(host),
                      "router_port = {0}".format(port),
                      "router_stats_port = {0}".format(self.router_stats),
                      "router_tag_mode = node",
                      "router_include_self = 0",
                      "router_sources = {0}".format(",".join(sources))]
        return "\n".join(lines) + "\n"

    def host_router_federation(self, host):
        lines = self._common_federation(
            "; Generated by util/cluster_cfg_gen.py: per-host IQ router of {0}, chained into the\n"
            "; central router on {1}. Install as /etc/heimdall/federation.ini on {0}.".format(
                host, self.router[0]))
        sources = ["{0}:{1}".format(h, i) for h, i, _ in self.instances if h == host]
        lines += ["router_port = {0}".format(DEFAULT_ROUTER_PORT),
                  "router_stats_port = {0}".format(DEFAULT_ROUTER_STATS_PORT),
                  "router_tag_mode = node",
                  "router_include_self = 0",
                  "router_sources = {0}".format(",".join(sources))]
        return "\n".join(lines) + "\n"

    def router_hosts(self):
        """{host: (router_port, stats_port)} of every router the plan runs."""
        if not self.router:
            return {}
        out = {h: (DEFAULT_ROUTER_PORT, DEFAULT_ROUTER_STATS_PORT) for h, _ in self.hosts if h != self.router[0]}
        out[self.router[0]] = (self.router[1], self.router_stats)
        return out

    def services(self, host):
        svc = []
        if host in [h for h, _ in self.coordinators]:
            svc.append(("coordinator", self.coordinators[0][1]))
        r = self.router_hosts().get(host)
        if r:
            svc += [("router", r[0]), ("router stats", r[1])]
        return svc

    def instance_ports(self, host, iid, unit):
        params = pi.params_of_text(self.instance_ini(host, iid, unit))
        return pi.instance_ports(params, iid, self.args.stride)

    def ports_md(self):
        a = self.args
        out = ["# Port plan: cluster `{0}`".format(a.cluster_id), "",
               "Generated by `util/cluster_cfg_gen.py`. Instance N listens on base + N x {0} "
               "(port_stride). With stride {0}, instance ids 10 (6000-6003) and 20 (7000-7003) "
               "collide with the default coordinator and router ports: keep ids 0-9 per host "
               "or move the service ports.".format(a.stride), "",
               "| host | instance | node key | serials | zmq | iq | ctl | status | events |",
               "|---|---|---|---|---|---|---|---|---|"]
        for host, iid, unit in self.instances:
            ports = {n: (p, e) for n, p, e in self.instance_ports(host, iid, unit)}
            base = a.serial_base + a.serial_stride * unit
            cells = []
            for name in ("zmq", "iq", "ctl", "status", "events"):
                p, e = ports[name]
                cells.append(str(p) if e else "{0} (off)".format(p))
            out.append("| {0} | {1} | {2} | {3}-{4} | {5} |".format(
                host, iid, self.fc.node_key(host, iid), base, base + self.num_ch - 1, " | ".join(cells)))
        svc_rows = [(h, name, port) for h in sorted(set([h for h, _ in self.hosts] +
                                                        [h for h, _ in self.coordinators] +
                                                        list(self.router_hosts())))
                    for name, port in self.services(h)]
        if svc_rows:
            out += ["", "| host | service | port |", "|---|---|---|"]
            out += ["| {0} | {1} | {2} |".format(h, n, p) for h, n, p in svc_rows]
        return "\n".join(out) + "\n"

    def provision_sh(self):
        planned = " ".join(h for h, _ in self.hosts)
        lines = ["#!/bin/sh",
                 "# Generated by util/cluster_cfg_gen.py: provisions the instances of one host from this",
                 "# directory with util/provision_instance.py --from-config. Copy the directory to each",
                 "# host and run there:  sudo sh provision.sh [HOST]   (default: the short host name)",
                 "# HEIMDALL_ROOT (default /opt/heimdall) and PYTHON (default python3) override paths.",
                 "set -e",
                 'HERE=$(cd "$(dirname "$0")" && pwd)',
                 'ROOT=${HEIMDALL_ROOT:-/opt/heimdall}',
                 'PYTHON=${PYTHON:-python3}',
                 "HOST=${1:-$(hostname -s | tr '[:upper:]' '[:lower:]')}",
                 'case "$HOST" in']
        for host, count in self.hosts:
            lines.append("{0})".format(host))
            for iid in range(count):
                lines.append('    "$PYTHON" "$ROOT/util/provision_instance.py" --root "$ROOT" --instance {0} '
                             '--from-config "$HERE/{1}"{2} --write'.format(
                                 iid, self.instance_rel(host, iid), " --in-place" if iid == 0 else ""))
            lines.append("    ;;")
        lines += ['*)',
                  '    echo "provision.sh: no instances planned for host \'$HOST\' (planned: {0})" >&2'.format(planned),
                  '    exit 2',
                  '    ;;',
                  'esac']
        return "\n".join(lines) + "\n"

    def write(self, out_dir):
        written = {}
        for host, iid, unit in self.instances:
            rel = self.instance_rel(host, iid)
            written[rel] = self.instance_ini(host, iid, unit)
        written["federation.ini"] = self.central_federation()
        if self.router:
            for host, _ in self.hosts:
                if host != self.router[0]:
                    written[os.path.join(host, "federation.ini")] = self.host_router_federation(host)
        written["ports.md"] = self.ports_md()
        written["provision.sh"] = self.provision_sh()
        for rel, text in written.items():
            path = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pi.write_atomic(path, text, 0o755 if rel == "provision.sh" else 0o644)
        return written

    # -- checks ------------------------------------------------------------

    def plan_errors(self):
        """The generator's own port and core plan checks: [(file, key, text)]."""
        errors = []
        a = self.args
        hosts = sorted(set([h for h, _ in self.hosts] + [h for h, _ in self.coordinators] +
                           list(self.router_hosts())))
        for host in hosts:
            owner = {}
            for name, port in self.services(host):
                if port in owner:
                    errors.append(("federation.ini", "router_port",
                                   "{0} port {1} on {2} equals the {3} port".format(name, port, host, owner[port][1])))
                owner[port] = ("federation.ini", name)
            for h, iid, unit in self.instances:
                if h != host:
                    continue
                rel = self.instance_rel(h, iid)
                for name, port, enabled in self.instance_ports(h, iid, unit):
                    if not enabled:
                        continue
                    if port > 65535:
                        errors.append((rel, "[federation] instance_id",
                                       "{0} port {1} of instance {2} is above 65535".format(name, port, iid)))
                    elif port in owner:
                        other = owner[port]
                        if other[0] == "federation.ini":
                            what = "the {0} port".format(other[1])
                            first = first_colliding_id(port, [p - iid * a.stride for _, p, _ in
                                                              self.instance_ports(h, iid, unit)], a.stride)
                            hint = ("; with stride {0} keep instance ids 0-{1} on {2} or move the {3} "
                                    "port".format(a.stride, first - 1, host, other[1]) if first else "")
                        else:
                            what, hint = "the {0} port of {1}".format(other[1], other[0]), ""
                        errors.append((rel, "[federation] instance_id",
                                       "{0} port {1} of instance {2} on {3} equals {4}{5}".format(
                                           name, port, iid, host, what, hint)))
                    else:
                        owner[port] = (rel, name)
        counts = dict(self.hosts)
        for host, ncores in sorted(self.cores.items()):
            need = CORES_PER_INSTANCE * counts.get(host, 0)
            if need > ncores:
                last = self.instance_rel(host, counts[host] - 1)
                errors.append((last, "[host] cpu_cores",
                               "{0} instances on {1} need {2} cores (4 per instance) but "
                               "--cores-per-host gives {3}".format(counts[host], host, need, ncores)))
        return errors


def first_colliding_id(port, bases, stride):
    """Smallest instance id whose port base + id*stride equals *port* (None if none)."""
    ids = [(port - b) // stride for b in bases if port >= b and (port - b) % stride == 0]
    return min(ids) if ids else None


def validate(cluster, out_dir, written, checker):
    errors, warnings = [], list(cluster.warnings)
    for rel in sorted(written):
        path = os.path.join(out_dir, rel)
        if rel.endswith(pi.INI_NAME):
            params = checker.read_config_file(path)
            if params is None:
                errors.append("{0}: cannot be read back".format(rel))
                continue
            try:
                errs = checker.check_ini(params, en_hw_check=False)
            except KeyError as e:
                errs = ["mandatory key {0} missing (from the template)".format(e)]
            errors += ["{0}: {1}".format(rel, e) for e in errs]
            warnings += ["{0}: {1}".format(rel, w) for w in checker.check_ini_warnings(params, en_hw_check=False)]
        elif rel.endswith("federation.ini"):
            params = checker.read_config_file(path)
            errs, warns = checker.check_federation_ini(params or {})
            errors += ["{0}: {1}".format(rel, e) for e in errs]
            warnings += ["{0}: {1}".format(rel, w) for w in warns]
    errors += ["{0}: {1}: {2}".format(f, k, t) for f, k, t in cluster.plan_errors()]
    if len(cluster.coordinators) == 2:
        warnings.append("federation.ini: 2 coordinator candidates: with 2 candidates, losing either host "
                        "blocks all mutations when require_quorum = 1; a quorum needs 3 candidates")
    return errors, warnings


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK
    try:
        checker, fc = _load_checker()
        cluster = Cluster(args, fc)
        out_dir = os.path.abspath(args.out)
        if os.path.exists(out_dir) and not os.path.isdir(out_dir):
            raise UsageError("--out {0} exists and is not a directory".format(out_dir))
        os.makedirs(out_dir, exist_ok=True)
        written = cluster.write(out_dir)
    except UsageError as e:
        print("ERROR: {0}".format(e), file=sys.stderr)
        return EXIT_USAGE
    except OSError as e:
        print("ERROR: {0}".format(e), file=sys.stderr)
        return EXIT_INVALID
    errors, warnings = validate(cluster, out_dir, written, checker)
    for w in warnings:
        print("WARNING: " + w, file=sys.stderr)
    for e in errors:
        print("ERROR: " + e, file=sys.stderr)
    for rel in sorted(written):
        print("written: {0}".format(os.path.join(out_dir, rel)))
    if errors:
        print("cluster_cfg_gen: {0} error(s); the files stay in {1} for inspection".format(
            len(errors), out_dir), file=sys.stderr)
        return EXIT_INVALID
    print("ok: {0} instance(s) on {1} host(s)".format(len(cluster.instances), len(cluster.hosts)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
