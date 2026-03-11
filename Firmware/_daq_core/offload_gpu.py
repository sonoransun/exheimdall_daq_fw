"""
   GPU FFT Engine for HeIMDALL DAQ Firmware.

   Uses pyopencl + VC4CL to offload FFT computation to the VideoCore VI GPU
   on Raspberry Pi 4. Falls back gracefully when pyopencl is not available.

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3

   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import logging
import math
import numpy as np

# Optional dependency -- the module stays importable without pyopencl
try:
    import pyopencl as cl
    import pyopencl.array as cl_array
    _HAS_PYOPENCL = True
except ImportError:
    _HAS_PYOPENCL = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenCL kernel source: radix-2 Cooley-Tukey FFT for complex float32
# ---------------------------------------------------------------------------

_FFT_KERNEL_SRC = r"""
/* Radix-2 Cooley-Tukey FFT kernel for complex float2 data.
 *
 * Each work-item handles one butterfly at each stage.
 * data layout: float2 (x = real, y = imag), length N (power of 2).
 *
 * Parameters:
 *   data      -- input/output buffer of float2[N]
 *   N         -- transform length
 *   log2N     -- log2(N)
 *   direction -- +1 for forward FFT, -1 for inverse FFT
 */

__kernel void bit_reverse(__global float2 *data, const int N, const int log2N) {
    int gid = get_global_id(0);
    if (gid >= N) return;

    int rev = 0;
    int val = gid;
    for (int i = 0; i < log2N; i++) {
        rev = (rev << 1) | (val & 1);
        val >>= 1;
    }

    if (gid < rev) {
        float2 tmp = data[gid];
        data[gid] = data[rev];
        data[rev] = tmp;
    }
}

__kernel void butterfly(__global float2 *data,
                        const int N,
                        const int half_size,
                        const int direction) {
    int gid = get_global_id(0);
    int size = half_size * 2;

    int group = gid / half_size;
    int pair  = gid % half_size;

    int i = group * size + pair;
    int j = i + half_size;

    if (j >= N) return;

    float angle = (float)direction * -2.0f * M_PI_F * (float)pair / (float)size;
    float2 w = (float2)(cos(angle), sin(angle));

    float2 a = data[i];
    float2 b;
    b.x = data[j].x * w.x - data[j].y * w.y;
    b.y = data[j].x * w.y + data[j].y * w.x;

    data[i] = a + b;
    data[j] = a - b;
}

__kernel void scale_inverse(__global float2 *data, const int N) {
    int gid = get_global_id(0);
    if (gid >= N) return;
    data[gid] /= (float)N;
}
"""


class GPUFFTEngine:
    """VideoCore VI GPU-accelerated FFT via OpenCL.

    Parameters
    ----------
    fft_size : int
        Transform length (must be power of 2).
    batch_size : int
        Reserved for future batch support (default 4).
    """

    def __init__(self, fft_size, batch_size=4):
        self.logger = logging.getLogger(__name__)
        self._available = False
        self.fft_size = fft_size
        self.batch_size = batch_size

        # Validate fft_size is a power of 2
        if fft_size < 2 or (fft_size & (fft_size - 1)) != 0:
            self.logger.error("fft_size must be a power of 2, got %d", fft_size)
            return

        self.log2N = int(math.log2(fft_size))

        if not _HAS_PYOPENCL:
            self.logger.warning("pyopencl not available -- GPU FFT engine disabled")
            return

        # Create OpenCL context (prefer VideoCore VI if present)
        try:
            self._ctx = cl.create_some_context(interactive=False)
            self._queue = cl.CommandQueue(self._ctx)
            self._prg = cl.Program(self._ctx, _FFT_KERNEL_SRC).build()
            self._available = True
            dev_name = self._ctx.devices[0].name
            self.logger.info("GPU FFT engine initialized on: %s (N=%d)",
                             dev_name, fft_size)
        except Exception as e:
            self.logger.warning("Failed to initialize OpenCL context: %s", e)

    @property
    def available(self):
        """True when the GPU backend is usable."""
        return self._available

    def forward(self, data, overwrite_x=False):
        """Compute forward FFT on the GPU.

        Parameters
        ----------
        data : ndarray, complex64 or complex128
            Input data of length fft_size.
        overwrite_x : bool
            Ignored (kept for API compatibility).

        Returns
        -------
        ndarray, complex64
        """
        if not self._available:
            raise RuntimeError("GPU FFT engine is not available")
        return self._run_fft(np.asarray(data, dtype=np.complex64), direction=1)

    def inverse(self, data, overwrite_x=False):
        """Compute inverse FFT on the GPU.

        Parameters
        ----------
        data : ndarray, complex64 or complex128
            Input data of length fft_size.
        overwrite_x : bool
            Ignored (kept for API compatibility).

        Returns
        -------
        ndarray, complex64
        """
        if not self._available:
            raise RuntimeError("GPU FFT engine is not available")
        return self._run_fft(np.asarray(data, dtype=np.complex64), direction=-1)

    def xcorr_batch(self, ref, test_channels, N_proc):
        """Compute cross-correlation magnitude squared for multiple channels.

        Parameters
        ----------
        ref : ndarray, complex64
            Reference channel samples (length N_proc).
        test_channels : ndarray, complex64, shape (n_ch, N_proc)
            Test channel samples.
        N_proc : int
            Processing length.

        Returns
        -------
        list of ndarray
            Cross-correlation |.|^2 for each test channel.
        """
        if not self._available:
            raise RuntimeError("GPU FFT engine is not available")

        np_zeros = np.zeros(N_proc, dtype=np.complex64)
        x_padd = np.concatenate([ref[:N_proc], np_zeros])
        x_fft = self._run_fft(x_padd, direction=1)
        x_fft_conj = x_fft.conj()

        results = []
        for ch_idx in range(test_channels.shape[0]):
            y_padd = np.concatenate([np_zeros, test_channels[ch_idx, :N_proc]])
            y_fft = self._run_fft(y_padd, direction=1)
            product = x_fft_conj * y_fft
            corr = self._run_fft(product, direction=-1)
            results.append(np.abs(corr) ** 2)

        return results

    def selftest(self):
        """Run a small FFT and compare against numpy to verify correctness.

        Returns
        -------
        dict
            Test result with keys 'passed', 'max_error', 'fft_size'.
        """
        if not self._available:
            return {'passed': False, 'max_error': float('inf'),
                    'fft_size': self.fft_size,
                    'reason': 'GPU not available'}

        test_size = min(self.fft_size, 1024)
        # Build a small engine if needed
        if test_size != self.fft_size:
            test_engine = GPUFFTEngine(test_size)
        else:
            test_engine = self

        np.random.seed(42)
        test_data = (np.random.randn(test_size).astype(np.float32) +
                     1j * np.random.randn(test_size).astype(np.float32))

        gpu_result = test_engine._run_fft(test_data.copy(), direction=1)
        cpu_result = np.fft.fft(test_data)

        max_error = float(np.max(np.abs(gpu_result - cpu_result.astype(np.complex64))))
        passed = max_error < 1e-2  # Allow some tolerance for float32

        self.logger.info("GPU self-test: passed=%s, max_error=%.6e, N=%d",
                         passed, max_error, test_size)
        return {'passed': passed, 'max_error': max_error, 'fft_size': test_size}

    # -- internal ------------------------------------------------------------

    def _run_fft(self, data, direction=1):
        """Execute FFT on GPU via OpenCL kernels.

        Parameters
        ----------
        data : ndarray, complex64
            Input data.
        direction : int
            +1 for forward, -1 for inverse.

        Returns
        -------
        ndarray, complex64
        """
        N = len(data)
        log2N = int(math.log2(N))

        # Represent complex64 as float2 (pairs of float32)
        buf_np = data.view(np.float32).copy()
        mf = cl.mem_flags
        buf_cl = cl.Buffer(self._ctx, mf.READ_WRITE | mf.COPY_HOST_PTR,
                           hostbuf=buf_np)

        # Bit reversal
        global_size = (N,)
        self._prg.bit_reverse(self._queue, global_size, None,
                              buf_cl, np.int32(N), np.int32(log2N))

        # Butterfly stages
        half_size = 1
        for _ in range(log2N):
            n_butterflies = N // 2
            self._prg.butterfly(self._queue, (n_butterflies,), None,
                                buf_cl, np.int32(N), np.int32(half_size),
                                np.int32(direction))
            half_size *= 2

        # Scale for inverse
        if direction == -1:
            self._prg.scale_inverse(self._queue, global_size, None,
                                    buf_cl, np.int32(N))

        # Read back
        cl.enqueue_copy(self._queue, buf_np, buf_cl)
        self._queue.finish()

        return buf_np.view(np.complex64)
