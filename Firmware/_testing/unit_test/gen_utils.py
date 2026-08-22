"""
    Description :
    Shared helpers for the unit-test frame generators (gen_std_frame,
    gen_ramp, gen_cw).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import select
import time

A_BUFF_READY = 1
B_BUFF_READY = 2


def wait_consumer_done(out_shmem_iface, timeout=10.0):
    """Deterministically wait (bounded by `timeout` seconds) until the
    consumer has finished with every in-flight buffer and closed its end of
    the backward FIFO — replaces the historic fixed time.sleep(2) after
    send_ctr_terminate().

    The consumer acknowledges buffers with single A/B_BUFF_READY bytes on
    the backward FIFO and, on receiving TERMINATE, closes its FIFO fds;
    the resulting EOF is the drain barrier.
    """
    bw_fd = out_shmem_iface.bw_ctr_fifo
    if bw_fd is None:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        try:
            readable, _, _ = select.select([bw_fd], [], [],
                                           min(0.2, remaining))
        except (OSError, ValueError):
            return
        if not readable:
            continue
        try:
            data = os.read(bw_fd, 16)
        except BlockingIOError:  # drop mode: spurious wakeup
            continue
        except OSError:
            return
        if not data:  # EOF: consumer closed its end -> fully drained
            return
        for byte in data:  # late buffer-free acknowledgements
            if byte == A_BUFF_READY:
                out_shmem_iface.buffer_free[0] = True
            elif byte == B_BUFF_READY:
                out_shmem_iface.buffer_free[1] = True
