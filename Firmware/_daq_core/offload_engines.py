"""
   Pluggable Compute Engine Abstractions for HeIMDALL DAQ Firmware.

   Provides FFTEngine and CorrelationEngine classes that dispatch to
   different compute backends (CPU/SciPy, GPU/OpenCL, FPGA).

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
import numpy as np

# Lazy-loaded optional dependencies
_scipy_fft = None
_gpu_engine_cls = None


def _get_scipy_fft():
    """Lazy import of scipy.fft to avoid import overhead when unused."""
    global _scipy_fft
    if _scipy_fft is None:
        from scipy import fft
        _scipy_fft = fft
    return _scipy_fft


def _get_gpu_engine_cls():
    """Lazy import of GPUFFTEngine from offload_gpu module."""
    global _gpu_engine_cls
    if _gpu_engine_cls is None:
        from offload_gpu import GPUFFTEngine
        _gpu_engine_cls = GPUFFTEngine
    return _gpu_engine_cls


# ---------------------------------------------------------------------------
# FFT Engine
# ---------------------------------------------------------------------------

class FFTEngine:
    """Pluggable FFT backend.

    Supported engine types:
        cpu_scipy : SciPy FFT with configurable worker threads (default)
        gpu       : VideoCore VI GPU via OpenCL (offload_gpu.py)
        fpga      : FPGA accelerated via ctypes (stub)
    """

    _VALID_ENGINES = ('cpu_scipy', 'gpu', 'fpga')

    def __init__(self, engine_type='cpu_scipy', fft_size=None, workers=4):
        self.logger = logging.getLogger(__name__)
        self.engine_type = engine_type
        self.fft_size = fft_size
        self.workers = workers

        if engine_type not in self._VALID_ENGINES:
            raise ValueError("Unknown FFT engine '{}'. Available: {}".format(
                engine_type, self._VALID_ENGINES))

        if engine_type == 'cpu_scipy':
            # Eagerly verify that scipy.fft is importable
            _get_scipy_fft()
            self.logger.info("FFTEngine: using cpu_scipy backend (workers=%d)", workers)

        elif engine_type == 'gpu':
            gpu_cls = _get_gpu_engine_cls()
            if fft_size is None:
                raise ValueError("fft_size is required for gpu FFT engine")
            self._gpu = gpu_cls(fft_size)
            if not self._gpu.available:
                raise RuntimeError("GPU FFT engine is not available on this system")
            self.logger.info("FFTEngine: using GPU backend (fft_size=%d)", fft_size)

        elif engine_type == 'fpga':
            raise NotImplementedError(
                "FPGA FFT engine requires the native C extension library "
                "(libfpga_fft.so). Build and install it first.")

    def forward(self, data, overwrite_x=False):
        """Compute forward FFT, returns complex array."""
        if self.engine_type == 'cpu_scipy':
            sfft = _get_scipy_fft()
            return sfft.fft(data, workers=self.workers, overwrite_x=overwrite_x)

        elif self.engine_type == 'gpu':
            return self._gpu.forward(data, overwrite_x=overwrite_x)

        # fpga would have raised in __init__

    def inverse(self, data, overwrite_x=False):
        """Compute inverse FFT, returns complex array."""
        if self.engine_type == 'cpu_scipy':
            sfft = _get_scipy_fft()
            return sfft.ifft(data, workers=self.workers, overwrite_x=overwrite_x)

        elif self.engine_type == 'gpu':
            return self._gpu.inverse(data, overwrite_x=overwrite_x)


# ---------------------------------------------------------------------------
# Correlation Engine
# ---------------------------------------------------------------------------

class CorrelationEngine:
    """Pluggable cross-correlation backend.

    Supported engine types:
        cpu_numpy : FFT-based correlation using scipy.fft (default)
        gpu       : GPU-accelerated correlation via offload_gpu.py
        fpga      : FPGA-accelerated (stub)
    """

    _VALID_ENGINES = ('cpu_numpy', 'gpu', 'fpga')

    def __init__(self, engine_type='cpu_numpy', N_proc=None, workers=4):
        self.logger = logging.getLogger(__name__)
        self.engine_type = engine_type
        self.N_proc = N_proc
        self.workers = workers

        if engine_type not in self._VALID_ENGINES:
            raise ValueError("Unknown correlation engine '{}'. Available: {}".format(
                engine_type, self._VALID_ENGINES))

        if engine_type == 'cpu_numpy':
            _get_scipy_fft()
            self.logger.info("CorrelationEngine: using cpu_numpy backend (workers=%d)", workers)

        elif engine_type == 'gpu':
            gpu_cls = _get_gpu_engine_cls()
            if N_proc is None:
                raise ValueError("N_proc is required for gpu correlation engine")
            self._gpu = gpu_cls(N_proc * 2)
            if not self._gpu.available:
                raise RuntimeError("GPU correlation engine is not available on this system")
            self.logger.info("CorrelationEngine: using GPU backend (N_proc=%d)", N_proc)

        elif engine_type == 'fpga':
            raise NotImplementedError(
                "FPGA correlation engine requires the native C extension library "
                "(libfpga_xcorr.so). Build and install it first.")

    def xcorr(self, ref_signal, test_signal, N_proc):
        """Compute cross-correlation magnitude squared.

        Implements the FFT-based cross-correlation:
            1. Zero-pad reference (trailing) and test (leading) to 2*N_proc
            2. Forward FFT both
            3. Conjugate-multiply in frequency domain
            4. Inverse FFT
            5. Return |result|^2

        This matches the inline implementation in delay_sync.py.

        Parameters
        ----------
        ref_signal : ndarray, complex64
            Reference channel IQ samples (at least N_proc samples).
        test_signal : ndarray, complex64
            Test channel IQ samples (at least N_proc samples).
        N_proc : int
            Number of samples to use for correlation.

        Returns
        -------
        ndarray, float
            Cross-correlation magnitude squared (length 2*N_proc).
        """
        if self.engine_type == 'cpu_numpy':
            return self._xcorr_cpu(ref_signal, test_signal, N_proc)

        elif self.engine_type == 'gpu':
            return self._gpu.xcorr_batch(
                ref_signal[:N_proc],
                test_signal[:N_proc].reshape(1, -1),
                N_proc)[0]

    def _xcorr_cpu(self, ref_signal, test_signal, N_proc):
        """CPU-based FFT cross-correlation (mirrors delay_sync.py lines 665-673)."""
        sfft = _get_scipy_fft()
        np_zeros = np.zeros(N_proc, dtype=np.complex64)

        x_padd = np.concatenate([ref_signal[:N_proc], np_zeros])
        x_fft = sfft.fft(x_padd, workers=self.workers, overwrite_x=True)

        y_padd = np.concatenate([np_zeros, test_signal[:N_proc]])
        y_fft = sfft.fft(y_padd, workers=self.workers, overwrite_x=True)

        corr = sfft.ifft(x_fft.conj() * y_fft, workers=self.workers,
                         overwrite_x=True)
        return np.abs(corr) ** 2
