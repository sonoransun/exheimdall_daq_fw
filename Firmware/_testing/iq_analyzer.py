"""
    Description :
    Offline analyzer/plotter for recorded IQ frames: single-frame .iqf files
    (iq_recorder.py without --hcap) and .hcap captures (iq_capture_format.py;
    --frame selects the frame, default the first frame with samples).

    Now an argparse CLI — the historic module-level defaults are preserved,
    so running it with no arguments analyzes the same file with the same
    plots enabled as before. Without --fs the sample rate is the header's
    sampling_freq (the module default 2.4 MHz when the header has none).
    raw_u8 frames (sample_bit_depth 8) are converted to complex samples
    ((u8 - 127.5) / 127.5).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import logging
import os
import sys

import numpy as np

# Import IQ header module
currentPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currentPath)
sys.path.insert(0, os.path.join(rootPath, "_daq_core"))
sys.path.insert(0, currentPath)
from iq_header import IQHeader
import iq_capture_format as hcap

logging.basicConfig(level=logging.INFO)

"""
---------------------
     P A R A M S
---------------------
(kept as module-level defaults for backward compatibility)
"""
file_name = "VEGAM_2.iqf"
fs = 2.4 * 10**6
std_ch_ind = 0

en_td_plot = True
en_fd_plot = False
en_xcorr_plot = False
en_iq_diff_plot = True

td_plot_ch_ind = 0
xcorr_channels = [0, 1]


def samples_from_payload(iq_header, iq_data_bytes):
    """(active_ant_chs, cpi_length) complex64 array of one frame's payload:
    complex float32 for sample_bit_depth 32, offset-binary u8 I/Q pairs
    (rtl_daq's raw format) for sample_bit_depth 8."""
    M = iq_header.active_ant_chs
    N = iq_header.cpi_length
    if iq_header.sample_bit_depth == 32:
        return np.frombuffer(iq_data_bytes, dtype=np.complex64).reshape(M, N).copy()
    if iq_header.sample_bit_depth == 8:
        raw = np.frombuffer(iq_data_bytes, dtype=np.uint8).reshape(M, N, 2).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        return (raw[:, :, 0] + 1j * raw[:, :, 1]).astype(np.complex64)
    raise ValueError("unsupported sample_bit_depth {:d}".format(iq_header.sample_bit_depth))


def load_frame(fname, frame_index=None):
    """Read one frame and return (header, iq_cf64). A .hcap capture yields
    frame `frame_index`, or its first frame with samples when None; any
    other file is one [1024B header][payload] frame (.iqf)."""
    if fname.endswith(".hcap"):
        iq_header, iq_data_bytes = load_capture_frame(fname, frame_index)
    else:
        with open(fname, "rb") as file_descr:
            iq_header_bytes = file_descr.read(1024)
            iq_header = IQHeader()
            iq_header.decode_header(iq_header_bytes)
            iq_data_length = int((iq_header.cpi_length * iq_header.active_ant_chs
                                  * (2 * iq_header.sample_bit_depth)) / 8)
            iq_data_bytes = file_descr.read(iq_data_length)
    iq_header.dump_header()
    return iq_header, samples_from_payload(iq_header, iq_data_bytes)


def load_capture_frame(fname, frame_index=None):
    """(IQHeader, payload) of frame `frame_index` of a .hcap capture (the
    first frame with samples when None)."""
    for index, (iq_header, payload) in enumerate(hcap.HcapReader(fname)):
        if frame_index is None and payload:
            return iq_header, payload
        if frame_index == index:
            if not payload:
                raise ValueError("frame {:d} of {} has no samples (frame_type {:d})".format(
                    index, fname, iq_header.frame_type))
            return iq_header, payload
    if frame_index is None:
        raise ValueError("{} has no frame with samples".format(fname))
    raise ValueError("{} has no frame {:d}".format(fname, frame_index))


def resolve_sample_rate(sample_rate, iq_header):
    """--fs when given, else the header sampling_freq, else the module default."""
    if sample_rate is not None:
        return sample_rate
    if iq_header.sampling_freq:
        return float(iq_header.sampling_freq)
    return fs


def analyze(fname=file_name, sample_rate=fs, ref_ch=std_ch_ind,
            td_plot=en_td_plot, fd_plot=en_fd_plot, xcorr_plot=en_xcorr_plot,
            iq_diff_plot=en_iq_diff_plot, td_channel=td_plot_ch_ind,
            xcorr_pair=None, show=True, frame_index=None):
    """Plot one frame. sample_rate=None takes the header's sampling_freq."""
    import matplotlib.pyplot as plt  # heavy/optional: import only when used

    if xcorr_pair is None:
        xcorr_pair = xcorr_channels

    iq_header, iq_cf64 = load_frame(fname, frame_index)
    sample_rate = resolve_sample_rate(sample_rate, iq_header)
    N = iq_header.cpi_length
    M = iq_header.active_ant_chs

    # Remove DC
    for m in range(M):
        iq_cf64[m, :] -= np.average(iq_cf64[m, :])

    if td_plot:
        plt.figure(1)
        plt.plot(iq_cf64[td_channel, 0:200].real)
        plt.plot(iq_cf64[td_channel, 0:200].imag)

    if fd_plot:
        plt.figure(2)
        freqs = np.fft.fftfreq(N, 1 / sample_rate)
        freqs = np.fft.fftshift(freqs)
        freqs /= 10**6
        for m in range(1):
            xw = np.fft.fft(iq_cf64[m, :])
            xw = np.fft.fftshift(xw)
            xw = abs(xw)
            xw /= np.max(xw)
            plt.plot(freqs, 20 * np.log10(xw))

    if xcorr_plot:
        plt.figure(3)

        N_proc = 2**16
        np_zeros = np.zeros(N_proc, dtype=np.complex64)
        x_padd = np.concatenate([iq_cf64[ref_ch, 0:N_proc], np_zeros])
        x_fft = np.fft.fft(x_padd)

        time_delay_indices = np.arange(0, 2 * N_proc) - N_proc
        for m in np.arange(1, M, 1):
            y_padd = np.concatenate([np_zeros, iq_cf64[m, 0:N_proc]])
            y_fft = np.fft.fft(y_padd)
            corr_function = np.fft.ifft(x_fft.conj() * y_fft)
            corr_function = abs(corr_function)
            corr_function_log = 20 * np.log10(corr_function)

            plt.plot(time_delay_indices, corr_function_log)

        plt.xlim([-100, 100])
        plt.xlabel("Time delay [sample]")
        plt.ylabel("Amplitude [dB]")

    if iq_diff_plot:
        plt.figure(4)
        xcorr = iq_cf64[xcorr_pair[0], :] * \
            np.conjugate(iq_cf64[xcorr_pair[1], :])
        xcorr /= np.max(np.abs(xcorr))
        plt.scatter(xcorr.real, xcorr.imag)

    if show and (td_plot or fd_plot or xcorr_plot or iq_diff_plot):
        plt.show()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze/plot a recorded .iqf IQ frame file or .hcap capture")
    parser.add_argument("file", nargs="?", default=file_name,
                        help="input .iqf or .hcap file (default: %(default)s)")
    parser.add_argument("--frame", type=int, default=None, metavar="K",
                        help=".hcap: analyze frame K (default: the first frame with samples)")
    parser.add_argument("--fs", type=float, default=None,
                        help="sampling frequency in Hz (default: the header's "
                             "sampling_freq, {:g} when it is 0)".format(fs))
    parser.add_argument("--ref-ch", type=int, default=std_ch_ind,
                        help="reference channel index (default: %(default)s)")
    parser.add_argument("--td-ch", type=int, default=td_plot_ch_ind,
                        help="time-domain plot channel (default: %(default)s)")
    parser.add_argument("--xcorr-pair", type=int, nargs=2,
                        default=xcorr_channels, metavar=("CH_A", "CH_B"),
                        help="channel pair for the IQ-difference plot")
    parser.add_argument("--td", dest="td", action="store_true",
                        default=en_td_plot, help="time-domain plot (default)")
    parser.add_argument("--no-td", dest="td", action="store_false")
    parser.add_argument("--fd", action="store_true", default=en_fd_plot,
                        help="frequency-domain plot")
    parser.add_argument("--xcorr", action="store_true",
                        default=en_xcorr_plot, help="cross-correlation plot")
    parser.add_argument("--iq-diff", dest="iq_diff", action="store_true",
                        default=en_iq_diff_plot,
                        help="IQ-difference scatter plot (default)")
    parser.add_argument("--no-iq-diff", dest="iq_diff", action="store_false")
    args = parser.parse_args(argv)

    analyze(fname=args.file, sample_rate=args.fs, ref_ch=args.ref_ch,
            td_plot=args.td, fd_plot=args.fd, xcorr_plot=args.xcorr,
            iq_diff_plot=args.iq_diff, td_channel=args.td_ch,
            xcorr_pair=args.xcorr_pair, frame_index=args.frame)


if __name__ == "__main__":
    main()
