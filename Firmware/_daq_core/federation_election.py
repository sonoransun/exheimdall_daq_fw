"""
    Federation Election — lease/term election of the coordinator candidates

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Design spec C.4. Configured only when ``[federation]
    coordinator_candidates`` is non-empty; otherwise the coordinator is
    ``static`` (term 0) and nothing here runs.

    * ``Election`` is a pure state machine: every message handler and the
      periodic ``tick(rpc)`` take the election lock only for in-memory state
      changes and the term persistence write. Network I/O is injected
      (``rpc``), so tests drive it with a simulated clock and a directed
      reachability matrix.
    * ``TermStore`` persists ``{"term", "voted_for"}`` atomically (temp +
      ``os.replace`` + fsync of file and directory) **before** any message
      carrying the term is sent. A failed write makes the candidate
      ineligible (``state_dir_unwritable``): it never claims and never grants.
    * ``ElectionRunner`` runs the ticks on their own thread, never
      overlapping (the next tick starts at ``max(scheduled_start,
      end_of_previous_tick)``, scheduled starts jittered by +-10 %).

    Wire messages (on :6000, newline-terminated; an optional trailing
    cluster_id token is checked by the receiver):

        LEADER [<cluster_id>]
        PREVOTE <term> <endpoint> [<cluster_id>]
        LEASE <term> <endpoint> [<cluster_id>]

    This is not consensus: with ``require_quorum = 0`` two leaders are
    possible during a partition and members fence stale terms (C.4.5).
"""
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from federation_common import request_json

#: Terms are u32 on the wire; a candidate never increments past this.
TERM_MAX = 0xFFFFFFF0
#: Largest term a message may carry (u32); a larger one is ignored, never adopted.
TERM_U32_MAX = 0xFFFFFFFF

ROLE_FOLLOWER = "follower"
ROLE_LEADER = "leader"


def settle_polls_min(interval, missed_polls, clock_margin):
    """Smallest valid ``election_settle_polls``: ``ceil(L*(1+eps)/I) + 1``."""
    lease = float(interval) * int(missed_polls)
    return int(math.ceil(lease * (1.0 + float(clock_margin)) / float(interval) - 1e-9)) + 1


def normalize_endpoint(ep):
    """``host:port`` with the host lower-cased; raises ValueError when malformed."""
    text = str(ep).strip()
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("bad coordinator endpoint '{}' (expected host:port)".format(text))
    if any(c.isspace() for c in host):
        raise ValueError("bad coordinator endpoint '{}' (expected host:port)".format(text))
    return "{}:{}".format(host.lower(), int(port))


def parse_candidates(value):
    """Parse ``coordinator_candidates`` (``host:port,...``, unique)."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        ep = normalize_endpoint(item)
        if ep in out:
            raise ValueError("duplicate coordinator candidate '{}'".format(ep))
        out.append(ep)
    return out


def validate_election_settings(candidates, interval=2.0, missed_polls=3, settle_polls=5,
                               clock_margin=0.05, failback_sec=60.0, require_quorum=False):
    """Raise ValueError for invalid election keys; return a list of warning strings."""
    if float(interval) <= 0:
        raise ValueError("election_interval_sec must be > 0")
    if int(missed_polls) < 1:
        raise ValueError("election_missed_polls must be >= 1")
    if not 0.0 <= float(clock_margin) <= 0.5:
        raise ValueError("election_clock_margin must be within 0.0..0.5")
    if float(failback_sec) < 0:
        raise ValueError("election_failback_sec must be >= 0")
    minimum = settle_polls_min(interval, missed_polls, clock_margin)
    if int(settle_polls) < minimum:
        raise ValueError(
            "election_settle_polls = {} is below the safe minimum {} "
            "(ceil(L*(1+eps)/I) + 1 with L = election_interval_sec * election_missed_polls)"
            .format(int(settle_polls), minimum))
    warnings = []
    if require_quorum and len(candidates) < 3:
        warnings.append("with 2 candidates, losing either host blocks all mutations")
    return warnings


class TermStore(object):
    """``<state_dir>/term``: ``{"term": t, "voted_for": "host:port"|null}``."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "term")

    def load(self):
        """(term, voted_for); (0, None) when absent. Raises OSError/ValueError when unreadable."""
        try:
            with open(self.path, "r") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return 0, None
        if not isinstance(doc, dict):
            raise ValueError("{} is not a JSON object".format(self.path))
        term = int(doc.get("term", 0))
        voted = doc.get("voted_for")
        if term < 0 or term > TERM_U32_MAX:
            raise ValueError("term out of range in {}".format(self.path))
        return term, (str(voted) if voted else None)

    def save(self, term, voted_for):
        atomic_write_json(self.path, {"term": int(term), "voted_for": voted_for})


class MemoryTermStore(object):
    """In-memory store for tests; ``fail = True`` makes every save raise."""

    def __init__(self, term=0, voted_for=None):
        self.term = term
        self.voted_for = voted_for
        self.fail = False
        self.saves = 0

    def load(self):
        return self.term, self.voted_for

    def save(self, term, voted_for):
        if self.fail:
            raise OSError("simulated unwritable state_dir")
        self.term, self.voted_for = int(term), voted_for
        self.saves += 1


def atomic_write_json(path, doc):
    """Write *doc* as JSON to *path*: temp file + fsync + os.replace + fsync(dir)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


class _Peer(object):
    __slots__ = ("endpoint", "rank", "failures", "reachable", "reachable_since", "role",
                 "term", "eligible", "lease_holder", "last_leader_report", "good_since",
                 "reason")

    def __init__(self, endpoint, rank):
        self.endpoint = endpoint
        self.rank = rank
        self.failures = 0
        self.reachable = False
        self.reachable_since = None
        self.role = None
        self.term = 0
        self.eligible = None
        self.lease_holder = None
        self.last_leader_report = None
        self.good_since = None
        self.reason = None


class Election(object):
    """Lease/vote election state machine of one coordinator candidate (C.4.3, C.4.4)."""

    def __init__(self, candidates, self_endpoint, *, interval=2.0, missed_polls=3,
                 settle_polls=5, clock_margin=0.05, failback_sec=60.0,
                 require_quorum=False, store=None, cluster_id="default",
                 clock=time.monotonic, on_change=None, schedule_version_fn=None,
                 schedule_term_fn=None, logger=None):
        self.candidates = parse_candidates(candidates)
        self.self_endpoint = normalize_endpoint(self_endpoint)
        if self.self_endpoint not in self.candidates:
            raise ValueError("coordinator_self '{}' is not one of coordinator_candidates"
                             .format(self.self_endpoint))
        self.warnings = validate_election_settings(
            self.candidates, interval, missed_polls, settle_polls, clock_margin,
            failback_sec, require_quorum)
        self.I = float(interval)
        self.K = int(missed_polls)
        self.L = self.I * self.K
        self.G = int(settle_polls) * self.I
        self.eps = float(clock_margin)
        self.F = float(failback_sec)
        self.require_quorum = bool(require_quorum)
        self.cluster_id = cluster_id
        self.clock = clock
        self.on_change = on_change
        self.schedule_version_fn = schedule_version_fn
        self.schedule_term_fn = schedule_term_fn
        self.logger = logger or logging.getLogger(__name__)
        self.store = store if store is not None else MemoryTermStore()
        self.lock = threading.Lock()
        self.n = len(self.candidates)
        self.rank = self.candidates.index(self.self_endpoint)
        self.peers = {ep: _Peer(ep, i) for i, ep in enumerate(self.candidates)
                      if ep != self.self_endpoint}

        self.state_error = False
        self.exhausted = False
        try:
            self.term, self.voted_for = self.store.load()
        except (OSError, ValueError) as e:
            self.logger.error("election: cannot read the term file: %s; this candidate is "
                              "not eligible", e)
            self.term, self.voted_for = 0, None
            self.state_error = True
        now = self.clock()
        self.start_mono = now
        self.role = ROLE_FOLLOWER
        self.reason = None
        self.leader = None             # (endpoint, term) of the last known leader
        self.lease_holder = None       # endpoint this candidate granted a lease to
        self.lease_expiry = 0.0
        self.grants = {}               # endpoint -> send time of a granted LEASE
        self.last_leader_seen = now
        self.leader_since = None
        self.last_quorum_mono = now
        self.hold_until = 0.0
        self.ticks = 0

    # ------------------------------------------------------------------
    # helpers (lock held)
    # ------------------------------------------------------------------

    def _settling(self, now):
        return now - self.start_mono < self.G

    def _eligibility(self, now):
        if self.state_error:
            return False, "state_dir_unwritable"
        if self.exhausted:
            return False, "term_exhausted"
        if self._settling(now):
            return False, "settling"
        return True, None

    def _lease_valid(self, now):
        return self.lease_holder is not None and self.lease_expiry > now

    def _quorum(self, now):
        horizon = self.L * (1.0 - self.eps)
        live = sum(1 for t in self.grants.values() if t + horizon > now)
        return (1 + live) > self.n / 2.0

    def _persist(self, term, voted_for):
        try:
            self.store.save(term, voted_for)
            return True
        except (OSError, ValueError) as e:
            if not self.state_error:
                self.logger.error("election: cannot persist the term (%s); this candidate "
                                  "is no longer eligible (state_dir_unwritable)", e)
            self.state_error = True
            if self.role == ROLE_LEADER:
                self._step_down("state_dir_unwritable")
            return False

    def _step_down(self, reason):
        was = self.role
        now = self.clock()
        self.role = ROLE_FOLLOWER
        self.reason = reason
        self.grants = {}
        self.leader_since = None
        self.last_leader_seen = now
        if reason == "handover":
            # let the better-ranked candidate campaign first: its lease from us
            # runs out after L and its own window is another L
            self.hold_until = now + 2.0 * self.L + 2.0 * self.I
        if was == ROLE_LEADER:
            self.logger.warning("election: stepping down at term %d (%s)", self.term, reason)
        return was == ROLE_LEADER

    def _adopt(self, term, source):
        """Adopt a higher term (persisted first). Returns True when adopted."""
        if term <= self.term:
            return False
        if term > TERM_U32_MAX:
            self.logger.warning("election: ignoring out-of-range term %d from %s", term, source)
            return False
        if not self._persist(term, None):
            return False
        self.term = term
        self.voted_for = None
        if self.role == ROLE_LEADER:
            self._step_down("higher_term")
        self.logger.info("election: adopted term %d from %s", term, source)
        return True

    def _notify(self):
        if self.on_change is not None:
            try:
                self.on_change(self.role, self.term)
            except Exception:
                self.logger.exception("election on_change callback failed")

    def _schedule_version(self):
        return self._held(self.schedule_version_fn)

    def _schedule_term(self):
        return self._held(self.schedule_term_fn)

    @staticmethod
    def _held(fn):
        if fn is None:
            return 0
        try:
            return int(fn())
        except Exception:
            return 0

    def _cluster_ok(self, cluster_id):
        return cluster_id is None or cluster_id == "" or cluster_id == self.cluster_id

    # ------------------------------------------------------------------
    # public read side
    # ------------------------------------------------------------------

    def role_term(self):
        with self.lock:
            return self.role, self.term

    def has_quorum(self):
        with self.lock:
            return self.role == ROLE_LEADER and self._quorum(self.clock())

    def leader_info(self):
        """{"endpoint","term"} of the current/last known leader, or None."""
        with self.lock:
            return self._leader_info(self.clock())

    def _leader_info(self, now):
        if self.role == ROLE_LEADER:
            return {"endpoint": self.self_endpoint, "term": self.term}
        if self._lease_valid(now):
            return {"endpoint": self.lease_holder, "term": self.term}
        if self.leader is not None:
            return {"endpoint": self.leader[0], "term": self.leader[1]}
        return None

    def eligible(self):
        with self.lock:
            return self._eligibility(self.clock())

    # ------------------------------------------------------------------
    # message handlers (C.4.3)
    # ------------------------------------------------------------------

    def handle_leader(self, cluster_id=None):
        """Reply object of ``LEADER``."""
        if not self._cluster_ok(cluster_id):
            return {"ok": False, "error": "cluster_mismatch", "expected": self.cluster_id}
        version = self._schedule_version()
        sched_term = self._schedule_term()
        with self.lock:
            now = self.clock()
            el, why = self._eligibility(now)
            if self.role == ROLE_LEADER:
                holder, expires = self.self_endpoint, int(self.L * 1000)
            elif self._lease_valid(now):
                holder, expires = self.lease_holder, int((self.lease_expiry - now) * 1000)
            else:
                holder, expires = None, 0
            return {
                "ok": True, "role": self.role, "term": self.term, "eligible": el,
                "reason": why if not el else self.reason,
                "schedule_version": version,
                # term of the held version (not the election term): pull
                # repair compares (schedule_term, schedule_version) (C.4.7)
                "schedule_term": sched_term,
                "leader": self._leader_info(now),
                "lease": {"holder": holder, "term": self.term, "expires_in_ms": max(0, expires)},
                "candidates": [{"endpoint": p.endpoint, "reachable": p.reachable,
                                "role": p.role, "term": p.term}
                               for p in sorted(self.peers.values(), key=lambda x: x.rank)],
                "quorum": self.role == ROLE_LEADER and self._quorum(now),
            }

    def handle_prevote(self, term, endpoint, cluster_id=None):
        """Reply object of ``PREVOTE <t> <ep>`` (no state change)."""
        if not self._cluster_ok(cluster_id):
            return {"ok": False, "error": "cluster_mismatch", "expected": self.cluster_id}
        term = int(term)
        endpoint = normalize_endpoint(endpoint)
        with self.lock:
            now = self.clock()
            holder = self.lease_holder if self._lease_valid(now) else None
            reason = None
            granted = False
            if self._settling(now):
                reason = "settling"
            elif self.state_error:
                reason = "state_dir_unwritable"
            elif term <= self.term:
                pass
            elif holder is not None and holder != endpoint:
                pass
            elif self.role == ROLE_LEADER and self._quorum(now):
                pass
            else:
                granted = True
            return {"ok": True, "granted": granted, "term": self.term,
                    "lease_holder": holder, "reason": reason}

    def handle_lease(self, term, endpoint, cluster_id=None):
        """Reply object of ``LEASE <t> <ep>``."""
        if not self._cluster_ok(cluster_id):
            return {"ok": False, "error": "cluster_mismatch", "expected": self.cluster_id}
        term = int(term)
        endpoint = normalize_endpoint(endpoint)
        changed = False
        with self.lock:
            now = self.clock()
            lease_ms = int(self.L * 1000)
            reason = None
            granted = False
            if self._settling(now):
                reason = "settling"
            elif self.state_error:
                reason = "state_dir_unwritable"
            elif term < self.term:
                pass
            else:
                proceed = True
                if term > self.term:
                    if self._lease_valid(now) and self.lease_holder != endpoint:
                        proceed = False            # stickiness: do not adopt t
                    else:
                        was_leader = self.role == ROLE_LEADER
                        if not self._adopt(term, endpoint):
                            proceed = False
                        changed = changed or was_leader
                if proceed and term == self.term:
                    if self.voted_for in (None, endpoint):
                        if self.voted_for != endpoint and not self._persist(self.term, endpoint):
                            proceed = False
                        if proceed:
                            self.voted_for = endpoint
                            self.lease_holder = endpoint
                            self.lease_expiry = now + self.L
                            self.leader = (endpoint, term)
                            self.last_leader_seen = now
                            granted = True
            holder = self.lease_holder if self._lease_valid(now) else None
            reply = {"ok": True, "granted": granted, "term": self.term, "lease_ms": lease_ms,
                     "lease_holder": holder, "reason": reason}
        if changed:
            self._notify()
        return reply

    def observe_term(self, term, source="reply"):
        """A message carried *term* (e.g. an XACK reply): adopt it when higher and step down.

        Returns True when the term was adopted.
        """
        with self.lock:
            was_leader = self.role == ROLE_LEADER
            adopted = self._adopt(int(term), source)
        if adopted and was_leader:
            self._notify()
        return adopted

    # ------------------------------------------------------------------
    # tick (C.4.4)
    # ------------------------------------------------------------------

    def _suffix(self):
        return " " + self.cluster_id if self.cluster_id else ""

    def _absorb_terms(self, replies, source_verb):
        """Adopt the highest term carried by *replies*; returns True when adopted."""
        best, src = 0, None
        for ep, r in replies.items():
            if isinstance(r, dict) and r.get("ok") and isinstance(r.get("term"), int):
                if r["term"] > best:
                    best, src = r["term"], ep
        if best > self.term:
            return self._adopt(best, "{} reply of {}".format(source_verb, src))
        return False

    def tick(self, rpc):
        """One election step. ``rpc([(endpoint, line), ...]) -> {endpoint: reply|None}``."""
        others = [p.endpoint for p in sorted(self.peers.values(), key=lambda x: x.rank)]
        changed_before = self.role_term()
        replies = rpc([(ep, "LEADER" + self._suffix()) for ep in others]) if others else {}
        with self.lock:
            self.ticks += 1
            now = self.clock()
            self._update_peers(replies, now)
            self._absorb_terms(replies, "LEADER")
            for ep, r in replies.items():
                if not (isinstance(r, dict) and r.get("ok")):
                    continue
                if r.get("role") == ROLE_LEADER and r.get("term", -1) >= self.term:
                    if self.role == ROLE_LEADER and r.get("term") == self.term:
                        # two leaders in one term (possible only without quorum):
                        # the worse-ranked one yields
                        if self.peers[ep].rank < self.rank:
                            self._step_down("conflict")
                    if self.role != ROLE_LEADER:
                        self.last_leader_seen = now
                        self.leader = (ep, r["term"])
            role = self.role
        if role == ROLE_LEADER:
            self._leader_step(rpc, others)
        else:
            self._follower_step(rpc, others)
        if self.role_term() != changed_before:
            self._notify()

    def _update_peers(self, replies, now):
        for ep, p in self.peers.items():
            r = replies.get(ep)
            if not (isinstance(r, dict) and r.get("ok")):
                p.failures += 1
                p.reachable = False
                p.reachable_since = None
                p.good_since = None
                continue
            p.failures = 0
            if not p.reachable:
                p.reachable_since = now
            p.reachable = True
            p.role = r.get("role")
            p.term = r.get("term", 0)
            p.eligible = bool(r.get("eligible"))
            p.reason = r.get("reason")
            lease = r.get("lease") or {}
            p.lease_holder = lease.get("holder")
            if p.role == ROLE_LEADER:
                p.last_leader_report = now
            good = p.eligible and p.lease_holder in (None, self.self_endpoint)
            if good:
                if p.good_since is None:
                    p.good_since = now
            else:
                p.good_since = None

    def _leader_step(self, rpc, others):
        with self.lock:
            if self.role != ROLE_LEADER:
                return
            now = self.clock()
            for p in self.peers.values():
                if (p.rank < self.rank and p.good_since is not None
                        and now - p.good_since >= self.F):
                    self._step_down("handover")
                    return
            term = self.term
        send_time = self.clock()
        line = "LEASE {} {}{}".format(term, self.self_endpoint, self._suffix())
        replies = rpc([(ep, line) for ep in others]) if others else {}
        with self.lock:
            if self.role != ROLE_LEADER or self.term != term:
                return
            if self._absorb_terms(replies, "LEASE"):
                return
            for ep, r in replies.items():
                if isinstance(r, dict) and r.get("ok") and r.get("granted"):
                    self.grants[ep] = send_time
            now = self.clock()
            if self._quorum(now):
                self.last_quorum_mono = now
            elif self.require_quorum and now - self.last_quorum_mono >= self.L:
                self._step_down("no_quorum")

    def _may_campaign(self, now):
        el, _ = self._eligibility(now)
        if not el or now < self.hold_until:
            return False
        if self._lease_valid(now) and self.lease_holder != self.self_endpoint:
            self.last_leader_seen = now
            return False
        window = self.L + self.rank * self.I
        if now - self.last_leader_seen < window:
            return False
        for p in self.peers.values():
            if p.rank >= self.rank:
                continue
            down = p.failures >= self.K
            ineligible = p.eligible is False
            not_leader = p.last_leader_report is None or p.last_leader_report < now - window
            if not (down or ineligible or not_leader):
                return False
        if self.term >= TERM_MAX:
            if not self.exhausted:
                self.logger.critical("term space exhausted; run daq_stop.sh N --reset-fence on "
                                     "every member and wipe the coordinator state_dir")
            self.exhausted = True
            return False
        return True

    def _follower_step(self, rpc, others):
        with self.lock:
            now = self.clock()
            if not self._may_campaign(now):
                return
            base = self.term
            t_new = base + 1
        line = "PREVOTE {} {}{}".format(t_new, self.self_endpoint, self._suffix())
        replies = rpc([(ep, line) for ep in others]) if others else {}
        with self.lock:
            if self.term != base or self.role == ROLE_LEADER:
                return
            if self._absorb_terms(replies, "PREVOTE") and self.term >= t_new:
                return
            granted = 0
            blocked = False
            for ep, r in replies.items():
                if not (isinstance(r, dict) and r.get("ok")):
                    continue
                if r.get("granted"):
                    granted += 1
                elif (r.get("reason") != "settling" and r.get("lease_holder")
                      and r.get("lease_holder") != self.self_endpoint):
                    blocked = True
            if self.require_quorum:
                if granted + 1 <= self.n / 2.0:
                    return
            elif blocked:
                return
            if self.term != base:
                return
            if not self._persist(t_new, self.self_endpoint):
                return
            self.term = t_new
            self.voted_for = self.self_endpoint
            self.reason = None
        send_time = self.clock()
        line = "LEASE {} {}{}".format(t_new, self.self_endpoint, self._suffix())
        replies = rpc([(ep, line) for ep in others]) if others else {}
        with self.lock:
            if self.term != t_new or self.role == ROLE_LEADER:
                return
            if self._absorb_terms(replies, "LEASE"):
                return
            grants = {}
            blocked = False
            for ep, r in replies.items():
                if not (isinstance(r, dict) and r.get("ok")):
                    continue
                if r.get("granted"):
                    grants[ep] = send_time
                elif (r.get("reason") != "settling" and r.get("lease_holder")
                      and r.get("lease_holder") != self.self_endpoint):
                    blocked = True
            if self.require_quorum:
                if len(grants) + 1 <= self.n / 2.0:
                    return
            elif blocked:
                return
            now = self.clock()
            self.role = ROLE_LEADER
            self.reason = None
            self.grants = grants
            self.leader = (self.self_endpoint, self.term)
            self.leader_since = now
            self.last_quorum_mono = now
            self.lease_holder = None
            self.logger.info("election: %s is leader at term %d", self.self_endpoint, self.term)


class ElectionRunner(object):
    """Runs ``Election.tick`` on its own thread with real :6000 I/O."""

    def __init__(self, election, auth_token=None, peer_timeout=2.0, request=request_json,
                 jitter=0.1):
        self.election = election
        self.auth_token = auth_token
        self.t_el = max(0.01, min(float(peer_timeout), election.I / 2.0))
        self.jitter = float(jitter)
        self._request = request
        self._stop = threading.Event()
        self._thread = None
        n = max(1, len(election.peers))
        self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="election")
        self.tick_starts = []          # (start, end) monotonic pairs of the last ticks
        self.logger = election.logger

    def rpc(self, requests):
        """Send every (endpoint, line) in parallel; {endpoint: reply or None} within T_el."""
        futures = {}
        for ep, line in requests:
            host, _, port = ep.rpartition(":")
            futures[self._executor.submit(self._one, host, int(port), line)] = ep
        done, _ = wait(futures, timeout=self.t_el + 0.05)
        out = {}
        for fut, ep in futures.items():
            out[ep] = fut.result() if fut in done else None
        return out

    def _one(self, host, port, line):
        try:
            return self._request(host, port, line, auth_token=self.auth_token,
                                 connect_timeout=self.t_el, timeout=self.t_el)
        except Exception:
            return None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="election")
        self._thread.start()

    def _loop(self):
        I = self.election.I
        scheduled = self.election.clock()
        while not self._stop.is_set():
            start = self.election.clock()
            try:
                self.election.tick(self.rpc)
            except Exception:
                self.logger.exception("election tick failed")
            end = self.election.clock()
            self.tick_starts.append((start, end))
            del self.tick_starts[:-64]
            scheduled += I * (1.0 + random.uniform(-self.jitter, self.jitter))
            if scheduled < end:
                scheduled = end
            self._stop.wait(max(0.0, scheduled - self.election.clock()))

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.t_el * 3 + 1.0)
        self._executor.shutdown(wait=False)
