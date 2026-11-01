"""
    Coordinator election tests (design spec C.4)

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Part 1 drives the pure ``Election`` state machine with a simulated clock
    and a directed reachability matrix (``can[a][b]``: a can open a
    connection to b and read its reply). Part 2 runs three real coordinators
    on ephemeral ports with ``election_interval_sec = 0.1``.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "_daq_core"))
sys.path.insert(0, HERE)

from federation_election import (Election, ElectionRunner, MemoryTermStore,  # noqa: E402
                                  TermStore, TERM_MAX, settle_polls_min,
                                  validate_election_settings)
from federation_coordinator import CoordinatorSettings, FederationCoordinator  # noqa: E402
from federation_scheduler import FederationScheduler  # noqa: E402
from test_federation_protocol import (FakeHWCServer, FakeStatusServer, member,  # noqa: E402
                                      rj, wait_for, raw_request, _json_reply)


class SimClock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SimNet(object):
    """n candidates c0..c{n-1}; ticks every I with per-node phase offsets."""

    def __init__(self, n=3, **kw):
        self.clock = SimClock()
        self.kw = dict(interval=2.0, missed_polls=3, settle_polls=5, clock_margin=0.05,
                       failback_sec=60.0, require_quorum=False)
        self.kw.update(kw)
        self.eps = ["c{}:{}".format(i, 6000 + i) for i in range(n)]
        self.can = {a: {b: True for b in self.eps} for a in self.eps}
        self.alive = {ep: True for ep in self.eps}
        self.stores = {ep: MemoryTermStore() for ep in self.eps}
        self.nodes = {}
        self.next_tick = {}
        self.history = []          # (t, {ep: (role, term)})
        for i, ep in enumerate(self.eps):
            self._boot(ep, offset=i * self.kw["interval"] / 3.0)

    def _boot(self, ep, offset=0.0):
        self.nodes[ep] = Election(self.eps, ep, store=self.stores[ep], clock=self.clock,
                                  **self.kw)
        self.alive[ep] = True
        self.next_tick[ep] = self.clock.t + offset

    def restart(self, ep):
        self._boot(ep)

    def kill(self, ep):
        self.alive[ep] = False

    def partition(self, a, b):
        self.can[a][b] = False

    def heal(self):
        for a in self.eps:
            for b in self.eps:
                self.can[a][b] = True

    def _dispatch(self, target, line):
        e = self.nodes[target]
        toks = line.split()
        verb = toks[0]
        if verb == "LEADER":
            return e.handle_leader(toks[1] if len(toks) > 1 else None)
        if verb == "PREVOTE":
            return e.handle_prevote(int(toks[1]), toks[2], toks[3] if len(toks) > 3 else None)
        if verb == "LEASE":
            return e.handle_lease(int(toks[1]), toks[2], toks[3] if len(toks) > 3 else None)
        raise AssertionError(line)

    def rpc_for(self, src):
        def rpc(requests):
            out = {}
            for ep, line in requests:
                if self.alive[src] and self.alive[ep] and self.can[src][ep]:
                    out[ep] = self._dispatch(ep, line)
                else:
                    out[ep] = None
            return out
        return rpc

    def run(self, seconds, step=None, check=None):
        step = step or self.kw["interval"] / 10.0
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            for ep in self.eps:
                if self.alive[ep] and self.clock.t >= self.next_tick[ep]:
                    self.nodes[ep].tick(self.rpc_for(ep))
                    self.next_tick[ep] += self.kw["interval"]
            snap = {ep: self.nodes[ep].role_term() for ep in self.eps if self.alive[ep]}
            self.history.append((self.clock.t, snap))
            if check is not None:
                check(self)

    def leaders(self):
        return [ep for ep in self.eps if self.alive[ep] and self.nodes[ep].role == "leader"]

    def majority_leaders(self):
        now = self.clock.t
        return [ep for ep in self.leaders() if self.nodes[ep]._quorum(now)]


def at_most_one_majority(net):
    assert len(net.majority_leaders()) <= 1, (net.clock.t, net.majority_leaders())


class TestElectionMachine(unittest.TestCase):

    def test_settle_validation(self):
        self.assertEqual(settle_polls_min(2.0, 3, 0.05), 5)
        self.assertEqual(settle_polls_min(0.1, 3, 0.05), 5)
        with self.assertRaises(ValueError):
            validate_election_settings(["a:1", "b:2"], 2.0, 3, 4, 0.05)
        with self.assertRaises(ValueError):
            Election(["a:1", "b:2"], "a:1", settle_polls=4, store=MemoryTermStore())
        warnings = validate_election_settings(["a:1", "b:2"], require_quorum=True)
        self.assertIn("with 2 candidates, losing either host blocks all mutations", warnings)

    def test_unique_leader_and_monotonic_terms(self):
        for quorum in (False, True):
            net = SimNet(3, require_quorum=quorum)
            net.run(60, check=at_most_one_majority)
            self.assertEqual(len(net.leaders()), 1, quorum)
            self.assertEqual(net.leaders(), ["c0:6000"])
            last = {}
            for _t, snap in net.history:
                self.assertLessEqual(sum(1 for r, _ in snap.values() if r == "leader"), 1)
                for ep, (_role, term) in snap.items():
                    self.assertGreaterEqual(term, last.get(ep, 0))
                    last[ep] = term

    def test_settle_period_no_claim_no_grant(self):
        net = SimNet(3)
        G = 5 * 2.0
        net.run(G - 0.5)
        self.assertEqual(net.leaders(), [])
        e = net.nodes["c1:6001"]
        r = e.handle_prevote(5, "c2:6002")
        self.assertEqual((r["granted"], r["reason"]), (False, "settling"))
        r = e.handle_lease(5, "c2:6002")
        self.assertEqual((r["granted"], r["reason"]), (False, "settling"))
        self.assertEqual(e.term, 0)                 # a higher term is not adopted either
        self.assertFalse(e.handle_leader()["eligible"])
        net.run(20)
        self.assertEqual(net.leaders(), ["c0:6000"])

    def test_follower_restart_within_lease(self):
        net = SimNet(3, require_quorum=True)
        net.run(30, check=at_most_one_majority)
        self.assertEqual(net.leaders(), ["c0:6000"])
        a, b, c = net.eps
        # B loses A in both directions; C restarts while A counts C's grant
        net.partition(b, a)
        net.partition(a, b)
        net.restart(c)
        settling = []

        def check(n):
            at_most_one_majority(n)
            if n.clock.t - n.nodes[c].start_mono < n.nodes[c].G:
                r = n.nodes[c].handle_prevote(n.nodes[b].term + 1, b)
                settling.append(r["reason"] == "settling" and not r["granted"])
        net.run(60, check=check)
        self.assertTrue(settling and all(settling))
        self.assertNotIn(b, net.majority_leaders())

    def test_grants_count_only_for_shortened_lease(self):
        clock = SimClock()
        e = Election(["a:1", "b:2", "c:3"], "a:1", store=MemoryTermStore(), clock=clock)
        e.role = "leader"
        e.grants = {"b:2": clock.t}
        L = e.L
        self.assertTrue(e._quorum(clock.t + L * 0.95 - 0.01))
        self.assertFalse(e._quorum(clock.t + L * 0.95 + 0.01))

    def test_stickiness(self):
        clock = SimClock()
        e = Election(["a:1", "b:2", "c:3"], "c:3", store=MemoryTermStore(), clock=clock)
        clock.t += e.G + 1
        self.assertTrue(e.handle_lease(1, "a:1")["granted"])
        r = e.handle_lease(2, "b:2")
        self.assertFalse(r["granted"])
        self.assertEqual(e.term, 1)                  # not adopted
        self.assertEqual(r["lease_holder"], "a:1")
        self.assertFalse(e.handle_prevote(2, "b:2")["granted"])
        clock.t += e.L + 0.01
        self.assertTrue(e.handle_lease(2, "b:2")["granted"])
        self.assertEqual(e.term, 2)

    def test_asymmetric_partition_keeps_a(self):
        for quorum in (False, True):
            net = SimNet(3, require_quorum=quorum)
            net.run(30)
            a, b, c = net.eps
            terms = {ep: net.nodes[ep].term for ep in net.eps}
            net.partition(b, a)
            net.partition(c, a)
            net.run(120, check=at_most_one_majority)
            self.assertEqual(net.leaders(), [a])
            self.assertEqual(net.nodes[b].term, terms[b])
            self.assertEqual(net.nodes[c].term, terms[c])

    def test_reverse_partition(self):
        net = SimNet(3, require_quorum=True)
        net.run(30)
        a, b, c = net.eps
        net.partition(a, b)
        net.partition(a, c)
        net.run(60, check=at_most_one_majority)
        self.assertEqual(net.nodes[a].role, "follower")
        self.assertEqual(net.leaders(), [b])
        net2 = SimNet(3, require_quorum=False)
        net2.run(30)
        a, b, c = net2.eps
        net2.partition(a, b)
        net2.partition(a, c)
        net2.run(60)
        self.assertEqual(net2.leaders(), [a])

    def test_no_quorum_with_two_of_three_down(self):
        net = SimNet(3, require_quorum=True)
        net.run(30)
        a, b, c = net.eps
        net.kill(b)
        net.kill(c)
        e = net.nodes[a]
        net.run(e.L * 0.95 + e.I)
        self.assertFalse(e.has_quorum())
        net.run(e.L + e.I)
        self.assertEqual(e.role, "follower")
        self.assertEqual(e.reason, "no_quorum")
        net.run(60)
        self.assertEqual(net.leaders(), [])

    def test_failback_only_after_continuous_eligibility(self):
        F = 20.0
        net = SimNet(3, failback_sec=F)
        net.run(30)
        a, b, c = net.eps
        net.kill(a)
        net.run(20)
        self.assertEqual(net.leaders(), [b])
        net.restart(a)
        restart_t = net.clock.t
        handover = []

        def check(n):
            if not handover and n.nodes[b].role == "follower":
                handover.append(n.clock.t)
        net.run(80, check=check)
        self.assertTrue(handover)
        self.assertGreaterEqual(handover[0] - restart_t, net.nodes[a].G + F - 0.01)
        self.assertEqual(net.nodes[b].reason, "handover")
        self.assertEqual(net.leaders(), [a])

    def test_flapping_rank0_bounded_churn(self):
        F = 20.0
        net = SimNet(3, failback_sec=F)
        net.run(30)
        a = net.eps[0]
        changes = []
        last = [tuple(net.leaders())]

        def check(n):
            cur = tuple(n.leaders())
            if cur and cur != last[0]:
                changes.append((n.clock.t, cur))
                last[0] = cur
        start = net.clock.t
        while net.clock.t - start < 180.0:     # rank 0: down 10 s, up 14 s, repeatedly
            net.kill(a)
            net.run(10.0, check=check)
            net.restart(a)
            net.run(14.0, check=check)
        bound = (net.clock.t - start) / (F + 5 * 2.0) + 1
        self.assertLessEqual(len(changes), bound, changes)
        self.assertEqual(changes[0][1], ("c1:6001",))

    def test_unwritable_state_dir_ineligible(self):
        net = SimNet(3)
        for ep in net.eps:
            net.stores[ep].fail = True
        net.restart(net.eps[0])
        net.stores[net.eps[0]].fail = True
        net.run(60)
        e = net.nodes[net.eps[0]]
        self.assertNotEqual(e.role, "leader")
        net2_e = Election(["a:1", "b:2"], "a:1", store=MemoryTermStore(), clock=SimClock())
        net2_e.store.fail = True
        net2_e.clock.t += net2_e.G + 1
        r = net2_e.handle_lease(3, "b:2")
        self.assertFalse(r["granted"])
        self.assertEqual(net2_e.handle_leader()["reason"], "state_dir_unwritable")
        self.assertFalse(net2_e.handle_leader()["eligible"])
        self.assertFalse(net2_e.handle_prevote(9, "b:2")["granted"])

    def test_term_store_atomic_and_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            store = TermStore(d)
            self.assertEqual(store.load(), (0, None))
            store.save(7, "a:1")
            self.assertEqual(store.load(), (7, "a:1"))
            self.assertEqual(sorted(os.listdir(d)), ["term"])
            with open(os.path.join(d, "term"), "w") as f:
                f.write("garbage")
            e = Election(["a:1", "b:2"], "a:1", store=TermStore(d))
            self.assertEqual(e.eligible(), (False, "state_dir_unwritable"))

    def test_term_saturation(self):
        clock = SimClock()
        e = Election(["a:1", "b:2"], "a:1", store=MemoryTermStore(term=TERM_MAX),
                     clock=clock)
        clock.t += 60
        with self.assertLogs("federation_election", level="CRITICAL"):
            e.tick(lambda reqs: {ep: None for ep, _ in reqs})
        self.assertEqual(e.role, "follower")
        self.assertEqual(e.term, TERM_MAX)
        self.assertEqual(e.eligible(), (False, "term_exhausted"))

    def test_out_of_range_term_never_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            clock = SimClock()
            e = Election(["a:1", "b:2"], "a:1", store=TermStore(d), clock=clock)
            clock.t += e.G + 1
            r = e.handle_lease(2 ** 40, "b:2")
            self.assertFalse(r["granted"])
            self.assertEqual(r["term"], 0)
            self.assertFalse(e.observe_term(2 ** 32, "XACK"))
            self.assertEqual(TermStore(d).load(), (0, None))
            e2 = Election(["a:1", "b:2"], "a:1", store=TermStore(d), clock=clock)
            self.assertFalse(e2.state_error)               # the term file still loads
            self.assertNotEqual(e2.eligible(), (False, "state_dir_unwritable"))
            self.assertTrue(e2.observe_term(0xFFFFFFFF, "XACK"))   # u32 max is a valid term

    def test_cluster_mismatch(self):
        e = Election(["a:1", "b:2"], "a:1", store=MemoryTermStore(), cluster_id="lab-a")
        self.assertEqual(e.handle_leader("lab-b"), {"ok": False, "error": "cluster_mismatch",
                                                    "expected": "lab-a"})
        self.assertEqual(e.handle_prevote(1, "b:2", "lab-b")["error"], "cluster_mismatch")

    def test_ticks_never_overlap(self):
        e = Election(["a:1", "b:2"], "a:1", interval=0.1, store=MemoryTermStore())

        def slow(host, port, line, **kw):
            time.sleep(0.3)
            raise socket.timeout("slow peer")
        runner = ElectionRunner(e, peer_timeout=2.0, request=slow)
        runner.start()
        time.sleep(1.5)
        runner.close()
        spans = list(runner.tick_starts)
        self.assertGreater(len(spans), 3)
        for (s0, e0), (s1, _e1) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s1, e0 - 1e-6)
        for s0, e0 in spans:
            self.assertLess(e0 - s0, 3 * (runner.t_el + 0.05) + 0.1)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Cluster(object):
    """Three real coordinators on ephemeral ports."""

    def __init__(self, instances=(), token=None, quorum=False, extra=None):
        self.ports = [_free_port() for _ in range(3)]
        self.eps = ["127.0.0.1:{}".format(p) for p in self.ports]
        self.dirs = [tempfile.mkdtemp(prefix="coordstate") for _ in range(3)]
        self.instances = list(instances)
        self.token = token
        self.quorum = quorum
        self.extra = dict(extra or {})
        self.coords = [None, None, None]
        for i in range(3):
            self.start(i)

    def settings(self, i):
        kw = dict(coordinator_candidates=",".join(self.eps), coordinator_self=self.eps[i],
                  state_dir=self.dirs[i], election_interval_sec=0.1, election_missed_polls=3,
                  election_settle_polls=5, election_failback_sec=60.0, peer_timeout=0.5,
                  require_quorum=self.quorum, xack_timeout_sec=0.5,
                  hwc_request_timeout_sec=0.5, peer_connect_timeout_sec=0.5,
                  status_timeout_sec=0.5, mutation_wait_sec=0.3, max_member_rto_sec=5.0,
                  health_poll_interval=0.2, sync_push_timeout_sec=1.0,
                  auth_token=self.token or "")
        kw.update(self.extra)
        return CoordinatorSettings(**kw)

    def start(self, i):
        c = FederationCoordinator(port=self.ports[i], instances=list(self.instances),
                                  listen_address="127.0.0.1", settings=self.settings(i))
        c.scheduler = FederationScheduler(coordinator=c)
        c.start()
        self.coords[i] = c
        return c

    def stop(self, i):
        if self.coords[i] is not None:
            self.coords[i].close()
            self.coords[i] = None

    def close(self):
        for i in range(3):
            self.stop(i)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def leaders(self):
        return [i for i, c in enumerate(self.coords)
                if c is not None and c._role_term()[0] == "leader"]

    def wait_leader(self, timeout=3.0):
        ok = wait_for(lambda: len(self.leaders()) == 1, timeout)
        return self.leaders()[0] if ok else None


class TestRealCoordinators(unittest.TestCase):

    def setUp(self):
        self._cleanup = []

    def tearDown(self):
        for fn in reversed(self._cleanup):
            try:
                fn()
            except Exception:
                pass

    def cluster(self, **kw):
        cl = Cluster(**kw)
        self._cleanup.append(cl.close)
        return cl

    def test_failover_replication_and_redirect(self):
        cl = self.cluster()
        t0 = time.monotonic()
        lead = cl.wait_leader(3.0 + 1.0)
        self.assertIsNotNone(lead, "no leader within 3 s")
        self.assertLess(time.monotonic() - t0, 4.0)
        replies = [rj(cl.ports[i], "LEADER") for i in range(3)]
        endpoints = {(r["leader"] or {}).get("endpoint") for r in replies}
        self.assertTrue(wait_for(lambda: len({(rj(cl.ports[i], "LEADER")["leader"] or {})
                                              .get("endpoint") for i in range(3)}) == 1, 3.0),
                        endpoints)
        sched = {"name": "s", "entries": [{"frequency": 433000000}, {"frequency": 868000000}]}
        out = rj(cl.ports[lead], "SCHEDULE SET", body=sched)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["replicated"]["stored"], 3)
        self.assertEqual(out["replicated"]["of"], 3)
        self.assertTrue(out["durable"])
        follower = (lead + 1) % 3
        redirect = rj(cl.ports[follower], "SCHEDULE SET", body=sched)
        self.assertEqual(redirect["error"], "not_leader")
        self.assertEqual(redirect["leader"]["endpoint"], cl.eps[lead])
        self.assertEqual(set(redirect["leader"]), {"endpoint", "term"})
        legacy = json.loads(raw_request(cl.ports[follower], b"FREQ 1").decode())
        self.assertEqual(legacy["error"], "not_leader")
        got = rj(cl.ports[follower], "SCHEDULE GET")
        self.assertEqual(got["from"], "follower")
        old_term = cl.coords[lead]._role_term()[1]
        cl.stop(lead)
        t1 = time.monotonic()
        self.assertTrue(wait_for(lambda: len(cl.leaders()) == 1, 3.0), "no failover in 3 s")
        self.assertLess(time.monotonic() - t1, 3.0)
        new = cl.leaders()[0]
        self.assertGreater(cl.coords[new]._role_term()[1], old_term)
        got = rj(cl.ports[new], "SCHEDULE GET")
        self.assertEqual([e["frequency"] for e in got["schedule"]["entries"]],
                         [433000000, 868000000])
        # term persisted across a restart
        cl.stop(new)
        with open(os.path.join(cl.dirs[new], "term")) as f:
            persisted = json.load(f)["term"]
        self.assertGreaterEqual(persisted, old_term + 1)
        c = cl.start(new)
        self.assertGreaterEqual(c._role_term()[1], persisted)

    def test_dropped_push_with_quorum(self):
        cl = self.cluster(quorum=True)
        lead = cl.wait_leader(4.0)
        self.assertIsNotNone(lead)
        sched = {"name": "s", "entries": [{"frequency": 433000000}]}
        self.assertEqual(rj(cl.ports[lead], "SCHEDULE SET", body=sched)["replicated"]["stored"], 3)
        version = cl.coords[lead]._version
        cl.coords[lead]._push_hook = lambda ep: True
        # the followers must not get the 'lost' version by pull repair either
        # (C.4.7: it stays committed on the leader and pull repair would fetch it)
        for i in range(3):
            if i != lead:
                cl.coords[i]._maybe_pull_repair = lambda *a: None
        out = rj(cl.ports[lead], "SCHEDULE SET",
                 body={"name": "lost", "entries": [{"frequency": 915000000}]})
        self.assertEqual(out["error"], "not_replicated")
        self.assertEqual(out["replicated"]["stored"], 1)
        self.assertEqual(out["version"], version + 1)
        cl.stop(lead)
        self.assertTrue(wait_for(lambda: len(cl.leaders()) == 1, 3.0))
        new = cl.leaders()[0]
        got = rj(cl.ports[new], "SCHEDULE GET")
        self.assertEqual(got["schedule"]["name"], "s")
        self.assertEqual(got["version"], version)

    def _count_pulls(self, cl):
        counts = [0, 0, 0]
        for i in range(3):
            c = cl.coords[i]
            if c is None:
                continue
            real = c._pull_repair

            def counted(*a, _i=i, _real=real):
                counts[_i] += 1
                return _real(*a)
            c._pull_repair = counted
        return counts

    def test_settled_cluster_does_no_pull_repair(self):
        # the leader's election term (1 or more) is above the term of the version it
        # holds (0 until the first commit): LEADER's schedule_term, not term, is compared
        cl = self.cluster()
        lead = cl.wait_leader(4.0)
        self.assertIsNotNone(lead)
        reply = rj(cl.ports[lead], "LEADER")
        self.assertGreaterEqual(reply["term"], 1)
        self.assertEqual((reply["schedule_term"], reply["schedule_version"]), (0, 0))
        counts = self._count_pulls(cl)
        time.sleep(1.5)                      # 15 election ticks
        self.assertEqual(counts, [0, 0, 0])
        # after a commit, failover and a new leader at a higher term: still none
        rj(cl.ports[lead], "SCHEDULE SET", body={"name": "s", "entries": [{"frequency": 1}]})
        held = (cl.coords[lead]._sync_term, cl.coords[lead]._version)
        cl.stop(lead)
        self.assertTrue(wait_for(lambda: len(cl.leaders()) == 1, 3.0))
        new = cl.leaders()[0]
        self.assertGreater(cl.coords[new]._role_term()[1], held[0])
        counts = self._count_pulls(cl)
        time.sleep(1.5)
        self.assertEqual(counts[new], 0)
        self.assertEqual(sum(counts), 0, counts)
        for c in cl.coords:
            if c is not None:
                self.assertEqual((c._sync_term, c._version), held)

    def test_dropped_push_is_pull_repaired_once(self):
        cl = self.cluster()
        lead = cl.wait_leader(4.0)
        self.assertIsNotNone(lead)
        cl.coords[lead]._push_hook = lambda ep: True
        counts = self._count_pulls(cl)
        out = rj(cl.ports[lead], "SCHEDULE SET",
                 body={"name": "x", "entries": [{"frequency": 915000000}]})
        self.assertEqual(out["replicated"]["stored"], 1)
        held = (cl.coords[lead]._sync_term, cl.coords[lead]._version)
        self.assertTrue(wait_for(lambda: all((c._sync_term, c._version) == held
                                             for c in cl.coords), 3.0))
        followers = [i for i in range(3) if i != lead]
        self.assertTrue(all(counts[i] >= 1 for i in followers), counts)
        # once repaired, the pulls stop
        time.sleep(0.3)
        before = list(counts)
        time.sleep(1.0)
        self.assertEqual(counts, before)

    def test_dropped_push_without_quorum_not_durable(self):
        cl = self.cluster(quorum=False)
        lead = cl.wait_leader(4.0)
        cl.coords[lead]._push_hook = lambda ep: True
        out = rj(cl.ports[lead], "SCHEDULE SET",
                 body={"name": "x", "entries": [{"frequency": 915000000}]})
        self.assertTrue(out["ok"])
        self.assertFalse(out["durable"])
        self.assertEqual(out["replicated"]["stored"], 1)
        self.assertEqual(len(out["replicated"]["stragglers"]), 2)

    def test_persisted_term_is_the_held_versions_term(self):
        ports = [_free_port(), _free_port()]
        eps = ["127.0.0.1:{}".format(p) for p in ports]
        with tempfile.TemporaryDirectory() as d:
            s = CoordinatorSettings(coordinator_candidates=",".join(eps),
                                    coordinator_self=eps[0], state_dir=d,
                                    election_interval_sec=0.1, election_missed_polls=3,
                                    election_settle_polls=5)
            c = FederationCoordinator(port=ports[0], instances=[], listen_address="127.0.0.1",
                                      settings=s)
            c.scheduler = FederationScheduler(coordinator=c)
            with c._mutation_lock:
                c.scheduler.set_master_schedule_doc(
                    {"name": "s", "entries": [{"frequency": 433000000}]})
                c._commit_version()
            held = (c._sync_term, c._version)
            c.election.observe_term(9, "test")        # own term ran ahead (failed campaign)
            c._persist()
            self.assertEqual(c._sync_doc()["term"], held[0])
            with open(os.path.join(d, "master_schedule.json")) as f:
                self.assertEqual(json.load(f)["term"], held[0])
            # a follower that refused the push (stored:false) at a newer (term, version)
            # does not hold this version and is not counted
            import federation_coordinator as fc
            real = fc.request_json
            fc.request_json = lambda *a, **k: {"ok": True, "stored": False, "term": 9,
                                               "version": held[1]}
            try:
                replicated, _ok, durable = c._replicate()
                self.assertEqual(replicated["stored"], 1)
                self.assertFalse(durable)
                fc.request_json = lambda *a, **k: {"ok": True, "stored": False,
                                                   "term": held[0], "version": held[1]}
                replicated, _ok, durable = c._replicate()
                self.assertEqual(replicated["stored"], 2)
                self.assertTrue(durable)
            finally:
                fc.request_json = real
            c.close()

    def test_auth_on_election_messages(self):
        cl = self.cluster(token="t0ken")
        self.assertIsNotNone(cl.wait_leader(4.0), "election did not work with AUTH")
        self.assertEqual(rj(cl.ports[0], "LEADER")["error"], "auth_required")
        self.assertTrue(rj(cl.ports[0], "LEADER", token="t0ken")["ok"])

    def test_leadership_lost_stops_fanout(self):
        hwc = FakeHWCServer(caps="hwsq,schb,schg,tlck,recl")
        self._cleanup.append(hwc.close)
        hwc.replies["RECL"] = _json_reply({"applied": False, "reason": "disabled"})
        hwc.delays["RECL"] = 1.0
        cl = self.cluster(instances=[member(0, hwc)], extra={"hwc_request_timeout_sec": 3.0})
        lead = cl.wait_leader(4.0)
        c = cl.coords[lead]
        out = {}
        t = threading.Thread(target=lambda: out.update(rj(cl.ports[lead], "RECAL")))
        t.start()
        self.assertTrue(wait_for(lambda: "RECL" in hwc.command_verbs(), 3.0))
        c.election.observe_term(c._role_term()[1] + 5, source="test")
        t.join(10)
        r = out["results"]["node0:0"]
        self.assertFalse(r.get("ok"))
        self.assertIn(r.get("error"), ("leadership_lost", "cancelled"), r)
        self.assertIsNone(r.get("applied"))       # RECL was sent, INIT never
        time.sleep(0.3)
        self.assertNotIn("INIT", hwc.command_verbs())

    def test_election_cadence_during_long_rebalance(self):
        hwc = FakeHWCServer()
        st = FakeStatusServer()
        self._cleanup.extend([hwc.close, st.close])
        hwc.delays["SCHE"] = 5.0
        cl = self.cluster(instances=[member(0, hwc, st)],
                          extra={"hwc_request_timeout_sec": 7.0, "max_member_rto_sec": 10.0})
        lead = cl.wait_leader(4.0)
        c = cl.coords[lead]
        sched = {"name": "big", "entries": [{"frequency": 400000000 + i, "gains": [100] * 5}
                                            for i in range(4)]}
        self.assertTrue(rj(cl.ports[lead], "SCHEDULE SET", body=sched)["ok"])
        self.assertTrue(wait_for(lambda: c._health_warm(), 5.0))
        result = {}
        th = threading.Thread(target=lambda: result.update(json.loads(
            raw_request(cl.ports[lead], b"REBALANCE", timeout=30).decode())))
        th.start()
        time.sleep(0.5)
        start_count = len(c.election_runner.tick_starts)
        time.sleep(3.0)
        spans = list(c.election_runner.tick_starts)[-(len(c.election_runner.tick_starts)
                                                     - start_count):]
        th.join(30)
        self.assertGreater(len(spans), 15)
        gaps = [b[0] - a[0] for a, b in zip(spans, spans[1:])]
        self.assertLess(max(gaps), 0.5)
        self.assertEqual(cl.leaders(), [lead])
        self.assertEqual(result.get("status"), "rebalanced", result)


if __name__ == "__main__":
    unittest.main()
