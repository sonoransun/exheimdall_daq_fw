"""
   Capacity model and pre-flight tool (spec I.1, I.2, I.6, I.7).

   * capacity_model formulas against the I.1 default column;
   * daq_preflight verdicts with --host-json fixtures: Pi 4 with two hardware
     instances (fail, core-count rule), Pi 5 with two (fail; warn when
     sharing is allowed), 8-core x86 with two controllers (ok), two units on
     one high-speed bus (fail), router memory bound, port collision with a
     coordinator port (fail);
   * --json output schema, exit codes 0/1/2/3, --host-json - (stdin),
     --dump-host-json round trip, live registry entries (--plus-live);
   * the probe helpers on fake sysfs/proc trees;
   * util/performance_monitor.py: /proc/<pid>/sched parsing, latency and
     throughput through a real streaming/IQDownload client against a fake
     iq_server, registry-based affinity expectations.

   Hardware-free and CI-safe: temp directories, ports from bind(0), no sudo.

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3
"""
import configparser
import contextlib
import glob
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.realpath(__file__))
FIRMWARE = os.path.dirname(os.path.dirname(_HERE))
REPO = os.path.dirname(FIRMWARE)
sys.path.insert(0, os.path.join(FIRMWARE, "_daq_core"))
sys.path.insert(0, FIRMWARE)

import capacity_model as cm  # noqa: E402
import daq_preflight  # noqa: E402

PREFLIGHT = os.path.join(FIRMWARE, "daq_preflight.py")
MIB = 1 << 20
GIB = 1 << 30

try:
    sys.path.insert(0, os.path.join(REPO, "util"))
    import performance_monitor as pm
    _PM_ERROR = None
except Exception as _e:  # noqa: BLE001 - any import problem skips those tests
    pm = None
    _PM_ERROR = _e


def ini_text(instance_id=0, num_ch=5, iface="eth", extra=""):
    return ("[hw]\nnum_ch = {n}\n"
            "[daq]\nsample_rate = 2400000\ndaq_buffer_size = 262144\n"
            "[pre_processing]\ncpi_size = 1048576\ndecimation_ratio = 1\n"
            "[calibration]\ncorr_size = 65536\n"
            "[data_interface]\nout_data_iface_type = {iface}\n"
            "[federation]\ninstance_id = {iid}\nport_stride = 100\n").format(
                n=num_ch, iface=iface, iid=instance_id) + extra


def params_of(text):
    p = configparser.ConfigParser(interpolation=None)
    p.read_string(text)
    return {s: dict(p.items(s)) for s in p.sections()}


def bus(n, serials, speed=480):
    return {"bus": n, "speed_mbps": speed, "devices": [{"serial": s, "devnum": i + 2}
                                                       for i, s in enumerate(serials)]}


def unit(k):
    return [str(1000 + 100 * k + i) for i in range(5)]


PI4 = {"schema": 1, "nproc": 4, "model": "Raspberry Pi 4 Model B Rev 1.4",
       "mem_available": 3500000000, "shm_size": 1900000000, "shm_free": 1900000000, "memlock": -1,
       "usb_buses": [bus(1, unit(0) + unit(1))], "nics": [{"name": "eth0", "speed_mbps": 1000}], "live": []}
PI5 = dict(PI4, model="Raspberry Pi 5 Model B Rev 1.0", usb_buses=[bus(1, unit(0)), bus(3, unit(1))])
X86 = {"schema": 1, "nproc": 8, "model": "x86 workstation", "mem_available": 16 * GIB,
       "shm_size": 8 * GIB, "shm_free": 8 * GIB, "memlock": -1,
       "usb_buses": [bus(1, unit(0)), bus(3, unit(1)), bus(2, [], 5000)],
       "nics": [{"name": "eth0", "speed_mbps": 10000}], "live": []}


class FormulaTest(unittest.TestCase):
    """capacity_model against the I.1 default column."""

    def setUp(self):
        self.inst = cm.instance_from_params(params_of(ini_text()), "x.ini")
        self.m = cm.instance_model(self.inst)

    def test_default_column(self):
        m = self.m
        self.assertEqual(m["usb_ingress_Bps"] / 1e6, 24.0)
        self.assertEqual(m["usb_transfer_bytes"], 30 * MIB)
        self.assertEqual(m["usb_ring_bytes"], 20 * MIB)
        self.assertEqual(m["rebuffer_ring_bytes"], 15 * MIB)
        self.assertEqual(m["decimator_in_bytes"], 2 * (1024 + (1 << 20) * 5 * 2))
        self.assertEqual(round(m["decimator_in_bytes"] / MIB), 20)
        self.assertEqual(round(m["decimator_in_consumer_map_bytes"] / MIB), 80)
        self.assertEqual(round(m["decimator_out_resident_bytes"] / MIB), 80)
        self.assertEqual(round(m["decimator_out_object_bytes"] / MIB), 640)
        self.assertEqual(round(m["delay_sync_iq_bytes"] / MIB), 80)
        self.assertEqual(m["delay_sync_copy_bytes"], 80 * MIB)  # 40 + 40 MiB per frame
        self.assertEqual(round(m["frame_period_sec"], 3), 0.437)
        self.assertEqual(m["egress_per_client_Bps"], 96e6)
        self.assertEqual(m["egress_per_client_Bps"] * 8 / 1e6, 768.0)
        self.assertEqual(m["cores"], [0, 1, 2, 3])
        self.assertEqual(m["rt_priorities"], {"rtl_daq": 99, "rebuffer": 94, "decimate": 92,
                                              "delay_sync": 90, "iq_server": 88, "hw_controller": 82})
        self.assertEqual(round(100 * 24.0 / cm.usb_bus_budget_MBps(480)), 69)

    def test_size_segments_from_config_shrinks_decimator_out(self):
        inst = cm.instance_from_params(params_of(ini_text()))
        self.assertFalse(inst["size_segments_from_config"])
        text = ini_text().replace("decimation_ratio = 1\n", "decimation_ratio = 1\nsize_segments_from_config = 1\n")
        m = cm.instance_model(cm.instance_from_params(params_of(text)))
        self.assertEqual(m["decimator_out_object_bytes"], m["decimator_out_resident_bytes"])
        self.assertEqual(round(m["shm_object_bytes"] / MIB), 180)

    def test_router_bound(self):
        frame = cm.frame_sizes(5, 1 << 20, 1, 1 << 16)["iq_frame"]
        bound = cm.router_memory_bound(frame, sources=2)
        self.assertEqual(bound, frame * (4 * 8 + 2 * 2 + 2))
        self.assertEqual(round(bound / GIB, 2), 1.48)

    def test_rebuffer_ring_integer_division(self):
        # Ncpi*R/B + 2 vs Nc/B + 2 with C integer division
        self.assertEqual(cm.rebuffer_ring_bytes(1, 1000, 1, 10, 300), 1 * (1000 // 300 + 2) * 300 * 2)
        self.assertEqual(cm.rebuffer_ring_bytes(1, 10, 1, 1000, 300), 1 * (1000 // 300 + 2) * 300 * 2)

    def test_rt_offset_and_default_cores(self):
        self.assertEqual(cm.rt_priorities(2)["rtl_daq"], 97)
        self.assertEqual(cm.default_cores(1, 4), [0, 1, 2, 3])
        self.assertEqual(cm.default_cores(1, 8), [4, 5, 6, 7])
        self.assertEqual(cm.default_cores(1), [4, 5, 6, 7])

    def test_synthetic_has_no_usb(self):
        m = cm.instance_model(cm.instance_from_params(params_of(ini_text()), mode="synthetic"))
        self.assertEqual((m["usb_ingress_Bps"], m["usb_ring_bytes"], m["usb_transfer_bytes"]), (0, 0, 0))

    def test_shmem_output_has_no_egress(self):
        m = cm.instance_model(cm.instance_from_params(params_of(ini_text(iface="shmem"))))
        self.assertEqual(m["egress_Bps"], 0)
        self.assertEqual(m["per_process_locked_bytes"]["iq_server"], 0)

    def test_serials_and_ports(self):
        inst = cm.instance_from_params(params_of(ini_text(3)))
        self.assertEqual(inst["serials"], ["1000", "1001", "1002", "1003", "1004"])
        self.assertEqual(inst["ports"], {"iq": 5300, "ctl": 5301, "status": None, "events": None, "zmq": 1430})
        text = ini_text().replace("num_ch = 5\n", "num_ch = 2\ndevice_serials = 7,9\n") + \
            "[monitoring]\nen_monitoring = 1\nen_status_server = 1\nstatus_server_port = 6100\nen_zmq_pub = 1\n"
        inst = cm.instance_from_params(params_of(text))
        self.assertEqual(inst["serials"], ["7", "9"])
        self.assertEqual(inst["ports"]["status"], 6100)
        self.assertEqual(inst["ports"]["events"], 5003)

    def test_missing_key_is_config_error(self):
        with self.assertRaises(cm.ConfigError) as ctx:
            cm.instance_from_params(params_of(ini_text().replace("corr_size = 65536\n", "")))
        self.assertIn("[calibration] corr_size", str(ctx.exception))

    def test_limits(self):
        text = ini_text(num_ch=32).replace("cpi_size = 1048576", "cpi_size = 16777216")
        problems = cm.limit_violations(cm.instance_from_params(params_of(text)))
        self.assertEqual(len(problems), 2)

    def test_enabled_ports_match_ini_checker(self):
        try:
            import ini_checker
        except Exception as e:  # noqa: BLE001
            self.skipTest("SKIP reason: ini_checker not importable ({0})".format(e))
        if not hasattr(ini_checker, "enabled_ports"):
            self.skipTest("SKIP reason: ini_checker.enabled_ports absent")
        presets = sorted(glob.glob(os.path.join(REPO, "config_files", "*", "daq_chain_config.ini")))
        presets.append(os.path.join(FIRMWARE, "daq_chain_config.ini"))
        for path in presets:
            with self.subTest(preset=path):
                params = daq_preflight.read_ini(path)
                ours = {k: v for k, v in cm.instance_from_params(params, path)["ports"].items() if v is not None}
                self.assertEqual(ours, ini_checker.enabled_ports(params))


class EvaluateTest(unittest.TestCase):
    """Verdicts of cm.evaluate() / daq_preflight.build_plan() on fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hd_capacity_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def report(self, configs, host, **kw):
        host = dict(host, probed=False)
        insts, feds, _ = daq_preflight.build_plan(configs, host, live_configs=False, **kw)
        return cm.evaluate(insts, host, feds)

    def two(self, extra0="", extra1="", iface="eth"):
        """Instances 0 and 1 of two KrakenSDR units (serial_base 1000/1100)."""
        return [self.write("i0.ini", ini_text(0, iface=iface, extra=extra0)),
                self.write("i1.ini", ini_text(1, iface=iface, extra=extra1)
                           .replace("num_ch = 5\n", "num_ch = 5\nserial_base = 1100\n"))]

    def test_pi4_two_hardware_instances_fail_core_rule(self):
        r = self.report(self.two(), PI4)
        self.assertEqual(r["resources"]["cores"]["verdict"], "fail")
        self.assertEqual(r["resources"]["cores"]["need"], 8)
        self.assertEqual(r["resources"]["cores"]["have"], 4)
        self.assertEqual(r["exit"], 2)

    def test_pi5_two_fail_then_warn_with_sharing_allowed(self):
        r = self.report(self.two(), PI5)
        self.assertEqual(r["resources"]["cores"]["verdict"], "fail")
        self.assertEqual(r["resources"]["board"]["verdict"], "warn")
        self.assertEqual(r["exit"], 2)
        allow = "[host]\nallow_core_sharing = allow\n"
        r = self.report(self.two(allow, allow, iface="shmem"), PI5)
        self.assertEqual(r["resources"]["cores"]["verdict"], "warn")
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "ok")
        self.assertEqual(r["resources"]["board"]["verdict"], "warn")
        self.assertEqual(r["exit"], 1)

    def test_core_overlap_policies(self):
        r = self.report(self.two(), PI5)
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "warn")
        self.assertEqual(r["resources"]["core_overlap"]["need"], 4)
        refuse = "[host]\nallow_core_sharing = refuse\n"
        r = self.report(self.two(refuse, ""), PI5)
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "fail")

    def test_x86_two_controllers_ok(self):
        r = self.report(self.two(), X86)
        bad = {k: v for k, v in r["resources"].items() if v["verdict"] not in ("ok", "skip")}
        self.assertEqual(bad, {})
        self.assertEqual(r["exit"], 0)
        self.assertEqual(r["resources"]["usb_bus:1"]["need"], 24.0)
        self.assertEqual(r["resources"]["usb_bus:3"]["need"], 24.0)
        self.assertNotIn("usb_bus:2", r["resources"])
        self.assertEqual(r["resources"]["cores"]["need"], 8)

    def test_two_units_on_one_high_speed_bus_fail(self):
        host = dict(X86, usb_buses=[bus(1, unit(0) + unit(1))])
        r = self.report(self.two(), host)
        self.assertEqual(r["resources"]["usb_bus:1"]["verdict"], "fail")
        self.assertEqual(r["resources"]["usb_bus:1"]["need"], 48.0)
        self.assertEqual(r["resources"]["usb_bus:1"]["have"], 35.0)

    def test_unattached_serials_warn(self):
        host = dict(X86, usb_buses=[bus(1, unit(0))])
        r = self.report(self.two(), host)
        self.assertEqual(r["resources"]["usb_unplaced"]["verdict"], "warn")
        self.assertIn("1100", r["resources"]["usb_unplaced"]["note"])

    def test_duplicate_serials_fail(self):
        r = self.report([self.write("a.ini", ini_text(0)), self.write("b.ini", ini_text(1))], X86)
        self.assertEqual(r["resources"]["serials"]["verdict"], "fail")
        self.assertEqual(r["resources"]["serials"]["need"], 5)

    def test_egress_warn_and_fail(self):
        host = dict(X86, nics=[{"name": "eth0", "speed_mbps": 1000}])
        r = self.report([self.write("a.ini", ini_text(0))], host)
        e = r["resources"]["egress:eth0"]
        self.assertEqual((e["need"], e["have"], e["verdict"]), (96.0, 125.0, "warn"))
        self.assertIn("77 % of link", e["note"])
        r = self.report(self.two(), host)
        self.assertEqual(r["resources"]["egress:eth0"]["verdict"], "fail")
        r = self.report([self.write("s.ini", ini_text(0, iface="shmem"))], host)
        self.assertEqual(r["resources"]["egress"]["verdict"], "skip")

    def test_slow_virtual_link_beside_fast_nic_does_not_fail(self):
        host = dict(X86, nics=[{"name": "eth0", "speed_mbps": 1000}, {"name": "tun0", "speed_mbps": 10},
                               {"name": "eth1", "speed_mbps": 1000}])
        r = self.report([self.write("a.ini", ini_text(0))], host)
        res = r["resources"]
        self.assertEqual(res["egress:eth0"]["verdict"], "warn")
        self.assertEqual(res["egress:eth1"]["verdict"], "warn")
        tun = res["egress:tun0"]
        self.assertEqual((tun["need"], tun["have"], tun["verdict"]), (96.0, 1.25, "skip"))
        self.assertIn("not judged: eth0, eth1 is faster", tun["note"])
        self.assertEqual(r["exit"], 1)
        # A slower link is not judged; a fast enough fastest link is ok.
        host = dict(X86, nics=[{"name": "eth0", "speed_mbps": 10000}, {"name": "eth1", "speed_mbps": 1000}])
        r = self.report([self.write("b.ini", ini_text(0))], host)
        self.assertEqual(r["resources"]["egress:eth0"]["verdict"], "ok")
        self.assertEqual(r["resources"]["egress:eth1"]["verdict"], "skip")
        # When every link is too slow, the fastest one still fails.
        host = dict(X86, nics=[{"name": "eth0", "speed_mbps": 100}, {"name": "tun0", "speed_mbps": 10}])
        r = self.report([self.write("c.ini", ini_text(0))], host)
        self.assertEqual(r["resources"]["egress:eth0"]["verdict"], "fail")
        self.assertEqual(r["resources"]["egress:tun0"]["verdict"], "skip")
        self.assertEqual(r["exit"], 2)

    def test_port_collision_with_coordinator_port_fails(self):
        fed = self.write("federation.ini", "[federation]\ncoordinator_port = 6000\n"
                                           "cluster_members = hosta:0,hosta:10\n")
        i10 = self.write("i10.ini", ini_text(10).replace("num_ch = 5\n", "num_ch = 5\nserial_base = 2000\n"))
        r = self.report([i10, fed], X86)
        ports = r["resources"]["ports"]
        self.assertEqual(ports["verdict"], "fail")
        self.assertIn("port 6000", ports["note"])
        self.assertIn("coordinator", ports["note"])
        self.assertEqual(r["exit"], 2)

    def test_local_coordinator_host_in_instance_ini(self):
        i10 = self.write("i10.ini", ini_text(10, extra="coordinator_host = localhost\n"))
        r = self.report([i10], X86)
        self.assertEqual(r["resources"]["ports"]["verdict"], "fail")
        remote = self.write("r10.ini", ini_text(10, extra="coordinator_host = 203.0.113.9\n"))
        r = self.report([remote], X86)
        self.assertEqual(r["resources"]["ports"]["verdict"], "ok")

    def test_coordinator_named_by_federation_ini_and_instance_ini_counts_once(self):
        # cluster_site on its coordinator host 'hosta': the instance INI's local
        # candidate and the federation.ini are the same listener (port 6000)
        site = os.path.join(REPO, "config_files", "cluster_site")
        is_local = lambda host: host == "hosta"  # noqa: E731
        inst_params = daq_preflight.read_ini(os.path.join(site, "daq_chain_config.ini"))
        fed_params = daq_preflight.read_ini(os.path.join(site, "federation.ini"))
        inst = cm.instance_from_params(inst_params, "inst0", "hw", 8, is_local)
        self.assertEqual(inst["service_ports"], [("coordinator", 6000)])
        fed = cm.federation_from_params(fed_params, "federation.ini", is_local)
        host = dict(X86, probed=False)
        r = cm.evaluate([inst], host, [fed])
        self.assertEqual(r["resources"]["ports"]["verdict"], "ok", r["resources"]["ports"]["note"])
        # a different instance whose iq port is 6000 still collides
        other = cm.instance_from_params(params_of(ini_text(10)),
                                        "i10", "hw", 8, is_local)
        other["ports"] = dict(other["ports"], iq=6000)
        r = cm.evaluate([inst, other], host, [fed])
        self.assertEqual(r["resources"]["ports"]["verdict"], "fail")
        self.assertIn("port 6000", r["resources"]["ports"]["note"])
        # a router declared in both files also counts once
        router = "router_port = 7000\nrouter_stats_port = 7001\n"
        inst_r = cm.instance_from_params(params_of(ini_text(0, extra=router)), "i0", "hw", 8, is_local)
        fed_r = cm.federation_from_params(params_of("[federation]\ncoordinator_port = 6000\n" + router),
                                          "federation.ini", is_local)
        self.assertEqual(cm.evaluate([inst_r], host, [fed_r])["resources"]["ports"]["verdict"], "ok")

    def test_local_candidate_port_is_the_coordinator_port(self):
        is_local = lambda host: host == "me"  # noqa: E731
        p = params_of(ini_text(0, extra="coordinator_port = 6000\n"
                                         "coordinator_candidates = other:6000, me:6100\n"))
        self.assertEqual(cm.service_ports(p, False, is_local), [("coordinator", 6100)])
        p = params_of(ini_text(0, extra="coordinator_candidates = me\n"))
        self.assertEqual(cm.service_ports(p, False, is_local), [("coordinator", 6000)])
        p = params_of(ini_text(0, extra="coordinator_candidates = me:x\n"))
        with self.assertRaises(cm.ConfigError):
            cm.service_ports(p, False, is_local)

    def test_unpinned_synthetic_live_entry_has_no_cores(self):
        a = self.write("a.ini", ini_text(0))
        live = {"schema": 1, "instance_id": 1, "mode": "synthetic", "cores": [],
                "ports": {"iq": 5100, "ctl": 5101, "status": None, "events": None, "zmq": 1230},
                "serials": [], "pids": {}}
        host = dict(X86, nproc=4, live=[live])
        r = self.report([a], host, plus_live=True)
        self.assertEqual(r["instances"][1]["cores"], [])
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "ok")
        self.assertIn("cores in use: 0,1,2,3", r["resources"]["cores"]["note"])
        # a pinned (--rt) synthetic chain on the same cores still overlaps
        r = self.report([a], dict(host, live=[dict(live, cores=[0, 1, 2, 3])]), plus_live=True)
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "warn")

    def test_non_finite_number_is_config_error(self):
        for bad in ("inf", "nan", "-inf"):
            with self.assertRaises(cm.ConfigError):
                cm.instance_from_params(params_of(ini_text(0).replace("cpi_size = 1048576", "cpi_size = " + bad)))

    def test_malformed_local_registry_entry_is_noted_and_ignored(self):
        a = self.write("a.ini", ini_text(0))
        live = {"instance_id": 1, "cores": [None], "pids": {}}
        host = dict(X86, probed=False, live=[live])
        insts, _, notes = daq_preflight.build_plan([a], host, plus_live=True)
        self.assertEqual([i["instance_id"] for i in insts], [0])
        self.assertTrue(any("live[].cores" in n for n in notes), notes)

    def test_ports_above_65535_fail(self):
        r = self.report([self.write("big.ini", ini_text(700))], X86)
        self.assertEqual(r["resources"]["ports"]["verdict"], "fail")
        self.assertIn("above 65535", r["resources"]["ports"]["note"])

    def test_router_memory(self):
        fed = self.write("federation.ini", "[federation]\nrouter_port = 7000\n"
                                           "router_sources = hosta:0,hostb:0\n")
        r = self.report([self.write("a.ini", ini_text(0)), fed], X86)
        rm = r["resources"]["router_memory"]
        frame = cm.frame_sizes(5, 1 << 20, 1, 1 << 16)["iq_frame"]
        self.assertEqual(rm["need"], cm.router_memory_bound(frame, 2))
        self.assertEqual(rm["verdict"], "ok")
        r = self.report([self.write("a.ini", ini_text(0)), fed], dict(X86, mem_available=GIB))
        self.assertEqual(r["resources"]["router_memory"]["verdict"], "fail")
        r = self.report([self.write("a.ini", ini_text(0))], X86)
        self.assertEqual(r["resources"]["router_memory"]["verdict"], "skip")
        self.assertEqual(r["resources"]["router_memory"]["note"], "no federation.ini with router keys")

    def test_memory_checks(self):
        r = self.report([self.write("a.ini", ini_text(0))], dict(X86, mem_available=500 * MIB, shm_free=100 * MIB,
                                                                 shm_size=400 * MIB, memlock=64 * 1024))
        res = r["resources"]
        self.assertEqual(res["mem_available"]["verdict"], "fail")
        self.assertEqual(res["shm_free"]["verdict"], "fail")
        self.assertEqual(res["shm_size"]["verdict"], "warn")
        self.assertEqual(res["memlock"]["verdict"], "warn")
        self.assertEqual(res["mem_available"]["need"],
                         cm.instance_model(cm.instance_from_params(params_of(ini_text())))["memory_need_bytes"]
                         + 512 * MIB)

    def test_frame_period_warn(self):
        slow = ini_text().replace("sample_rate = 2400000", "sample_rate = 100000")
        r = self.report([self.write("slow.ini", slow)], X86)
        self.assertEqual(r["resources"]["frame_period"]["verdict"], "warn")

    def test_limits_fail(self):
        r = self.report([self.write("big.ini", ini_text(num_ch=32))], X86)
        self.assertEqual(r["resources"]["limits"]["verdict"], "fail")

    def test_unprobed_keys_skip(self):
        r = self.report([self.write("a.ini", ini_text(0))], {"schema": 1})
        for key in ("cores", "mem_available", "shm_free", "shm_size", "memlock", "usb_bus", "egress", "board"):
            self.assertEqual(r["resources"][key]["verdict"], "skip", key)
        self.assertEqual(r["exit"], 0)

    def test_add_instances_and_live_entries(self):
        a = self.write("a.ini", ini_text(0))
        host = dict(X86, nproc=16, usb_buses=[bus(i + 1, unit(i)) for i in range(4)])
        r = self.report([a], host, add_instances=2)
        ids = [(i["instance_id"], i["source"]) for i in r["instances"]]
        self.assertEqual(ids, [(0, "config"), (1, "planned"), (2, "planned")])
        self.assertEqual(r["instances"][2]["cores"], [8, 9, 10, 11])
        self.assertEqual(r["instances"][2]["ports"]["iq"], 5200)
        self.assertEqual(r["resources"]["usb_bus:3"]["need"], 24.0)
        live = {"schema": 1, "instance_id": 1, "mode": "hw", "cores": [4, 5, 6, 7],
                "ports": {"iq": 5100, "ctl": 5101, "status": 5102, "events": None, "zmq": 1230},
                "serials": unit(1), "pids": {}}
        r = self.report([a], dict(host, live=[live]), plus_live=True, add_instances=1)
        self.assertEqual([(i["instance_id"], i["source"]) for i in r["instances"]],
                         [(0, "config"), (1, "live"), (2, "planned")])
        self.assertEqual(r["resources"]["cores"]["need"], 12)
        r = self.report([a], dict(host, live=[dict(live, cores=[0, 1, 2, 3])]), plus_live=True)
        self.assertEqual(r["resources"]["core_overlap"]["verdict"], "warn")
        r = self.report([a], dict(host, live=[dict(live, ports=dict(live["ports"], status=5000))]), plus_live=True)
        self.assertEqual(r["resources"]["ports"]["verdict"], "fail")
        # without --plus-live the live entries are ignored
        r = self.report([a], dict(host, live=[live]))
        self.assertEqual(len(r["instances"]), 1)

    def test_add_instances_needs_template(self):
        fed = self.write("federation.ini", "[federation]\ncoordinator_port = 6000\n")
        with self.assertRaises(daq_preflight.UsageError):
            daq_preflight.build_plan([fed], dict(X86), add_instances=1)


class CliTest(unittest.TestCase):
    """daq_preflight.py as a program: schema, exit codes, stdin, dump."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hd_preflight_cli_")
        self.env = dict(os.environ, HEIMDALL_RUN_DIR=os.path.join(self.tmp, "run"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def run_tool(self, *args, stdin=None):
        res = subprocess.run([sys.executable, PREFLIGHT] + list(args), cwd=self.tmp, env=self.env,
                             input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=120)
        return res.returncode, res.stdout, res.stderr

    def test_json_schema_and_exit_codes(self):
        a = self.write("a.ini", ini_text(0))
        b = self.write("b.ini", ini_text(1).replace("num_ch = 5\n", "num_ch = 5\nserial_base = 1100\n"))
        x86 = self.write("x86.json", json.dumps(X86))
        code, out, err = self.run_tool("--config", a, b, "--host-json", x86, "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(set(doc) - {"notes"}, {"schema", "exit", "host", "instances", "resources", "virtual_bytes"})
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["exit"], 0)
        self.assertEqual(doc["host"], {"nproc": 8, "model": "x86 workstation", "probed": False})
        inst = doc["instances"][0]
        self.assertEqual(inst["config"], os.path.abspath(a))
        self.assertEqual((inst["instance_id"], inst["mode"]), (0, "hw"))
        self.assertEqual(inst["ports"], {"iq": 5000, "ctl": 5001, "status": None, "events": None, "zmq": 1130})
        for key in ("cores", "core_overlap", "mem_available", "shm_free", "shm_size", "memlock", "ports",
                    "frame_period", "router_memory"):
            self.assertIn(key, doc["resources"])
        for name, res in doc["resources"].items():
            self.assertEqual(set(res), {"need", "have", "unit", "verdict", "note"}, name)
            self.assertIn(res["verdict"], ("ok", "warn", "fail", "skip"))
        self.assertEqual(doc["virtual_bytes"]["decimator_in_consumer_map"],
                         2 * 2 * cm.frame_sizes(5, 1 << 20, 1, 1 << 16)["decimator_in_consumer_map"])
        # warnings -> 1, --strict turns them into 2
        host = self.write("eth.json", json.dumps(dict(X86, nics=[{"name": "eth0", "speed_mbps": 1000}])))
        code, out, _ = self.run_tool("--config", a, "--host-json", host, "--json")
        self.assertEqual((code, json.loads(out)["exit"]), (1, 1))
        code, out, _ = self.run_tool("--config", a, "--host-json", host, "--json", "--strict")
        self.assertEqual((code, json.loads(out)["exit"]), (2, 2))
        # failures -> 2
        pi4 = self.write("pi4.json", json.dumps(PI4))
        code, out, _ = self.run_tool("--config", a, b, "--host-json", pi4, "--json")
        self.assertEqual((code, json.loads(out)["exit"]), (2, 2))

    def test_host_json_from_stdin_and_text_output(self):
        a = self.write("a.ini", ini_text(0))
        code, out, err = self.run_tool("--config", a, "--host-json", "-", stdin=json.dumps(X86))
        self.assertEqual(code, 0, err)
        self.assertIn("RESOURCE", out)
        self.assertIn("result: ok (exit 0)", out)
        self.assertRegex(out, r"usb_bus:1\s+24\.0 MB/s\s+35\.0 MB/s\s+ok")

    def test_usage_errors_exit_3(self):
        a = self.write("a.ini", ini_text(0))
        self.assertEqual(self.run_tool("--config", os.path.join(self.tmp, "missing.ini"))[0], 3)
        self.assertEqual(self.run_tool("--bogus")[0], 3)
        bad = self.write("bad.json", json.dumps({"nproc": "eight"}))
        code, _, err = self.run_tool("--config", a, "--host-json", bad)
        self.assertEqual(code, 3)
        self.assertIn("nproc", err)
        self.assertEqual(self.run_tool("--config", a, "--host-json", "-", stdin="{not json")[0], 3)
        broken = self.write("broken.ini", ini_text(0).replace("corr_size = 65536\n", ""))
        code, _, err = self.run_tool("--config", broken)
        self.assertEqual(code, 3)
        self.assertIn("corr_size", err)
        inf = self.write("inf.ini", ini_text(0).replace("cpi_size = 1048576", "cpi_size = inf"))
        code, _, err = self.run_tool("--config", inf, "--host-json", "-", stdin=json.dumps(X86))
        self.assertEqual(code, 3, err)
        self.assertIn("cpi_size", err)

    def test_malformed_live_entries_exit_3(self):
        a = self.write("a.ini", ini_text(0))
        base = {"instance_id": 1, "pids": {}}
        for field, value in (("cores", [None]), ("cores", ["a"]), ("cores", 3), ("ports", {"iq": "5100"}),
                             ("ports", {"iq": "x"}), ("ports", [5100]), ("serials", [1000]),
                             ("serials", "1000"), ("rt_priority_offset", "2"), ("mode", "fast"),
                             ("instance_id", -1), ("instance_id", "1")):
            doc = {"nproc": 8, "live": [dict(base, **{field: value})]}
            code, out, err = self.run_tool("--config", a, "--host-json", "-", "--plus-live",
                                           stdin=json.dumps(doc))
            self.assertEqual(code, 3, (field, value, out, err))
            self.assertIn("live[].", err, (field, value))
            self.assertNotIn("Traceback", err)
        ok = {"nproc": 8, "live": [dict(base, cores=[4, 5, 6, 7], ports={"iq": 5100, "status": None},
                                        serials=["1100"], rt_priority_offset=2)]}
        code, _, err = self.run_tool("--config", a, "--host-json", "-", "--plus-live", stdin=json.dumps(ok))
        self.assertIn(code, (0, 1, 2), err)

    def test_internal_error_never_exits_1(self):
        # a crash must not look like "warnings" (exit 1) to hd_preflight/heimdall-ctl
        a = self.write("a.ini", ini_text(0))
        stderr = io.StringIO()
        orig = cm.evaluate

        def boom(*args, **kw):
            raise RuntimeError("model bug")
        cm.evaluate = boom
        try:
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = daq_preflight.main(["--config", a, "--host-json", self.write("x.json", json.dumps(X86))])
        finally:
            cm.evaluate = orig
        self.assertEqual(code, 3)
        self.assertIn("internal error: RuntimeError: model bug", stderr.getvalue())

    def test_default_config_is_cwd_ini(self):
        self.write("daq_chain_config.ini", ini_text(0))
        code, out, _ = self.run_tool("--host-json", self.write("x.json", json.dumps(X86)), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["instances"][0]["config"],
                         os.path.join(os.path.realpath(self.tmp), "daq_chain_config.ini"))

    def test_dump_host_json_round_trip(self):
        code, out, err = self.run_tool("--dump-host-json")
        self.assertEqual(code, 0, err)
        dumped = json.loads(out)
        self.assertEqual(dumped["schema"], 1)
        daq_preflight.validate_host_json(dumped)
        host = self.write("dump.json", out)
        a = self.write("a.ini", ini_text(0))
        code, out, err = self.run_tool("--config", a, "--host-json", host, "--json")
        self.assertIn(code, (0, 1, 2), err)
        doc = json.loads(out)
        self.assertEqual(doc["host"]["nproc"], dumped.get("nproc"))
        self.assertEqual(doc["host"]["model"], dumped.get("model"))
        self.assertFalse(doc["host"]["probed"])

    def test_probed_run_exits_ok_or_warn(self):
        # K.2 acceptance: on a CI runner the probed default plan exits 0 or 1
        a = self.write("a.ini", ini_text(0, iface="shmem"))
        code, out, err = self.run_tool("--config", a, "--json", "--mode", "synthetic")
        self.assertIn(code, (0, 1), out + err)
        self.assertTrue(json.loads(out)["host"]["probed"])

    def test_probed_shipped_default_hw_exits_ok_or_warn(self):
        # K.2 acceptance with the probed hardware path: the shipped default
        # INI in hw mode exits 0 or 1 on a host with at least 4 CPUs; on a
        # smaller runner the core-count rule is the only failure
        shipped = os.path.join(FIRMWARE, "daq_chain_config.ini")
        code, out, err = self.run_tool("--config", shipped, "--json")
        self.assertIn(code, (0, 1, 2), out + err)
        doc = json.loads(out)
        self.assertTrue(doc["host"]["probed"])
        self.assertEqual(doc["instances"][0]["mode"], "hw")
        failing = sorted(k for k, v in doc["resources"].items() if v["verdict"] == "fail")
        nproc = doc["host"]["nproc"]
        if nproc is not None and nproc < cm.CORES_PER_HW_INSTANCE:
            self.assertEqual(failing, ["cores"] if code == 2 else [], out)
        else:
            self.assertEqual(failing, [], out)
            self.assertIn(code, (0, 1), out)

    def test_plus_live_reads_registry(self):
        run = os.path.join(self.tmp, "run")
        os.makedirs(run)
        a = self.write("a.ini", ini_text(0))
        self.write("daq_chain_config.ini", ini_text(1, iface="shmem"))  # the live chain's INI (cwd)
        role = os.path.basename(sys.executable)
        entry = {"schema": 1, "instance_id": 1, "mode": "synthetic", "cwd": self.tmp,
                 "cores": [0, 1, 2, 3], "ports": {"iq": 5100, "ctl": 5101, "status": None, "events": None,
                                                  "zmq": 1230},
                 "serials": [], "pids": {role: os.getpid()}}
        with open(os.path.join(run, "inst1.json"), "w") as f:
            json.dump(entry, f)
        dead = dict(entry, instance_id=2, pids={role: 2 ** 22 + 12345})
        with open(os.path.join(run, "inst2.json"), "w") as f:
            json.dump(dead, f)
        code, out, err = self.run_tool("--config", a, "--plus-live", "--json")
        doc = json.loads(out)
        # run_dirs() also scans /tmp/heimdall-run-<uid>: ignore chains of this
        # developer machine, keep the ones of this test's registry
        tmp = os.path.realpath(self.tmp)
        mine = [(i["instance_id"], i["source"]) for i in doc["instances"]
                if i["config"] and os.path.realpath(i["config"]).startswith(tmp + os.sep)]
        self.assertEqual(mine, [(0, "config"), (1, "live")], err)
        self.assertEqual(doc["resources"]["core_overlap"]["need"], 4)


class ProbeTest(unittest.TestCase):
    """daq_preflight probe helpers on fake trees."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hd_probe_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_fake_sysfs_proc(self):
        for name, busnum, speed in (("usb1", 1, "480"), ("usb2", 2, "5000")):
            self.put("usb/{0}/busnum".format(name), str(busnum))
            self.put("usb/{0}/speed".format(name), speed)
        for i, serial in enumerate(("1000", "1001")):
            d = "usb/1-1.{0}".format(i + 1)
            for attr, val in (("idVendor", "0bda"), ("idProduct", "2838"), ("busnum", "1"),
                              ("devnum", str(i + 3)), ("serial", serial)):
                self.put("{0}/{1}".format(d, attr), val)
        self.put("net/eth0/speed", "1000")
        self.put("net/wlan0/speed", "-1")
        self.put("net/lo/speed", "10")
        self.put("net/tun0/speed", "10")
        self.put("net/tun0/tun_flags", "0x1001")
        self.put("proc/meminfo", "MemTotal: 100 kB\nMemAvailable:  2048 kB\n")
        os.makedirs(os.path.join(self.tmp, "run"))
        host = daq_preflight.host_probe(sysfs_usb_root=os.path.join(self.tmp, "usb"),
                                        net_root=os.path.join(self.tmp, "net"),
                                        proc_root=os.path.join(self.tmp, "proc"),
                                        shm_path=self.tmp, registry_dirs=[os.path.join(self.tmp, "run")])
        daq_preflight.validate_host_json(host)
        self.assertEqual(host["mem_available"], 2048 * 1024)
        self.assertEqual(host["usb_buses"], [
            {"bus": 1, "speed_mbps": 480, "devices": [{"serial": "1000", "devnum": 3},
                                                       {"serial": "1001", "devnum": 4}]},
            {"bus": 2, "speed_mbps": 5000, "devices": []}])
        self.assertEqual(host["nics"], [{"name": "eth0", "speed_mbps": 1000}])
        self.assertGreater(host["shm_size"], 0)
        self.assertEqual(host["live"], [])

    def test_missing_trees_are_not_probed(self):
        missing = os.path.join(self.tmp, "nope")
        host = daq_preflight.host_probe(sysfs_usb_root=missing, net_root=missing, proc_root=missing,
                                        shm_path=missing, registry_dirs=[])
        for key in ("usb_buses", "nics", "mem_available", "shm_size", "shm_free"):
            self.assertNotIn(key, host)

    def test_run_dir_hints(self):
        self.put("fw/_logs/inst1/pids/run_dir", "/somewhere/run\n")
        dirs = daq_preflight.run_dirs(os.path.join(self.tmp, "fw"))
        self.assertIn("/somewhere/run", dirs)


class _FakeIQServer(object):
    """Speaks iq_server's port-5000 protocol: waits for `streaming`, sends a
    frame, then one frame per `IQDownload`."""

    def __init__(self, frames, payload_ch=2, cpi=64, delay_ms=40):
        self.frames = frames
        self.payload_ch, self.cpi, self.delay_ms = payload_ch, cpi, delay_ms
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.acks = 0
        self.handshake = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _frame(self, cpi_index):
        from iq_header import IQHeader
        h = IQHeader()
        h.active_ant_chs = self.payload_ch
        h.cpi_length = self.cpi
        h.sample_bit_depth = 32
        h.cpi_index = cpi_index
        h.header_version = 8
        h.time_stamp = int(time.time() * 1000) - self.delay_ms
        return h.encode_header() + b"\x00" * (self.cpi * self.payload_ch * 8)

    def _serve(self):
        conn, _ = self.listener.accept()
        conn.settimeout(10)
        with conn:
            self.handshake = conn.recv(64)
            for i, idx in enumerate(self.frames):
                if i:
                    ack = conn.recv(64)
                    if ack != b"IQDownload":
                        return
                    self.acks += 1
                conn.sendall(self._frame(idx))
            conn.recv(64)  # last ack or close

    def close(self):
        self.listener.close()
        self.thread.join(5)


@unittest.skipIf(pm is None, "SKIP reason: util/performance_monitor.py not importable ({0})".format(_PM_ERROR))
class PerformanceToolingTest(unittest.TestCase):

    def test_parse_sched(self):
        text = ("delay_sync.py (1234, #threads: 9)\n-----------------\nse.exec_start : 1.0\n"
                "policy                                       :                    1\n"
                "prio                                         :                    9\n")
        self.assertEqual(pm.parse_sched(text), {"policy": 1, "policy_name": "SCHED_FIFO", "rt": True, "prio": 9})
        self.assertFalse(pm.parse_sched("policy : 0\nprio : 120\n")["rt"])
        self.assertIsNone(pm.parse_sched("nothing here\n"))

    def test_percentile_and_summary(self):
        self.assertEqual(pm.percentile([5, 1, 3, 2, 4], 50), 3)
        self.assertEqual(pm.percentile(list(range(1, 101)), 95), 95)
        samples = [{"recv_ms": 0, "cpi_index": i, "frame_type": 0, "bytes": 1000, "latency_ms": float(i)}
                   for i in (0, 1, 2, 5)]
        s = pm.summarize_samples(samples, 2.0)
        self.assertEqual(s["frames"], 4)
        self.assertEqual(s["throughput_MBps"], 4 * 1000 / 2.0 / 1e6)
        self.assertEqual(s["cpi_gaps"], 2)
        self.assertEqual(s["latency_max_ms"], 5.0)

    def test_stream_through_real_client(self):
        server = _FakeIQServer([10, 11, 12, 14, 15], delay_ms=40)
        try:
            res = pm.measure_stream("127.0.0.1", server.port, frames=5, timeout=10)
        finally:
            server.close()
        self.assertEqual(server.handshake, b"streaming")
        self.assertEqual(server.acks, 4)
        self.assertEqual(res["frames"], 5)
        frame_bytes = 1024 + 64 * 2 * 8
        self.assertEqual(res["bytes"], 5 * frame_bytes)
        self.assertEqual(res["cpi_gaps"], 1)
        self.assertGreaterEqual(res["latency_p50_ms"], 39.0)
        self.assertLess(res["latency_max_ms"], 5000.0)
        self.assertAlmostEqual(res["throughput_MBps"], res["bytes"] / res["elapsed_s"] / 1e6)

    def test_closed_connection_raises(self):
        server = _FakeIQServer([1])
        try:
            with self.assertRaises(ConnectionError):
                pm.measure_stream("127.0.0.1", server.port, frames=3, timeout=10)
        finally:
            server.close()

    def test_affinity_expectations(self):
        tmp = tempfile.mkdtemp(prefix="hd_pm_")
        try:
            with open(os.path.join(tmp, "daq_chain_config.ini"), "w") as f:
                f.write(ini_text(1, extra="[host]\ncpu_cores = 8,9,10,11\ndelay_sync_cores = 10,12\n"
                                          "rt_priority_offset = 3\n"))
            run = os.path.join(tmp, "run")
            os.makedirs(run)
            mon = pm.DAQPerformanceMonitor(1, tmp, [run])
            layout = mon.expected_layout()
            self.assertEqual((layout["source"], layout["cores"], layout["rt_priority_offset"]),
                             ("config", [8, 9, 10, 11], 3))
            exp = mon.expected_affinity(layout)
            self.assertEqual(exp["rtl_daq"], [8])
            self.assertEqual(exp["decimate"], [9])
            self.assertEqual(exp["delay_sync"], [10, 12])
            self.assertEqual(exp["iq_server"], [10])
            self.assertEqual(exp["hw_controller"], [11])
            role = os.path.basename(sys.executable)
            with open(os.path.join(run, "inst1.json"), "w") as f:
                json.dump({"instance_id": 1, "cores": [4, 5, 6, 7], "pids": {role: os.getpid()}}, f)
            layout = mon.expected_layout()
            self.assertEqual((layout["source"], layout["cores"]), ("registry", [4, 5, 6, 7]))
            os.remove(os.path.join(run, "inst1.json"))
            with open(os.path.join(tmp, "daq_chain_config.ini"), "w") as f:
                f.write(ini_text(1))
            self.assertEqual(mon.expected_layout()["source"], "default")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_affinity_follows_start_script_on_small_hosts(self):
        mon = pm.DAQPerformanceMonitor(0, FIRMWARE, [])
        mon.processes = {"rtl_daq": 1, "rebuffer": 2, "decimate": 3, "delay_sync": 4,
                         "iq_server": 5, "hw_controller": 6}
        layout = {"source": "default", "delay_sync_cores": [], "rt_priority_offset": 0,
                  "cpu_cores_set": False}
        exp = mon.expected_affinity(dict(layout, nproc=3, cores=[0, 1, 2, 0]))
        self.assertEqual((exp["delay_sync"], exp["iq_server"], exp["hw_controller"]), ([2], None, None))
        exp = mon.expected_affinity(dict(layout, nproc=2, cores=[0, 1, 0, 1]))
        self.assertEqual((exp["rtl_daq"], exp["decimate"], exp["delay_sync"]), ([0], [1], [1]))
        self.assertEqual((exp["iq_server"], exp["hw_controller"]), (None, None))
        exp = mon.expected_affinity(dict(layout, nproc=4, cores=[0, 1, 2, 3]))
        self.assertEqual((exp["iq_server"], exp["hw_controller"]), ([2], [3]))
        # [host] cpu_cores pins every tier whatever nproc is
        exp = mon.expected_affinity(dict(layout, nproc=2, cores=[0, 1, 0, 1], cpu_cores_set=True,
                                         source="registry"))
        self.assertEqual((exp["delay_sync"], exp["hw_controller"]), ([0], [1]))
        # daq_synthetic_start.sh --rt pins the four tiers on any host
        mon.processes = {"synthesizer": 1, "delay_sync": 4, "hw_controller": 6}
        exp = mon.expected_affinity(dict(layout, nproc=2, cores=[0, 1, 0, 1]))
        self.assertEqual((exp["delay_sync"], exp["hw_controller"]), ([0], [1]))

    def test_pid_discovery(self):
        tmp = tempfile.mkdtemp(prefix="hd_pm_")
        try:
            pids = os.path.join(tmp, "_logs", "inst0", "pids")
            os.makedirs(pids)
            with open(os.path.join(pids, "delay_sync.pid"), "w") as f:
                f.write("{0}\n".format(os.getpid()))
            with open(os.path.join(pids, "rebuffer.pid"), "w") as f:
                f.write("{0}\n".format(2 ** 22 + 4321))
            mon = pm.DAQPerformanceMonitor(0, tmp, [])
            self.assertEqual(mon.discover_daq_processes(quiet=True), {"delay_sync": os.getpid()})
            rt = mon.check_realtime_status()
            self.assertIn("delay_sync", rt["process_scheduling"])
            self.assertEqual(rt["process_scheduling"]["delay_sync"].get("expected_rt_priority", 90), 90)
            # an unpinned synthetic chain (no --rt) has no affinity expectations
            mon.processes = {"synthesizer": os.getpid(), "delay_sync": os.getpid()}
            self.assertFalse(mon.chain_pinned())
            self.assertEqual(mon.validate_cpu_affinity(), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipIf(pm is not None and pm.psutil is None, "SKIP reason: psutil not installed")
    def test_continuous_monitoring_with_iq_client(self):
        tmp = tempfile.mkdtemp(prefix="hd_pm_")
        server = _FakeIQServer(list(range(200)), delay_ms=25)
        try:
            pids = os.path.join(tmp, "_logs", "inst0", "pids")
            os.makedirs(pids)
            with open(os.path.join(pids, "delay_sync.pid"), "w") as f:
                f.write("{0}\n".format(os.getpid()))
            mon = pm.DAQPerformanceMonitor(0, tmp, [])
            out = os.path.join(tmp, "m.json")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                mon.start_monitoring(interval=0.2, duration=1.0, iq_host="127.0.0.1", iq_port=server.port)
                mon.export_metrics(out)
            self.assertIn("IQ CLIENT", printed.getvalue())
            with open(out) as f:
                doc = json.load(f)
            self.assertTrue(doc["metrics"]["stage_metrics"])
            self.assertIn("delay_sync", doc["metrics"]["stage_metrics"][0][1])
            self.assertGreater(doc["latency_summary"]["frames"], 0)
            self.assertGreaterEqual(doc["latency_summary"]["latency_p50_ms"], 24.0)
        finally:
            server.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
