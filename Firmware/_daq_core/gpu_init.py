#!/usr/bin/env python3
"""
   GPU Initialization and Self-Test Utility.

   Initializes the VideoCore VI GPU for compute offload and runs a
   self-test FFT to verify correctness. Outputs results as JSON.

   Runnable standalone:  python3 gpu_init.py

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
import json
import logging
import sys

logger = logging.getLogger(__name__)


def init_gpu(fft_size=1024):
    """Initialize VideoCore VI GPU for compute offload.

    Steps:
        1. Check pyopencl availability
        2. Create OpenCL context
        3. Instantiate GPUFFTEngine
        4. Run self-test FFT
        5. Report capabilities

    Parameters
    ----------
    fft_size : int
        FFT size for self-test (default 1024, must be power of 2).

    Returns
    -------
    dict
        Result with keys: available (bool), device (str or None),
        platform (str or None), selftest (dict or None), error (str or None).
    """
    result = {
        'available': False,
        'device': None,
        'platform': None,
        'selftest': None,
        'error': None,
    }

    # Step 1: Check pyopencl
    try:
        import pyopencl as cl
    except ImportError:
        result['error'] = 'pyopencl not installed'
        logger.warning("pyopencl not available -- GPU init failed")
        return result

    # Step 2: Create context and enumerate
    try:
        platforms = cl.get_platforms()
        if not platforms:
            result['error'] = 'No OpenCL platforms found'
            logger.warning("No OpenCL platforms found")
            return result

        # Find a suitable device
        device = None
        platform_name = None
        for plat in platforms:
            devices = plat.get_devices()
            if devices:
                device = devices[0]
                platform_name = plat.name
                break

        if device is None:
            result['error'] = 'No OpenCL devices found'
            logger.warning("No OpenCL devices found")
            return result

        result['device'] = device.name
        result['platform'] = platform_name

        logger.info("OpenCL device: %s (%s)", device.name, platform_name)
        logger.info("  Max compute units: %d", device.max_compute_units)
        logger.info("  Max work group size: %d", device.max_work_group_size)
        logger.info("  Global memory: %d MB",
                     device.global_mem_size // (1024 * 1024))

    except Exception as e:
        result['error'] = 'OpenCL context creation failed: {}'.format(e)
        logger.error("Failed to create OpenCL context: %s", e)
        return result

    # Step 3: Instantiate GPUFFTEngine
    try:
        from offload_gpu import GPUFFTEngine
        engine = GPUFFTEngine(fft_size)

        if not engine.available:
            result['error'] = 'GPUFFTEngine initialization failed'
            logger.warning("GPUFFTEngine not available after init")
            return result

    except Exception as e:
        result['error'] = 'GPUFFTEngine creation failed: {}'.format(e)
        logger.error("Failed to create GPUFFTEngine: %s", e)
        return result

    # Step 4: Run self-test
    try:
        selftest_result = engine.selftest()
        result['selftest'] = selftest_result

        if selftest_result['passed']:
            result['available'] = True
            logger.info("GPU self-test PASSED (max_error=%.2e)",
                        selftest_result['max_error'])
        else:
            result['error'] = 'Self-test failed (max_error={:.2e})'.format(
                selftest_result['max_error'])
            logger.warning("GPU self-test FAILED (max_error=%.2e)",
                           selftest_result['max_error'])

    except Exception as e:
        result['error'] = 'Self-test exception: {}'.format(e)
        logger.error("GPU self-test exception: %s", e)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    fft_size = 1024
    if len(sys.argv) > 1:
        try:
            fft_size = int(sys.argv[1])
        except ValueError:
            print("Usage: python3 gpu_init.py [fft_size]")
            sys.exit(1)

    result = init_gpu(fft_size)
    print(json.dumps(result, indent=2, default=str))

    sys.exit(0 if result['available'] else 1)
