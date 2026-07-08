"""
    Antenna Profile

    Describes the RF characteristics and mechanical mounting of the antenna
    element(s) feeding the receiver chain: element gain, beamwidth, polarization,
    passive feed losses, and a mechanical->electrical boresight offset.

    The profile is a shared input to two subsystems:
      - gain_budget.py       : element gain (dBi) and cable/connector losses are
                               terms in the total-gain and cascaded-noise-figure budget.
      - orientation_controller.py : the boresight offsets correct a commanded
                               bearing for the antenna's mechanical mounting skew.

    No hardware access. Parsed from the optional [antenna] config section
    (disabled by default), mirroring signal_scheduler.ScheduleParser.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Accepted polarization identifiers (kept in sync with ini_checker.valid_polarizations)
VALID_POLARIZATIONS = ['vertical', 'horizontal', 'rhcp', 'lhcp', 'slant45', 'dual']


@dataclass
class AntennaProfile:
    """RF + mechanical description of the antenna element(s)."""
    name: str = "isotropic"
    element_gain_dbi: float = 0.0
    beamwidth_az_deg: float = 360.0
    beamwidth_el_deg: float = 360.0
    polarization: str = "vertical"
    cable_loss_db: float = 0.0
    connector_loss_db: float = 0.0
    boresight_az_offset_deg: float = 0.0
    boresight_el_offset_deg: float = 0.0

    @property
    def feed_loss_db(self):
        """Total passive loss between the antenna element and the tuner input."""
        return self.cable_loss_db + self.connector_loss_db

    def apply_boresight(self, az_deg, el_deg):
        """Correct a commanded (electrical) bearing for mechanical mounting skew.

        Returns the mechanical set-point the rotator should be driven to so that
        the antenna's electrical boresight points at the requested az/el.
        """
        return (az_deg + self.boresight_az_offset_deg,
                el_deg + self.boresight_el_offset_deg)

    def to_status(self):
        """Compact dict for status/telemetry reporting."""
        return {
            "name": self.name,
            "element_gain_dbi": self.element_gain_dbi,
            "beamwidth_az_deg": self.beamwidth_az_deg,
            "beamwidth_el_deg": self.beamwidth_el_deg,
            "polarization": self.polarization,
            "feed_loss_db": self.feed_loss_db,
            "boresight_az_offset_deg": self.boresight_az_offset_deg,
            "boresight_el_offset_deg": self.boresight_el_offset_deg,
        }


class AntennaProfileParser:
    """Parse an AntennaProfile from an INI [antenna] section or JSON."""

    @staticmethod
    def from_ini_section(parser, section_name="antenna"):
        """Return an AntennaProfile, or None if the section is absent/disabled."""
        if not parser.has_section(section_name):
            return None
        if not parser.getint(section_name, 'en_antenna_profile', fallback=0):
            return None

        pol = parser.get(section_name, 'polarization', fallback='vertical').strip().lower()
        if pol not in VALID_POLARIZATIONS:
            logger.warning("Unknown polarization '%s', defaulting to 'vertical'", pol)
            pol = 'vertical'

        return AntennaProfile(
            name=parser.get(section_name, 'profile_name', fallback='isotropic'),
            element_gain_dbi=parser.getfloat(section_name, 'element_gain_dbi', fallback=0.0),
            beamwidth_az_deg=parser.getfloat(section_name, 'beamwidth_az_deg', fallback=360.0),
            beamwidth_el_deg=parser.getfloat(section_name, 'beamwidth_el_deg', fallback=360.0),
            polarization=pol,
            cable_loss_db=parser.getfloat(section_name, 'cable_loss_db', fallback=0.0),
            connector_loss_db=parser.getfloat(section_name, 'connector_loss_db', fallback=0.0),
            boresight_az_offset_deg=parser.getfloat(section_name, 'boresight_az_offset_deg', fallback=0.0),
            boresight_el_offset_deg=parser.getfloat(section_name, 'boresight_el_offset_deg', fallback=0.0),
        )

    @staticmethod
    def from_json(json_str):
        """Parse an AntennaProfile from a JSON string."""
        data = json.loads(json_str)
        pol = str(data.get('polarization', 'vertical')).strip().lower()
        if pol not in VALID_POLARIZATIONS:
            pol = 'vertical'
        return AntennaProfile(
            name=data.get('name', data.get('profile_name', 'isotropic')),
            element_gain_dbi=float(data.get('element_gain_dbi', 0.0)),
            beamwidth_az_deg=float(data.get('beamwidth_az_deg', 360.0)),
            beamwidth_el_deg=float(data.get('beamwidth_el_deg', 360.0)),
            polarization=pol,
            cable_loss_db=float(data.get('cable_loss_db', 0.0)),
            connector_loss_db=float(data.get('connector_loss_db', 0.0)),
            boresight_az_offset_deg=float(data.get('boresight_az_offset_deg', 0.0)),
            boresight_el_offset_deg=float(data.get('boresight_el_offset_deg', 0.0)),
        )
