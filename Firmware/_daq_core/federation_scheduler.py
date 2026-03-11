"""
    Federation Scheduler — Coordinated Frequency Schedule Partitioning

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Extends the DAQ frequency scheduling concept across multiple federated
    instances. Partitions a master frequency schedule among healthy instances
    using configurable strategies (round_robin, range, capability).
"""
import logging
import time


class FederationScheduler:
    """Partition and distribute frequency schedules across federated instances."""

    def __init__(self, coordinator=None, health_monitor=None):
        """
        Parameters
        ----------
        coordinator : FederationCoordinator or None
            Used to send schedule commands to instances.
        health_monitor : FederationHealth or None
            Used to determine which instances are healthy.
        """
        self.logger = logging.getLogger(__name__)
        self.coordinator = coordinator
        self.health_monitor = health_monitor
        self._current_assignments = {}
        self._master_schedule = None

    def set_master_schedule(self, frequencies, gains, dwell_frames, strategy="round_robin"):
        """
        Set the master schedule to be partitioned.

        Parameters
        ----------
        frequencies : list of int
            RF center frequencies in Hz.
        gains : list of int
            Gain values for each frequency.
        dwell_frames : list of int
            Number of frames to dwell on each frequency.
        strategy : str
            Partition strategy: "round_robin", "range", or "capability".
        """
        self._master_schedule = {
            "frequencies": list(frequencies),
            "gains": list(gains),
            "dwell_frames": list(dwell_frames),
            "strategy": strategy,
        }
        self.logger.info("Master schedule set: %d frequencies, strategy=%s",
                         len(frequencies), strategy)

    def partition_schedule(self, instance_ids=None, strategy=None):
        """
        Partition the master schedule across given instances.

        Parameters
        ----------
        instance_ids : list of int or None
            If None, uses healthy peers from health_monitor.
        strategy : str or None
            Override the strategy set in set_master_schedule.

        Returns
        -------
        dict
            Mapping instance_id -> {frequencies, gains, dwell_frames}
        """
        if self._master_schedule is None:
            self.logger.warning("No master schedule set")
            return {}

        if instance_ids is None:
            if self.health_monitor is not None:
                peers = self.health_monitor.get_healthy_peers()
                peer_table = self.health_monitor.get_peer_table()
                instance_ids = [peer_table[p]["instance_id"] for p in peers
                                if peer_table[p]["instance_id"] >= 0]
                instance_ids.append(self.health_monitor.instance_id)
                instance_ids = sorted(set(instance_ids))
            else:
                instance_ids = [0]

        if not instance_ids:
            self.logger.warning("No instances available for partition")
            return {}

        strat = strategy or self._master_schedule.get("strategy", "round_robin")
        freqs = self._master_schedule["frequencies"]
        gains = self._master_schedule["gains"]
        dwells = self._master_schedule["dwell_frames"]

        if strat == "round_robin":
            assignments = self._partition_round_robin(instance_ids, freqs, gains, dwells)
        elif strat == "range":
            assignments = self._partition_range(instance_ids, freqs, gains, dwells)
        else:
            # Default to round_robin for unknown strategies
            assignments = self._partition_round_robin(instance_ids, freqs, gains, dwells)

        self._current_assignments = assignments
        self.logger.info("Schedule partitioned: %s",
                         {k: len(v["frequencies"]) for k, v in assignments.items()})
        return assignments

    def _partition_round_robin(self, instance_ids, freqs, gains, dwells):
        """Alternate frequencies across instances in round-robin order."""
        n = len(instance_ids)
        assignments = {iid: {"frequencies": [], "gains": [], "dwell_frames": []}
                       for iid in instance_ids}
        for i, (f, g, d) in enumerate(zip(freqs, gains, dwells)):
            target = instance_ids[i % n]
            assignments[target]["frequencies"].append(f)
            assignments[target]["gains"].append(g)
            assignments[target]["dwell_frames"].append(d)
        return assignments

    def _partition_range(self, instance_ids, freqs, gains, dwells):
        """Assign contiguous frequency ranges to each instance."""
        n = len(instance_ids)
        total = len(freqs)
        # Sort by frequency
        indices = sorted(range(total), key=lambda i: freqs[i])
        chunk_size = max(1, total // n)

        assignments = {iid: {"frequencies": [], "gains": [], "dwell_frames": []}
                       for iid in instance_ids}
        for chunk_idx, iid in enumerate(instance_ids):
            start = chunk_idx * chunk_size
            if chunk_idx == n - 1:
                # Last instance gets remainder
                end = total
            else:
                end = start + chunk_size
            for idx in indices[start:end]:
                assignments[iid]["frequencies"].append(freqs[idx])
                assignments[iid]["gains"].append(gains[idx])
                assignments[iid]["dwell_frames"].append(dwells[idx])
        return assignments

    def distribute(self):
        """
        Send partitioned schedules to each instance via the coordinator.

        Returns
        -------
        dict
            Per-instance send results.
        """
        if not self._current_assignments:
            self.logger.warning("No assignments to distribute, call partition_schedule first")
            return {}

        if self.coordinator is None:
            self.logger.warning("No coordinator configured, cannot distribute")
            return {}

        results = {}
        for iid, sched in self._current_assignments.items():
            freq_str = ",".join(str(f) for f in sched["frequencies"])
            gain_str = ",".join(str(g) for g in sched["gains"])
            dwell_str = ",".join(str(d) for d in sched["dwell_frames"])
            # Use coordinator to send schedule to this instance
            try:
                result = self.coordinator._send_to_instance(
                    iid,
                    "SCHEDULE {} {} {}".format(freq_str, gain_str, dwell_str)
                )
                results[iid] = result
            except Exception as e:
                results[iid] = {"error": str(e)}
        return results

    def rebalance(self):
        """Re-partition and re-distribute the schedule based on current health."""
        self.logger.info("Rebalancing schedule across federation")
        self.partition_schedule()
        return self.distribute()

    def get_assignments(self):
        """Return the current schedule assignments."""
        return dict(self._current_assignments)
