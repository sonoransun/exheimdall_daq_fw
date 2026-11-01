"""
    Unit tests for federation_common (membership grammar, :6000 framing, fan-out)

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Hardware-free and CI-safe: framing tests use socket.socketpair(), the
    client-helper test binds an ephemeral port, and every timeout is a short
    parameter.
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '_daq_core'))

import federation_common as fc  # noqa: E402


# ---------------------------------------------------------------------------
# Membership grammar
# ---------------------------------------------------------------------------

class TestNodeKeyAndIdentity(unittest.TestCase):

    def test_node_key_lower_cases_host(self):
        self.assertEqual(fc.node_key("HostA", 1), "hosta:1")
        self.assertEqual(fc.node_key("10.0.0.5", "2"), "10.0.0.5:2")

    def test_node_key_brackets_ipv6(self):
        self.assertEqual(fc.node_key("FE80::1", 0), "[fe80::1]:0")

    def test_compute_ports(self):
        self.assertEqual(fc.compute_ports(0),
                         {"iq": 5000, "ctl": 5001, "status": 5002, "events": 5003, "zmq": 1130})
        self.assertEqual(fc.compute_ports(1),
                         {"iq": 5100, "ctl": 5101, "status": 5102, "events": 5103, "zmq": 1230})
        self.assertEqual(fc.compute_ports(2, port_stride=10, status_base_port=6002)["status"],
                         6022)

    def test_grammar_validators(self):
        self.assertTrue(fc.is_valid_node_id("hosta:1"))
        self.assertTrue(fc.is_valid_node_id("a" * 48))
        self.assertFalse(fc.is_valid_node_id("a" * 49))
        self.assertFalse(fc.is_valid_node_id("-bad"))
        self.assertFalse(fc.is_valid_node_id("has space"))
        self.assertTrue(fc.is_valid_cluster_id("lab-a"))
        self.assertTrue(fc.is_valid_cluster_id("c" * 31))
        self.assertFalse(fc.is_valid_cluster_id("c" * 32))
        self.assertTrue(fc.is_valid_auth_token("s3cr3t!"))
        self.assertFalse(fc.is_valid_auth_token("with space"))
        self.assertFalse(fc.is_valid_auth_token(""))
        self.assertFalse(fc.is_valid_auth_token("x" * 129))

    def test_default_node_id(self):
        with mock.patch("socket.gethostname", return_value="KrakenSDR.lan"):
            self.assertEqual(fc.default_node_id(3), "krakensdr:3")
        with mock.patch("socket.gethostname", return_value="weird host!" + "x" * 80):
            nid = fc.default_node_id(12)
            self.assertTrue(fc.is_valid_node_id(nid), nid)
            self.assertTrue(nid.endswith(":12"))

    def test_self_host(self):
        with mock.patch("socket.gethostname", return_value="HostA.example.org"):
            self.assertEqual(fc.self_host(), "hosta")
            self.assertEqual(fc.self_host("Edge-1"), "edge-1")
            self.assertEqual(fc.self_host("  "), "hosta")
            with self.assertRaises(ValueError):
                fc.self_host("127.0.0.1")
        with mock.patch("socket.gethostname", return_value="localhost"):
            with self.assertRaisesRegex(ValueError, "advertise_host"):
                fc.self_host()

    def test_self_member(self):
        with mock.patch("socket.gethostname", return_value="hosta"):
            m = fc.self_member(1)
        self.assertEqual(m["node_key"], "hosta:1")
        self.assertEqual(m["host"], "hosta")
        self.assertEqual(m["connect_host"], "127.0.0.1")
        self.assertEqual(m["ports"]["ctl"], 5101)
        self.assertTrue(m["self"])
        m = fc.self_member(2, port_stride=10, advertise_host="10.0.0.9")
        self.assertEqual(m["node_key"], "10.0.0.9:2")
        self.assertEqual(m["ports"]["iq"], 5020)


class TestParseMember(unittest.TestCase):

    def test_canonical_entry(self):
        m = fc.parse_member("HostA:1")
        self.assertEqual(m["host"], "HostA")
        self.assertEqual(m["connect_host"], "HostA")
        self.assertEqual(m["instance_id"], 1)
        self.assertEqual(m["node_key"], "hosta:1")
        self.assertIsNone(m["node_id"])
        self.assertEqual(m["port_stride"], 100)
        self.assertEqual(m["ports"],
                         {"iq": 5100, "ctl": 5101, "status": 5102, "events": 5103, "zmq": 1230})
        self.assertFalse(m["legacy_status_port"])
        self.assertEqual(m["entry"], "HostA:1")

    def test_node_id_suffix(self):
        m = fc.parse_member(" hosta:0@site-a.rx1 ")
        self.assertEqual(m["node_id"], "site-a.rx1")
        self.assertEqual(m["node_key"], "hosta:0")
        self.assertEqual(m["entry"], "hosta:0@site-a.rx1")

    def test_stride_and_status_base(self):
        m = fc.parse_member("h:3", default_stride=10, status_base_port=6002)
        self.assertEqual(m["ports"]["status"], 6032)
        self.assertEqual(m["ports"]["iq"], 5030)
        self.assertEqual(m["port_stride"], 10)

    def test_ipv6_bracketed(self):
        m = fc.parse_member("[fe80::1]:2")
        self.assertEqual(m["host"], "fe80::1")
        self.assertEqual(m["node_key"], "[fe80::1]:2")

    def test_exact_error_strings(self):
        for bad in ("hosta", "hosta:", ":1", "hosta:x", "hosta:-1", "a b:1", "fe80::1:2",
                    "host/a:1", "hosta:1:2"):
            with self.assertRaises(ValueError) as cm:
                fc.parse_member(bad)
            self.assertEqual(str(cm.exception),
                             "bad member entry '{}' (expected host:instance_id[@node_id])"
                             .format(bad))
        with self.assertRaises(ValueError) as cm:
            fc.parse_member("hosta:1@-bad")
        self.assertEqual(str(cm.exception), "bad node_id '-bad'")
        with self.assertRaises(ValueError) as cm:
            fc.parse_member("hosta:1@")
        self.assertEqual(str(cm.exception), "bad node_id ''")

    def test_port_overflow_is_an_error_in_canonical_form(self):
        # host:5102 in cluster_members means instance 5102 -> ports > 65535
        with self.assertRaises(ValueError) as cm:
            fc.parse_member("hosta:5102")
        self.assertIn("bad member entry 'hosta:5102'", str(cm.exception))

    def test_loopback_rejected_by_default(self):
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "[::1]", "LOCALHOST"):
            entry = "{}:0".format(host)
            with self.assertRaises(ValueError) as cm:
                fc.parse_member(entry)
            self.assertEqual(str(cm.exception),
                             "member '{}' uses a loopback host; use the host's advertised name"
                             .format(entry))
        self.assertEqual(fc.parse_member("127.0.0.1:0", allow_loopback=True)["node_key"],
                         "127.0.0.1:0")

    def test_legacy_status_port_form(self):
        m = fc.parse_member("hostb:5102", legacy_port_form=True)
        self.assertEqual(m["instance_id"], 1)
        self.assertEqual(m["node_key"], "hostb:1")
        self.assertEqual(m["ports"]["status"], 5102)
        self.assertEqual(m["ports"]["ctl"], 5101)
        self.assertTrue(m["legacy_status_port"])
        m = fc.parse_member("hostb:5002", legacy_port_form=True)
        self.assertEqual(m["instance_id"], 0)

    def test_legacy_form_unfit_port(self):
        m = fc.parse_member("hostb:7777", legacy_port_form=True)
        self.assertEqual(m["instance_id"], -1)
        self.assertEqual(m["node_key"], "hostb:port7777")
        self.assertEqual(m["ports"]["status"], 7777)
        self.assertIsNone(m["ports"]["ctl"])
        self.assertIsNone(m["ports"]["iq"])
        # below base and stride 0 also do not fit (today's rule)
        self.assertEqual(fc.parse_member("h:4000", legacy_port_form=True)["instance_id"], -1)
        self.assertEqual(fc.parse_member("h:5002", legacy_port_form=True,
                                         default_stride=0)["instance_id"], -1)
        # today's rule does not range-check legacy entries: accepted, never answers
        m = fc.parse_member("h:70000", legacy_port_form=True)
        self.assertEqual((m["instance_id"], m["ports"]["status"]), (-1, 70000))

    def test_legacy_form_small_number_is_instance_id(self):
        m = fc.parse_member("hostb:3", legacy_port_form=True)
        self.assertEqual(m["instance_id"], 3)
        self.assertFalse(m["legacy_status_port"])

    def test_legacy_form_allows_loopback(self):
        self.assertEqual(fc.parse_member("127.0.0.1:1", legacy_port_form=True)["node_key"],
                         "127.0.0.1:1")

    def test_legacy_form_logged_once(self):
        entry = "logonce-host:5302"
        with self.assertLogs("federation_common", level="INFO") as cm:
            fc.parse_member(entry, legacy_port_form=True)
            fc.parse_member(entry, legacy_port_form=True)
            fc.parse_member("other-logonce:5302", legacy_port_form=True)
        lines = [r for r in cm.output if entry in r]
        self.assertEqual(len(lines), 1)
        self.assertIn("peer_list entry '{}' is a legacy status-port form".format(entry),
                      lines[0])

    def test_matches_federation_health_legacy_rule(self):
        """parse_member(legacy_port_form=True) == today's FederationHealth._parse_peer."""
        def today(addr, base=5002, stride=100):
            host, num_str = addr.rsplit(":", 1)
            num = int(num_str)
            if num >= 1024:
                port = num
                offset = num - base
                if stride > 0 and offset >= 0 and offset % stride == 0:
                    iid = offset // stride
                else:
                    iid = -1
            else:
                iid = num
                port = base + iid * stride
            return host, port, iid
        for addr in ("h:0", "h:7", "h:1023", "h:1024", "h:5002", "h:5102", "h:5150", "h:9002",
                     "2001:db8::5:5102", "fe80::1:5102", "::1:5002", "fe80::1%eth0:5102",
                     "h:" + "9" * 30):
            host, port, iid = today(addr)
            m = fc.parse_member(addr, legacy_port_form=True)
            self.assertEqual((m["host"], m["ports"]["status"], m["instance_id"]),
                             (host, port, iid), addr)
        # today's int() strips whitespace around the number
        _host, port, iid = today("hostb: 5102")
        m = fc.parse_member("hostb: 5102", legacy_port_form=True)
        self.assertEqual((m["host"], m["ports"]["status"], m["instance_id"]),
                         ("hostb", port, iid))

    def test_legacy_form_unbracketed_ipv6(self):
        m = fc.parse_member("2001:db8::5:5102", legacy_port_form=True)
        self.assertEqual((m["host"], m["instance_id"], m["node_key"]),
                         ("2001:db8::5", 1, "[2001:db8::5]:1"))
        m = fc.parse_member("::1:5002", legacy_port_form=True)
        self.assertEqual((m["host"], m["node_key"]), ("::1", "[::1]:0"))
        # a host with ':' that is not an IPv6 literal stays an error
        with self.assertRaisesRegex(ValueError, r"^bad member entry 'a:b:5102'"):
            fc.parse_member("a:b:5102", legacy_port_form=True)

    def test_legacy_relaxations_not_in_strict_grammar(self):
        for entry in ("2001:db8::5:1", "hostb: 1", "hostb :1"):
            with self.assertRaisesRegex(ValueError, r"^bad member entry "):
                fc.parse_member(entry, allow_loopback=True)

    def test_huge_number_is_bad_member_entry(self):
        entry = "h:" + "9" * 5000
        expected = "bad member entry '{}' (expected host:instance_id[@node_id])".format(entry)
        for legacy in (False, True):
            with self.assertRaises(ValueError) as cm:
                fc.parse_member(entry, legacy_port_form=legacy)
            self.assertEqual(str(cm.exception), expected, legacy)
        # canonical form: more than ten digits is refused without conversion
        with self.assertRaisesRegex(ValueError, r"^bad member entry "):
            fc.parse_member("h:" + "1" * 11)

    def test_non_ascii_digits_rejected(self):
        with self.assertRaisesRegex(ValueError, r"^bad member entry "):
            fc.parse_member("h:\u0663")


class TestParseMemberList(unittest.TestCase):

    def test_list_and_empty_items(self):
        ms = fc.parse_member_list("hosta:0, hostb:1@b1,,  ")
        self.assertEqual([m["node_key"] for m in ms], ["hosta:0", "hostb:1"])
        self.assertEqual(ms[1]["node_id"], "b1")
        self.assertEqual(fc.parse_member_list(""), [])
        self.assertEqual(fc.parse_member_list(None), [])
        self.assertEqual(len(fc.parse_member_list(["hosta:0", "hostb:0"])), 2)

    def test_duplicates(self):
        with self.assertRaises(ValueError) as cm:
            fc.parse_member_list("hosta:0,HOSTA:0")
        self.assertEqual(str(cm.exception), "duplicate member 'hosta:0'")
        ms = fc.parse_member_list("hosta:0,HOSTA:0", allow_duplicates=True)
        self.assertEqual(len(ms), 2)

    def test_duplicate_node_id_suffix_still_duplicate_key(self):
        with self.assertRaises(ValueError):
            fc.parse_member_list("hosta:0@x,hosta:0@y")

    def test_peer_list_mode(self):
        # peer_list: legacy form + duplicates kept, loopback allowed, order kept
        ms = fc.parse_member_list("127.0.0.1:5102,127.0.0.1:1,hostc:2", legacy_port_form=True,
                                  allow_duplicates=True)
        self.assertEqual([m["node_key"] for m in ms],
                         ["127.0.0.1:1", "127.0.0.1:1", "hostc:2"])

    def test_cluster_members_mode_rejects_loopback(self):
        with self.assertRaises(ValueError) as cm:
            fc.parse_member_list("hosta:0,localhost:1")
        self.assertIn("uses a loopback host", str(cm.exception))

    def test_kwargs_forwarded(self):
        ms = fc.parse_member_list("h:1", default_stride=10)
        self.assertEqual(ms[0]["ports"]["ctl"], 5011)


class TestRouterSources(unittest.TestCase):

    def test_iqserver_source(self):
        s = fc.parse_router_source("HostA:2@rx2")
        self.assertEqual(s, {"kind": "iqserver", "entry": "HostA:2@rx2", "host": "HostA",
                             "connect_host": "HostA", "instance_id": 2,
                             "node_key": "hosta:2", "node_id": "rx2", "port": 5200})
        self.assertEqual(fc.parse_router_source("h:1", default_stride=10)["port"], 5010)

    def test_router_source(self):
        s = fc.parse_router_source("router://Edge1:7000")
        self.assertEqual(s, {"kind": "router", "entry": "router://Edge1:7000", "host": "Edge1",
                             "connect_host": "Edge1", "port": 7000, "node_key": None,
                             "via": "Edge1:7000"})

    def test_bad_router_source_strings(self):
        for bad in ("router://h", "router://h:0", "router://h:70000", "http://h:1", "h", "h:x",
                    "router://:7000"):
            with self.assertRaises(ValueError) as cm:
                fc.parse_router_source(bad)
            self.assertEqual(str(cm.exception),
                             "bad router source '{}' (expected host:instance_id[@node_id] or "
                             "router://host:port)".format(bad))
        with self.assertRaises(ValueError) as cm:
            fc.parse_router_source("h:1@!")
        self.assertEqual(str(cm.exception), "bad node_id '!'")

    def test_router_source_huge_port(self):
        for entry in ("router://h:" + "9" * 5000, "h:" + "9" * 5000):
            with self.assertRaises(ValueError) as cm:
                fc.parse_router_source(entry)
            self.assertTrue(str(cm.exception).startswith("bad router source '"),
                            str(cm.exception)[:80])

    def test_router_source_list(self):
        srcs = fc.parse_router_source_list("hosta:0, router://edge:7000 ,127.0.0.1:1")
        self.assertEqual([s["kind"] for s in srcs], ["iqserver", "router", "iqserver"])

    def test_duplicate_router_source(self):
        with self.assertRaises(ValueError) as cm:
            fc.parse_router_source_list("hosta:0,HostA:0")
        self.assertEqual(str(cm.exception), "duplicate router source 'hosta:0'")
        with self.assertRaises(ValueError) as cm:
            fc.parse_router_source_list("router://e:7000,router://E:7000")
        self.assertEqual(str(cm.exception), "duplicate router source 'router://e:7000'")

    def test_loopback_with_tag_mode_node(self):
        fc.parse_router_source_list("127.0.0.1:0", tag_mode="instance")
        with self.assertRaises(ValueError) as cm:
            fc.parse_router_source_list("localhost:0", tag_mode="node")
        self.assertEqual(str(cm.exception),
                         "router source 'localhost:0' uses a loopback host; with tag_mode=node "
                         "use the host's advertised name")
        # router:// entries carry upstream tags: not keyed locally
        fc.parse_router_source_list("router://127.0.0.1:7000", tag_mode="node")

    def test_bad_tag_mode(self):
        with self.assertRaises(ValueError):
            fc.parse_router_source_list("h:0", tag_mode="bogus")


class TestLocalHost(unittest.TestCase):

    def setUp(self):
        fc.clear_local_host_cache()

    def tearDown(self):
        fc.clear_local_host_cache()

    def test_is_loopback(self):
        for h in ("localhost", "LocalHost", "127.0.0.1", "127.255.0.9", "::1", "[::1]",
                  "::ffff:127.0.0.1", "ip6-localhost", "foo.localhost"):
            self.assertTrue(fc.is_loopback(h), h)
        for h in ("10.0.0.1", "hosta", "", None, "128.0.0.1", "::2"):
            self.assertFalse(fc.is_loopback(h), h)

    def test_loopback_and_hostname_are_local(self):
        self.assertTrue(fc.is_local_host("127.0.0.1"))
        self.assertTrue(fc.is_local_host("localhost"))
        name = socket.gethostname()
        self.assertTrue(fc.is_local_host(name))
        self.assertTrue(fc.is_local_host(name.upper()))
        self.assertTrue(fc.is_local_host(name.split(".", 1)[0]))
        self.assertFalse(fc.is_local_host(None))
        self.assertFalse(fc.is_local_host(""))

    def test_remote_literals_are_not_local(self):
        # TEST-NET-3 / documentation prefix are never assigned to a CI runner
        self.assertFalse(fc.is_local_host("203.0.113.77"))
        self.assertFalse(fc.is_local_host("2001:db8::77"))

    def test_unresolved_name_is_not_local_without_resolve(self):
        with mock.patch("socket.getaddrinfo") as gai:
            self.assertFalse(fc.is_local_host("definitely-not-this-host.invalid"))
            gai.assert_not_called()

    def test_resolve_name_to_local_address(self):
        fake = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 0))]
        real = socket.getaddrinfo

        def gai(host, *a, **k):
            if host == "alias-of-me.example":
                return fake
            return real(host, *a, **k)
        with mock.patch("socket.getaddrinfo", side_effect=gai):
            self.assertTrue(fc.is_local_host("alias-of-me.example", resolve=True))

    def test_local_interface_address(self):
        # an address actually bound to a local interface (if the host has one besides lo)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("203.0.113.1", 9))
            addr = s.getsockname()[0]
            s.close()
        except OSError:
            self.skipTest("no IPv4 route on this host")
        if addr in ("0.0.0.0",):
            self.skipTest("no routable local address")
        self.assertTrue(fc.is_local_host(addr), addr)

    def test_non_unicast_addresses_are_not_local(self):
        # bind() accepts these on Linux/macOS, but none is an interface address
        for h in ("224.0.0.1", "239.1.2.3", "0.0.0.0", "255.255.255.255", "240.0.0.1",
                  "::", "ff02::1", "::ffff:224.0.0.1", "::ffff:0.0.0.0"):
            self.assertFalse(fc.is_local_host(h), h)

    def test_cache_is_bounded(self):
        with mock.patch.object(fc, "LOCAL_HOST_CACHE_MAX", 8), \
                mock.patch.object(fc, "_compute_local", return_value=False):
            for i in range(50):
                fc.is_local_host("host-{}.example".format(i))
            self.assertEqual(len(fc._local_cache), 8)
            # the newest answers are the ones kept
            self.assertIn(("host-49.example", False), fc._local_cache)
            self.assertNotIn(("host-0.example", False), fc._local_cache)

    def test_cached(self):
        with mock.patch.object(fc, "_compute_local", return_value=True) as comp:
            self.assertTrue(fc.is_local_host("10.9.8.7"))
            self.assertTrue(fc.is_local_host("10.9.8.7"))
            self.assertEqual(comp.call_count, 1)
            fc.clear_local_host_cache()
            fc.is_local_host("10.9.8.7")
            self.assertEqual(comp.call_count, 2)


# ---------------------------------------------------------------------------
# :6000 request assembly (D.1)
# ---------------------------------------------------------------------------

FAST = dict(first_timeout=2.0, chunk_timeout=0.3, line_timeout=3.0, body_timeout=1.0)


class _Pair(object):
    """socketpair: server end for read_request, client end for the test."""

    def __init__(self):
        self.server, self.client = socket.socketpair()

    def send_later(self, parts, gap=0.05, shutdown=False):
        def run():
            try:
                for p in parts:
                    self.client.sendall(p)
                    time.sleep(gap)
                if shutdown:
                    self.client.shutdown(socket.SHUT_WR)
            except OSError:
                pass                      # the reader gave up and closed: expected
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def close(self):
        self.server.close()
        self.client.close()


class TestReadRequest(unittest.TestCase):

    def setUp(self):
        self.p = _Pair()

    def tearDown(self):
        self.p.close()

    def read(self, **kw):
        args = dict(FAST)
        args.update(kw)
        return fc.read_request(self.p.server, **args)

    def test_legacy_no_newline_is_complete(self):
        self.p.client.sendall(b"STATUS")
        t0 = time.monotonic()
        req = self.read(chunk_timeout=5.0)
        self.assertLess(time.monotonic() - t0, 1.0)     # no wait for more bytes
        self.assertEqual(req.verb, "STATUS")
        self.assertEqual(req.args, "")
        self.assertTrue(req.legacy)
        self.assertTrue(req.legacy_verb)
        self.assertIsNone(req.body)

    def test_legacy_lower_case_with_args(self):
        self.p.client.sendall(b"freq 433000000")
        req = self.read()
        self.assertEqual(req.verb, "FREQ")
        self.assertEqual(req.tokens, ["433000000"])
        self.assertTrue(req.legacy)

    def test_legacy_verb_with_newline(self):
        self.p.client.sendall(b"INSTANCE 1 FREQ 100\n")
        req = self.read()
        self.assertEqual((req.verb, req.args, req.legacy), ("INSTANCE", "1 FREQ 100", False))

    def test_newline_ends_line(self):
        self.p.client.sendall(b"NODES STATUS FRESH\r\ntrailing junk")
        req = self.read()
        self.assertEqual(req.verb, "NODES")
        self.assertEqual(req.args, "STATUS FRESH")
        self.assertIsNone(req.body)
        self.assertFalse(req.legacy_verb)

    def test_split_segments_new_verb(self):
        self.p.send_later([b"MEM", b"BERS", b"\n"])
        req = self.read()
        self.assertEqual(req.verb, "MEMBERS")

    def test_split_segments_with_body(self):
        body = json.dumps({"node_key": "hosta:1", "x": "y" * 5000}).encode()
        line = "REGISTER @{}\n".format(len(body)).encode()
        data = line + body
        self.p.send_later([data[:3], data[3:20], data[20:4000], data[4000:]])
        req = self.read()
        self.assertEqual(req.verb, "REGISTER")
        self.assertEqual(req.args, "")
        self.assertEqual(req.body, body)
        self.assertEqual(req.body_json()["node_key"], "hosta:1")

    def test_body_extra_bytes_ignored(self):
        self.p.client.sendall(b"SCHEDULE SET range @2\n{}EXTRA")
        req = self.read()
        self.assertEqual(req.args, "SET range")
        self.assertEqual(req.tokens, ["SET", "range"])
        self.assertEqual(req.body, b"{}")
        self.assertEqual(req.body_json(), {})

    def test_eof_terminates_unterminated_line(self):
        self.p.send_later([b"VERSION"], shutdown=True)
        req = self.read(chunk_timeout=5.0)
        self.assertEqual(req.verb, "VERSION")
        self.assertFalse(req.legacy)

    def test_inter_chunk_timeout_terminates_line(self):
        self.p.client.sendall(b"HEALTH")
        t0 = time.monotonic()
        req = self.read(chunk_timeout=0.3)
        dt = time.monotonic() - t0
        self.assertEqual(req.verb, "HEALTH")
        self.assertGreaterEqual(dt, 0.25)
        self.assertLess(dt, 2.5)

    def test_line_total_timeout_bounds_slow_drip(self):
        stop = threading.Event()

        def drip():
            while not stop.is_set():
                try:
                    self.p.client.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.05)
        t = threading.Thread(target=drip, daemon=True)
        self.p.client.sendall(b"PEERS")
        t.start()
        t0 = time.monotonic()
        req = self.read(chunk_timeout=0.3, line_timeout=0.6)
        stop.set()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(req.verb.startswith("PEERSX"))

    def test_empty_and_eof(self):
        self.p.client.shutdown(socket.SHUT_WR)
        self.assertIsNone(self.read())

    def test_blank_line_is_none(self):
        self.p.client.sendall(b"   \n")
        self.assertIsNone(self.read())

    def test_first_read_timeout_is_none(self):
        self.assertIsNone(self.read(first_timeout=0.2))

    def test_line_too_long(self):
        self.p.send_later([b"FOO " + b"a" * 5000] * 3, gap=0.01)
        with self.assertRaises(fc.RequestError) as cm:
            self.read(max_line=8192)
        self.assertEqual(cm.exception.reply,
                         {"ok": False, "error": "line_too_long", "limit": 8192})
        self.assertTrue(cm.exception.newline)

    def test_line_too_long_default_limit_value(self):
        self.assertEqual(fc.MAX_LINE_BYTES, 65536)
        self.assertEqual(fc.MAX_BODY_BYTES, 1048576)
        self.assertEqual(fc.MAX_INLINE_BYTES, 3072)

    def test_body_too_large(self):
        self.p.client.sendall(b"REGISTER @1048577\n")
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.reply,
                         {"ok": False, "error": "body_too_large", "limit": 1048576})

    def test_body_marker_huge_digit_count(self):
        # more digits than CPython's int-string limit: still a RequestError
        self.p.client.sendall(b"HEALTH @" + b"9" * 5000 + b"\n")
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.reply,
                         {"ok": False, "error": "body_too_large", "limit": 1048576})
        self.assertTrue(cm.exception.newline)

    def test_body_marker_leading_zeros(self):
        self.p.client.sendall(b"HEALTH @" + b"0" * 5000 + b"2\n{}")
        req = self.read()
        self.assertEqual(req.body, b"{}")

    def test_body_marker_all_zeros_is_bad_request(self):
        self.p.client.sendall(b"HEALTH @" + b"0" * 5000 + b"\n")
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.code, "bad_request")

    def test_non_ascii_digit_marker_is_an_argument(self):
        self.p.client.sendall("SCHEDULE SET @\u0663\nabc".encode("utf-8"))
        req = self.read()
        self.assertIsNone(req.body)
        self.assertEqual(req.tokens, ["SET", "@\u0663"])

    def test_body_zero_is_bad_request(self):
        self.p.client.sendall(b"REGISTER @0\n")
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.code, "bad_request")

    def test_body_timeout(self):
        self.p.client.sendall(b"REGISTER @100\n{\"a\":")
        t0 = time.monotonic()
        with self.assertRaises(fc.RequestError) as cm:
            self.read(body_timeout=0.4)
        self.assertEqual(cm.exception.reply, {"ok": False, "error": "timeout"})
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_body_truncated_by_eof(self):
        self.p.send_later([b"REGISTER @100\n{}"], shutdown=True)
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("got 2 of 100", cm.exception.reply["message"])

    def test_inline_json_small_ok_large_refused(self):
        doc = json.dumps({"entries": [1, 2, 3]})
        self.p.client.sendall("SCHEDULE SET {}\n".format(doc).encode())
        req = self.read()
        self.assertEqual(req.body_json(), {"entries": [1, 2, 3]})
        self.assertEqual(req.inline_json(), doc)

    def test_inline_json_over_limit(self):
        doc = json.dumps({"x": "y" * 3100})
        self.p.client.sendall("SCHEDULE SET {}\n".format(doc).encode())
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.reply,
                         {"ok": False, "error": "body_too_large_use_@n", "limit": 3072})

    def test_first_chunk_full_without_newline_for_json_verb(self):
        data = b"SCHEDULE SET " + b"{" + b"x" * 5000
        self.p.client.sendall(data)
        time.sleep(0.1)       # let the whole payload queue so recv(4096) fills
        with self.assertRaises(fc.RequestError) as cm:
            self.read()
        self.assertEqual(cm.exception.code, "body_too_large_use_@n")

    def test_bad_inline_json(self):
        self.p.client.sendall(b"SCHEDULE SET {nope\n")
        req = self.read()
        with self.assertRaises(fc.RequestError) as cm:
            req.body_json()
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIsNone(fc.Request("SCHEDULE", "GET", None, False, "SCHEDULE GET").body_json())

    def test_at_token_without_digits_is_an_argument(self):
        self.p.client.sendall(b"MEMBER ADD hosta:1@rx1\n")
        req = self.read()
        self.assertEqual(req.tokens, ["ADD", "hosta:1@rx1"])
        self.assertIsNone(req.body)

    # --- AUTH ---------------------------------------------------------------

    def test_auth_prefix_stripped_without_token(self):
        self.p.client.sendall(b"AUTH whatever HEALTH\n")
        req = self.read()
        self.assertEqual(req.verb, "HEALTH")
        self.assertEqual(req.line, "HEALTH")

    def test_auth_ok(self):
        self.p.client.sendall(b"AUTH s3cret REGISTER @2\n{}")
        req = self.read(auth_token="s3cret")
        self.assertEqual((req.verb, req.body), ("REGISTER", b"{}"))

    def test_auth_legacy_no_newline(self):
        self.p.client.sendall(b"AUTH s3cret STATUS")
        req = self.read(auth_token="s3cret")
        self.assertEqual(req.verb, "STATUS")
        self.assertTrue(req.legacy)

    def test_auth_missing(self):
        self.p.client.sendall(b"STATUS")
        with self.assertRaises(fc.RequestError) as cm:
            self.read(auth_token="s3cret")
        self.assertEqual(cm.exception.reply, {"ok": False, "error": "auth_required"})
        self.assertFalse(cm.exception.newline)     # legacy verb keeps legacy framing

    def test_auth_wrong_checked_before_body(self):
        # the body never arrives: auth is refused without waiting for it
        self.p.client.sendall(b"AUTH nope REGISTER @500\n")
        t0 = time.monotonic()
        with self.assertRaises(fc.RequestError) as cm:
            self.read(auth_token="s3cret", body_timeout=5.0)
        self.assertEqual(cm.exception.code, "auth_required")
        self.assertTrue(cm.exception.newline)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_auth_checked_before_use_at_n(self):
        # a full first chunk of a JSON verb with no newline: auth comes first
        for data in (b"REGISTER " + b"x" * 5000, b"AUTH nope REGISTER " + b"x" * 5000):
            p = _Pair()
            try:
                p.client.sendall(data)
                time.sleep(0.1)
                with self.assertRaises(fc.RequestError) as cm:
                    fc.read_request(p.server, auth_token="s3cret", **FAST)
                self.assertEqual(cm.exception.reply, {"ok": False, "error": "auth_required"})
                self.assertTrue(cm.exception.newline)
            finally:
                p.close()
        self.p.client.sendall(b"AUTH s3cret REGISTER " + b"x" * 5000)
        time.sleep(0.1)
        with self.assertRaises(fc.RequestError) as cm:
            self.read(auth_token="s3cret")
        self.assertEqual(cm.exception.code, "body_too_large_use_@n")

    def test_auth_only_prefix(self):
        self.p.client.sendall(b"AUTH s3cret\n")
        self.assertIsNone(self.read(auth_token="s3cret"))


class TestJsonReplyAndClient(unittest.TestCase):

    def test_json_reply_framing(self):
        self.assertEqual(fc.json_reply({"ok": True, "ts": 1}, False), b'{"ok": true, "ts": 1}')
        self.assertEqual(fc.json_reply({"error": "Unknown command"}, True),
                         b'{"error": "Unknown command"}\n')

    def test_encode_request(self):
        self.assertEqual(fc.encode_request("HEALTH"), b"HEALTH\n")
        self.assertEqual(fc.encode_request("REGISTER", body={"a": 1}, auth_token="t0k"),
                         b'AUTH t0k REGISTER @7\n{"a":1}')
        self.assertEqual(fc.encode_request("SCHEDULE SYNC", body=b"xyz"),
                         b"SCHEDULE SYNC @3\nxyz")
        with self.assertRaises(ValueError):
            fc.encode_request("A\nB")
        with self.assertRaises(ValueError):
            fc.encode_request("PING", auth_token="bad token")
        with self.assertRaises(ValueError):
            fc.encode_request("X", body=b"")

    def test_request_json_round_trip_with_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        seen = []

        def serve():
            for _ in range(2):
                conn, _addr = srv.accept()
                try:
                    try:
                        req = fc.read_request(conn, auth_token="tok", **FAST)
                        seen.append(req)
                        reply = {"ok": True, "verb": req.verb, "body": req.body_json()}
                        conn.sendall(fc.json_reply(reply, not req.legacy_verb))
                    except fc.RequestError as e:
                        conn.sendall(fc.json_reply(e.reply, e.newline))
                finally:
                    conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            r = fc.request_json("127.0.0.1", port, "REGISTER", body={"k": "v"},
                                auth_token="tok", timeout=5.0)
            self.assertEqual(r, {"ok": True, "verb": "REGISTER", "body": {"k": "v"}})
            r = fc.request_json("127.0.0.1", port, "HEALTH", timeout=5.0)
            self.assertEqual(r, {"ok": False, "error": "auth_required"})
        finally:
            t.join(5)
            srv.close()


# ---------------------------------------------------------------------------
# fan_out (D.3.2)
# ---------------------------------------------------------------------------

class TestFanOut(unittest.TestCase):

    def setUp(self):
        self.reg = fc.FanOutRegistry()

    def test_results_in_parallel_and_in_order(self):
        def worker(target, ctx):
            time.sleep(0.2)
            return {"ok": True, "t": target}
        targets = ["k{}".format(i) for i in range(8)]
        t0 = time.monotonic()
        res = fc.fan_out(targets, worker, max_workers=8, deadline_sec=5.0, registry=self.reg)
        self.assertLess(time.monotonic() - t0, 1.5)        # parallel, not 1.6 s
        self.assertEqual(list(res), targets)
        self.assertEqual(res["k3"], {"ok": True, "t": "k3"})

    def test_dict_targets_keyed_by_node_key(self):
        targets = fc.parse_member_list("hosta:0,hostb:1")
        res = fc.fan_out(targets, lambda t, ctx: {"ok": True, "iid": t["instance_id"]},
                         max_workers=4, deadline_sec=2.0, registry=self.reg)
        self.assertEqual(res, {"hosta:0": {"ok": True, "iid": 0},
                               "hostb:1": {"ok": True, "iid": 1}})

    def test_custom_key_and_duplicates(self):
        res = fc.fan_out([{"id": 1}], lambda t, c: {"ok": True}, key=lambda t: t["id"],
                         max_workers=1, deadline_sec=1.0, registry=self.reg)
        self.assertEqual(res, {1: {"ok": True}})
        with self.assertRaises(ValueError):
            fc.fan_out(["a", "a"], lambda t, c: {}, max_workers=2, deadline_sec=1.0,
                       registry=self.reg)

    def test_empty_targets(self):
        self.assertEqual(fc.fan_out([], lambda t, c: {}, max_workers=4, deadline_sec=1.0,
                                    registry=self.reg), {})

    def test_deadline_stragglers_phase_and_applied(self):
        release = threading.Event()

        def worker(target, ctx):
            ctx.phase = {"fast": "command", "conn": "connect", "xack": "xack",
                         "cmd": "command", "verify": "verify", "chunks": "chunks"}[target]
            if target == "fast":
                return {"ok": True, "applied": True}
            release.wait(10)
            return {"ok": True}
        targets = ["fast", "conn", "xack", "chunks", "cmd", "verify"]
        t0 = time.monotonic()
        res = fc.fan_out(targets, worker, max_workers=8, deadline_sec=0.4, registry=self.reg)
        dt = time.monotonic() - t0
        try:
            self.assertLess(dt, 2.0)
            self.assertEqual(res["fast"], {"ok": True, "applied": True})
            for k in ("conn", "xack", "chunks"):
                self.assertEqual(res[k], {"ok": False, "applied": False, "error": "timeout",
                                          "phase": "connect" if k == "conn" else k})
            for k in ("cmd", "verify"):
                self.assertEqual(res[k], {"ok": False, "applied": None, "error": "timeout",
                                          "phase": "command" if k == "cmd" else k})
            self.assertEqual(self.reg.abandoned_workers(), 5)
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while self.reg.abandoned_workers() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.reg.abandoned_workers(), 0)

    def test_cancel_event_set_at_deadline_and_checked_before_send(self):
        sent = []
        cancel = threading.Event()
        done = threading.Event()
        outcome = {}

        def worker(target, ctx):
            ctx.phase = "xack"
            time.sleep(0.6)                     # XACK took longer than the deadline
            r = ctx.check_before_send()
            outcome[target] = r
            if r is None:
                sent.append(target)
            done.set()
            return r or {"ok": True}
        res = fc.fan_out(["m1"], worker, max_workers=2, deadline_sec=0.2,
                         cancel_event=cancel, registry=self.reg)
        self.assertTrue(cancel.is_set())
        self.assertEqual(res["m1"]["error"], "timeout")
        self.assertFalse(res["m1"]["applied"])
        self.assertTrue(done.wait(5))
        self.assertEqual(sent, [])
        self.assertEqual(outcome["m1"], {"ok": False, "applied": False, "error": "cancelled",
                                         "phase": "xack"})

    def test_deadline_between_passing_check_and_send_reports_possibly_sent(self):
        sent = []
        done = threading.Event()

        def worker(target, ctx):
            ctx.phase = "xack"
            r = ctx.check_before_send()
            if r is None:
                time.sleep(0.6)                 # the deadline falls here, before the send
                sent.append(target)             # the frame goes out anyway
            done.set()
            return r or {"ok": True}
        res = fc.fan_out(["m1"], worker, max_workers=1, deadline_sec=0.2, registry=self.reg)
        self.assertTrue(done.wait(5))
        self.assertEqual(sent, ["m1"])
        self.assertEqual(res["m1"], {"ok": False, "applied": None, "error": "timeout",
                                     "phase": "command"})

    def test_deadline_during_check_never_sends_and_is_conservative(self):
        sent = []
        done = threading.Event()
        cancel = threading.Event()
        outcome = {}

        def slow_guard():
            cancel.wait(5)                      # the deadline falls inside the check
            return True

        def worker(target, ctx):
            ctx.phase = "xack"
            r = ctx.check_before_send()
            outcome["r"], outcome["phase"] = r, ctx.phase
            if r is None:
                sent.append(target)
            done.set()
            return r or {"ok": True}
        res = fc.fan_out(["m1"], worker, max_workers=1, deadline_sec=0.2, cancel_event=cancel,
                         still_valid=slow_guard, registry=self.reg)
        self.assertTrue(done.wait(5))
        self.assertEqual(sent, [])
        self.assertEqual(outcome["r"], {"ok": False, "applied": False, "error": "cancelled",
                                        "phase": "xack"})
        self.assertEqual(outcome["phase"], "xack")          # restored after the failed check
        self.assertEqual(res["m1"], {"ok": False, "applied": None, "error": "timeout",
                                     "phase": "command"})

    def test_check_before_send_phase_bookkeeping(self):
        cancel = threading.Event()
        ctx = fc.FanOutContext("k", "k", time.monotonic() + 5.0, cancel)
        ctx.phase = "chunks"
        self.assertIsNone(ctx.check_before_send(0.5))
        self.assertEqual(ctx.phase, "command")
        ctx.phase = "xack"
        late = ctx.check_before_send(60.0)
        self.assertEqual(late["reason"], "rto_exceeds_deadline")
        self.assertFalse(late["applied"])
        self.assertEqual(ctx.phase, "xack")
        ctx.phase = "verify"                    # a second frame after one was already sent
        cancel.set()
        self.assertEqual(ctx.check_before_send(), {"ok": False, "applied": None,
                                                   "error": "cancelled", "phase": "verify"})
        self.assertEqual(ctx.phase, "verify")
        with self.assertRaises(ValueError):
            ctx.check_before_send(next_phase="xack")
        self.assertEqual(ctx.phase, "verify")

    def test_no_cancel_when_all_finish(self):
        cancel = threading.Event()
        fc.fan_out(["a"], lambda t, c: {"ok": True}, max_workers=1, deadline_sec=2.0,
                   cancel_event=cancel, registry=self.reg)
        self.assertFalse(cancel.is_set())

    def test_shutdown_does_not_wait_for_stuck_worker(self):
        stuck = threading.Event()

        def worker(target, ctx):
            if target == "stuck":
                stuck.wait(30)
            return {"ok": True}
        t0 = time.monotonic()
        res = fc.fan_out(["ok", "stuck"], worker, max_workers=2, deadline_sec=0.3,
                         registry=self.reg)
        try:
            self.assertLess(time.monotonic() - t0, 1.5)
            self.assertEqual(res["ok"], {"ok": True})
            self.assertEqual(res["stuck"]["error"], "timeout")
            self.assertEqual(self.reg.abandoned_workers(), 1)
        finally:
            stuck.set()

    def test_queued_workers_cancelled_not_abandoned(self):
        block = threading.Event()
        started = []

        def worker(target, ctx):
            started.append(target)
            block.wait(10)
            return {"ok": True}
        res = fc.fan_out(["a", "b", "c"], worker, max_workers=1, deadline_sec=0.3,
                         registry=self.reg)
        try:
            self.assertEqual(started, ["a"])
            self.assertEqual(self.reg.abandoned_workers(), 1)   # only the running one
            for k in ("b", "c"):
                self.assertEqual(res[k], {"ok": False, "applied": False, "error": "timeout",
                                          "phase": "connect"})
        finally:
            block.set()
        time.sleep(0.3)
        self.assertEqual(started, ["a"])                        # never started later

    def test_busy_when_abandoned_exceeds_limit(self):
        block = threading.Event()
        try:
            fc.fan_out(["a", "b", "c"], lambda t, c: block.wait(10), max_workers=3,
                       deadline_sec=0.2, registry=self.reg)
            self.assertEqual(self.reg.abandoned_workers(), 3)
            with self.assertRaises(fc.FanOutBusy) as cm:
                fc.fan_out(["x"], lambda t, c: {}, max_workers=1, deadline_sec=1.0,
                           registry=self.reg)
            self.assertEqual(cm.exception.reply,
                             {"ok": False, "error": "busy", "retry_in_sec": 5})
            # with a larger pool the gauge (3) is within 2 x 2
            fc.fan_out(["x"], lambda t, c: {"ok": True}, max_workers=2, deadline_sec=1.0,
                       registry=self.reg)
        finally:
            block.set()

    def test_worker_exception(self):
        def worker(target, ctx):
            ctx.phase = "command"
            raise RuntimeError("boom")
        with self.assertLogs("federation_common", level="ERROR"):
            res = fc.fan_out(["a"], worker, max_workers=1, deadline_sec=1.0, registry=self.reg)
        self.assertEqual(res["a"], {"ok": False, "applied": None, "error": "internal_error",
                                    "message": "boom", "phase": "command"})

    def test_registry_registration_and_cancel_all(self):
        seen = {}
        gate = threading.Event()

        def worker(target, ctx):
            seen["running"] = self.reg.running()
            gate.wait(5)
            return ctx.check_before_send(phase="xack") or {"ok": True}
        t = threading.Thread(target=lambda: seen.setdefault(
            "res", fc.fan_out(["a"], worker, max_workers=1, deadline_sec=5.0,
                              registry=self.reg)))
        t.start()
        deadline = time.monotonic() + 3
        while "running" not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen["running"], 1)
        self.assertEqual(self.reg.cancel_all(), 1)     # leadership change
        gate.set()
        t.join(5)
        self.assertEqual(seen["res"]["a"]["error"], "cancelled")
        self.assertEqual(self.reg.running(), 0)

    def test_still_valid_guard(self):
        state = {"leader": True}

        def worker(target, ctx):
            ctx.phase = "xack"
            state["leader"] = False                   # stepped down during XACK
            return ctx.check_before_send(1.0) or {"ok": True}
        res = fc.fan_out(["a"], worker, max_workers=1, deadline_sec=5.0,
                         still_valid=lambda: state["leader"], registry=self.reg)
        self.assertEqual(res["a"], {"ok": False, "applied": False, "error": "leadership_lost",
                                    "phase": "xack"})

    def test_rto_exceeds_deadline(self):
        def worker(target, ctx):
            return ctx.check_before_send(t_cmd=26.0) or {"ok": True}
        res = fc.fan_out(["a"], worker, max_workers=1, deadline_sec=2.0, registry=self.reg)
        r = res["a"]
        self.assertEqual(r["reason"], "rto_exceeds_deadline")
        self.assertFalse(r["applied"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["rto_ms"], 26000)
        self.assertTrue(0 < r["remaining_ms"] <= 2000)

    def test_context_timeouts(self):
        ctx = fc.FanOutContext("k", "k", time.monotonic() + 1.0, threading.Event())
        self.assertEqual(ctx.phase, "connect")
        self.assertTrue(ctx.still_valid())
        self.assertLessEqual(ctx.read_timeout(26.0), 1.0)
        self.assertEqual(ctx.read_timeout(0.5), 0.5)
        late = fc.FanOutContext("k", "k", time.monotonic() - 1.0, threading.Event())
        self.assertEqual(late.read_timeout(5.0), 0.001)
        self.assertIsNone(ctx.check_before_send(0.5))

    def test_module_level_registry_functions(self):
        ev = threading.Event()
        before = fc.DEFAULT_REGISTRY.running()
        fc.register_fanout(ev)
        try:
            self.assertEqual(fc.DEFAULT_REGISTRY.running(), before + 1)
            self.assertGreaterEqual(fc.cancel_all_fanouts(), 1)
            self.assertTrue(ev.is_set())
        finally:
            fc.unregister_fanout(ev)
        self.assertEqual(fc.DEFAULT_REGISTRY.running(), before)
        self.assertIsInstance(fc.abandoned_workers(), int)


class TestStdlibOnly(unittest.TestCase):

    def test_imports_stdlib_only(self):
        import ast
        src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '_daq_core')
        stdlib = {"collections", "hmac", "ipaddress", "json", "logging", "re", "socket",
                  "threading", "time", "concurrent", "os", "tempfile"}
        for name in ("federation_common.py", "hwc_relay.py", "version.py"):
            with open(os.path.join(src_dir, name)) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [(node.module or "").split(".")[0]]
                else:
                    continue
                for m in mods:
                    self.assertIn(m, stdlib, "{} imports {}".format(name, m))

    def test_version(self):
        import version
        self.assertEqual(version.FW_VERSION, "1.1.0-dev")


if __name__ == "__main__":
    unittest.main()
