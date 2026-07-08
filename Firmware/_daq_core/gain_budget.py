"""
    RF Front-End Gain / Noise-Figure Budget

    Pure, hardware-free link-budget math for amplified receivers: total system
    gain, cascaded noise figure (Friis), and P1dB compression headroom for an
    external-LNA / inline-amplifier chain placed ahead of the RTL-SDR tuner.

    External LNA gain is CONTINUOUS dB and is kept entirely separate from the
    R820T `valid_gains` quantizer and the per-frame `if_gains[]` field (which
    drives the gain-lock state machine in hw_controller.py). Nothing in this
    module touches hardware; every function is unit-testable on its own.

    Config is parsed from the optional [amplification] section (disabled by
    default), mirroring signal_scheduler.ScheduleParser.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Sentinel meaning "compression not modelled for this channel".
P1DB_NOT_MODELLED = 99.0


# ---------------------------------------------------------------------------
# dB helpers
# ---------------------------------------------------------------------------
def db_to_lin(x_db):
    """Convert a dB (power-ratio) value to linear."""
    return 10.0 ** (x_db / 10.0)


def lin_to_db(x_lin):
    """Convert a linear power ratio to dB. Guards against non-positive input."""
    if x_lin <= 0.0:
        return float('-inf')
    import math
    return 10.0 * math.log10(x_lin)


# ---------------------------------------------------------------------------
# Gain / noise-figure math
# ---------------------------------------------------------------------------
def total_system_gain_db(antenna_gain_dbi, feed_loss_db, ext_lna_gain_db, tuner_gain_db):
    """Signal-path gain from the incident wavefront to the ADC, in dB.

    antenna_gain_dbi enters as a signal-only term; feed loss is subtracted;
    external LNA and tuner IF gains are added.
    """
    return antenna_gain_dbi - feed_loss_db + ext_lna_gain_db + tuner_gain_db


def friis_nf_db(stages):
    """Cascaded noise figure (dB) of an ordered list of stages.

    `stages` is a list of (gain_db, nf_db) tuples ordered from the antenna port
    toward the ADC. A passive loss of L dB enters as (gain_db=-L, nf_db=L).

    Implements F = F1 + (F2-1)/G1 + (F3-1)/(G1*G2) + ...  in the linear domain.
    Returns the system noise figure in dB. An empty list returns 0 dB.
    """
    if not stages:
        return 0.0
    f_total = 0.0
    gain_product = 1.0
    for i, (gain_db, nf_db) in enumerate(stages):
        f_stage = db_to_lin(nf_db)
        if i == 0:
            f_total = f_stage
        else:
            f_total += (f_stage - 1.0) / gain_product
        gain_product *= db_to_lin(gain_db)
    return lin_to_db(f_total)


def system_nf_db(feed_loss_db, ext_lna_gain_db, ext_lna_nf_db, tuner_gain_db, tuner_nf_db):
    """Cascaded NF of feed-loss -> external LNA -> tuner, in dB.

    The antenna element gain is a signal-only term (not a Friis noise stage),
    so it does not appear here.
    """
    stages = [
        (-feed_loss_db, feed_loss_db),     # passive feed loss
        (ext_lna_gain_db, ext_lna_nf_db),  # external LNA
        (tuner_gain_db, tuner_nf_db),      # RTL-SDR tuner
    ]
    return friis_nf_db(stages)


def p1db_headroom_db(expected_input_dbm, gain_before_output_db, p1db_out_dbm):
    """Headroom (dB) between the estimated stage output level and its P1dB.

    Positive => operating below compression; <= margin => approaching P1dB.
    `gain_before_output_db` is the gain from the antenna port up to (and
    including) the amplifier whose output-referred P1dB is `p1db_out_dbm`.
    """
    estimated_output_dbm = expected_input_dbm + gain_before_output_db
    return p1db_out_dbm - estimated_output_dbm


# ---------------------------------------------------------------------------
# Frequency-dependent LNA response table
# ---------------------------------------------------------------------------
def parse_lna_freq_table(table_str):
    """Parse "fMHz:gain_db:nf_db;fMHz:gain_db:nf_db;..." into a sorted list.

    Returns a list of (freq_hz, gain_db, nf_db) tuples sorted by frequency, or
    an empty list for an empty/blank string.
    """
    table = []
    if not table_str or not table_str.strip():
        return table
    for entry in table_str.split(';'):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(':')
        try:
            f_hz = float(parts[0]) * 1e6
            gain = float(parts[1]) if len(parts) > 1 else 0.0
            nf = float(parts[2]) if len(parts) > 2 else 0.0
            table.append((f_hz, gain, nf))
        except (ValueError, IndexError):
            logger.warning("Malformed lna_freq_table entry '%s', skipping", entry)
    table.sort(key=lambda t: t[0])
    return table


def lookup_lna_response(table, freq_hz):
    """Nearest-frequency (gain_db, nf_db) from a parsed lna_freq_table.

    Empty table returns (0.0, 0.0) so a flat/undefined response is neutral.
    """
    if not table:
        return (0.0, 0.0)
    best = min(table, key=lambda t: abs(t[0] - freq_hz))
    return (best[1], best[2])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _parse_float_list(s, n, default):
    """Parse a comma-separated float list of expected length n.

    On length mismatch or parse error, returns [default]*n and logs a warning.
    """
    try:
        vals = [float(x.strip()) for x in s.split(',') if x.strip() != '']
    except ValueError:
        logger.warning("Malformed float list '%s', using default %s", s, default)
        return [default] * n
    if len(vals) != n:
        logger.warning("Expected %d values but got %d in '%s', using default", n, len(vals), s)
        return [default] * n
    return vals


@dataclass
class RfFrontendConfig:
    """Parsed [amplification] configuration (per-channel arrays sized to M)."""
    enabled: bool = False
    ext_lna_gains_db: list = field(default_factory=list)
    ext_lna_nf_db: list = field(default_factory=list)
    ext_lna_p1db_dbm: list = field(default_factory=list)
    tuner_nf_db: float = 3.5
    expected_input_dbm: float = -60.0
    p1db_margin_db: float = 3.0
    max_total_gain_db: float = 80.0
    en_bias_tee_runtime: bool = False
    bias_tee_state: list = field(default_factory=list)
    lna_freq_table: list = field(default_factory=list)

    def channel_budget(self, ch, tuner_gain_db, antenna_profile=None, freq_hz=None):
        """Compute the link budget for one channel.

        Returns a dict: total_gain_db, system_nf_db, p1db_headroom_db,
        compressed (bool), over_max_gain (bool).
        """
        ext_gain = self.ext_lna_gains_db[ch] if ch < len(self.ext_lna_gains_db) else 0.0
        ext_nf = self.ext_lna_nf_db[ch] if ch < len(self.ext_lna_nf_db) else 0.0
        p1db = self.ext_lna_p1db_dbm[ch] if ch < len(self.ext_lna_p1db_dbm) else P1DB_NOT_MODELLED

        # Optional frequency-dependent LNA response (adds to the static per-ch values)
        if self.lna_freq_table and freq_hz is not None:
            fg, fn = lookup_lna_response(self.lna_freq_table, freq_hz)
            ext_gain += fg
            ext_nf = ext_nf + fn if fn else ext_nf

        antenna_gain = antenna_profile.element_gain_dbi if antenna_profile else 0.0
        feed_loss = antenna_profile.feed_loss_db if antenna_profile else 0.0

        total_gain = total_system_gain_db(antenna_gain, feed_loss, ext_gain, tuner_gain_db)
        nf = system_nf_db(feed_loss, ext_gain, ext_nf, tuner_gain_db, self.tuner_nf_db)

        # Compression headroom at the external-LNA output (only if P1dB modelled)
        compressed = False
        headroom = float('inf')
        if p1db < P1DB_NOT_MODELLED:
            headroom = p1db_headroom_db(self.expected_input_dbm, ext_gain - feed_loss, p1db)
            compressed = headroom <= self.p1db_margin_db

        return {
            "total_gain_db": total_gain,
            "system_nf_db": nf,
            "p1db_headroom_db": headroom,
            "compressed": compressed,
            "over_max_gain": total_gain > self.max_total_gain_db,
        }


class RfFrontendParser:
    """Parse an RfFrontendConfig from an INI [amplification] section."""

    @staticmethod
    def from_ini_section(parser, num_ch, section_name="amplification"):
        """Return an RfFrontendConfig, or None if absent/disabled."""
        if not parser.has_section(section_name):
            return None
        if not parser.getint(section_name, 'en_amplification', fallback=0):
            return None

        g = parser.get(section_name, 'ext_lna_gains_db', fallback='')
        nf = parser.get(section_name, 'ext_lna_nf_db', fallback='')
        p1 = parser.get(section_name, 'ext_lna_p1db_dbm', fallback='')
        bias = parser.get(section_name, 'bias_tee_state', fallback='')

        cfg = RfFrontendConfig(
            enabled=True,
            ext_lna_gains_db=_parse_float_list(g, num_ch, 0.0),
            ext_lna_nf_db=_parse_float_list(nf, num_ch, 0.0),
            ext_lna_p1db_dbm=_parse_float_list(p1, num_ch, P1DB_NOT_MODELLED),
            tuner_nf_db=parser.getfloat(section_name, 'tuner_nf_db', fallback=3.5),
            expected_input_dbm=parser.getfloat(section_name, 'expected_input_dbm', fallback=-60.0),
            p1db_margin_db=parser.getfloat(section_name, 'p1db_margin_db', fallback=3.0),
            max_total_gain_db=parser.getfloat(section_name, 'max_total_gain_db', fallback=80.0),
            en_bias_tee_runtime=bool(parser.getint(section_name, 'en_bias_tee_runtime', fallback=0)),
            bias_tee_state=[int(round(x)) for x in _parse_float_list(bias, num_ch, 0.0)],
            lna_freq_table=parse_lna_freq_table(parser.get(section_name, 'lna_freq_table', fallback='')),
        )
        return cfg
