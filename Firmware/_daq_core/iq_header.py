from struct import Struct, pack, unpack
import logging
import sys
"""
    Desctiption: IQ Frame header definition
    For header field description check the corresponding documentation
    Total length: 1024 byte
    Project: HeIMDALL DAQ Firmware
    Author: Tamás Pető
"""

# Precompiled header codec (module level, built once - this runs per frame in
# the delay_sync and hw_controller hot loops).
# NOTE: The format uses NATIVE alignment on purpose: the C struct in
# iq_header.h relies on implicit padding around the uint64 fields
# (the explicit little-endian '<' variant would be 1016 bytes). Do not
# add a byte-order prefix without adding explicit padding.
HEADER_FORMAT = "II16sIIIQQQIQIIQIII" + "I"*32 + "IIII" + "I"*192 + "I"
HEADER_STRUCT = Struct(HEADER_FORMAT)
# Fail fast on exotic ABIs where native alignment diverges from the wire format
assert HEADER_STRUCT.size == 1024, \
    "IQ header ABI mismatch: native struct size is {:d}, expected 1024. " \
    "This platform's alignment rules are incompatible with iq_header.h".format(HEADER_STRUCT.size)


class IQHeader():

    FRAME_TYPE_DATA  = 0
    FRAME_TYPE_DUMMY = 1
    FRAME_TYPE_RAMP  = 2
    FRAME_TYPE_CAL   = 3
    FRAME_TYPE_TRIGW = 4
    
    SYNC_WORD = 0x2bf7b95a

    # Header layout version. v8 names slots inside the previously-zero reserved[]
    # region for RF front-end (amplification) + antenna-orientation telemetry.
    HEADER_VERSION = 8

    # Named uint32 slot indices inside reserved[192] (lockstep with iq_header.h)
    RSV_EXT_LNA_GAINS = 0      # [0:32]  external LNA gain, dB x10
    RSV_TOTAL_GAINS = 32       # [32:64] total system gain, dB x10
    RSV_SYSTEM_NF_MDB = 64     # [64:96] system noise figure, milli-dB
    RSV_COMPRESSION_FLAGS = 96 # per-channel compression bitmask
    RSV_BIAS_TEE_STATE = 97    # per-channel bias-tee bitmask
    RSV_ANTENNA_AZ_CDEG = 98   # antenna azimuth, centi-deg (0..36000)
    RSV_ANTENNA_EL_CDEG = 99   # antenna elevation, centi-deg (+9000 offset)
    RSV_ROTATOR_STATE = 100    # orientation controller state enum
    RSV_AGG_POWER_MDB = 101    # aggregate channel power, milli-dB (scan objective)
    RSV_BUFFER_OVERRUN_CNT = 102 # cumulative USB ring-buffer overrun events since start
                                 # (stamped by rtl_daq; 0 = none/unsupported)

    def __init__(self):

        self.logger = logging.getLogger(__name__)
        self.header_size = 1024 # size in bytes
        # Number of 32-bit reserved WORDS (192 words = 768 bytes). The historic
        # attribute name 'reserved_bytes' is misleading but preserved for
        # backward compatibility; 'reserved_words' is the accurate alias.
        self.reserved_bytes = 192
        self.reserved_words = 192

        self.sync_word=self.SYNC_WORD        # uint32_t        
        self.frame_type=0                    # uint32_t 
        self.hardware_id=""                  # char [16]
        self.unit_id=0                       # uint32_t 
        self.active_ant_chs=0                # uint32_t 
        self.ioo_type=0                      # uint32_t 
        self.rf_center_freq=0                # uint64_t 
        self.adc_sampling_freq=0             # uint64_t 
        self.sampling_freq=0                 # uint64_t 
        self.cpi_length=0                    # uint32_t 
        self.time_stamp=0                    # uint64_t 
        self.daq_block_index=0               # uint32_t
        self.cpi_index=0                     # uint32_t 
        self.ext_integration_cntr=0          # uint64_t 
        self.data_type=0                     # uint32_t 
        self.sample_bit_depth=0              # uint32_t 
        self.adc_overdrive_flags=0           # uint32_t 
        self.if_gains=[0]*32                 # uint32_t x 32
        self.delay_sync_flag=0               # uint32_t
        self.iq_sync_flag=0                  # uint32_t
        self.sync_state=0                    # uint32_t
        self.noise_source_state=0            # uint32_t        
        self.reserved=[0]*self.reserved_bytes# uint32_t x reserverd_bytes
        self.header_version=0                # uint32_t 

    def decode_header(self, iq_header_byte_array):
        """
            Unpack,decode and store the content of the iq header
        """
        iq_header_list = HEADER_STRUCT.unpack(iq_header_byte_array)

        self.sync_word            = iq_header_list[0]
        self.frame_type           = iq_header_list[1]
        self.hardware_id          = iq_header_list[2].decode()
        self.unit_id              = iq_header_list[3]
        self.active_ant_chs       = iq_header_list[4]
        self.ioo_type             = iq_header_list[5]
        self.rf_center_freq       = iq_header_list[6]
        self.adc_sampling_freq    = iq_header_list[7]
        self.sampling_freq        = iq_header_list[8]
        self.cpi_length           = iq_header_list[9]
        self.time_stamp           = iq_header_list[10]
        self.daq_block_index      = iq_header_list[11]
        self.cpi_index            = iq_header_list[12]
        self.ext_integration_cntr = iq_header_list[13]
        self.data_type            = iq_header_list[14]
        self.sample_bit_depth     = iq_header_list[15]
        self.adc_overdrive_flags  = iq_header_list[16]
        self.if_gains             = iq_header_list[17:49]
        self.delay_sync_flag      = iq_header_list[49]
        self.iq_sync_flag         = iq_header_list[50]
        self.sync_state           = iq_header_list[51]
        self.noise_source_state   = iq_header_list[52]
        self.reserved             = list(iq_header_list[53:53+self.reserved_bytes])
        self.header_version       = iq_header_list[52+self.reserved_bytes+1]

    def encode_header(self):
        """
            Pack the iq header information into a byte array
            (single precompiled struct pack - runs once per frame)
        """
        fields = [self.sync_word, self.frame_type,
                  self.hardware_id.encode(),  # '16s' zero-pads/truncates to 16 bytes
                  self.unit_id, self.active_ant_chs, self.ioo_type, self.rf_center_freq, self.adc_sampling_freq,
                  self.sampling_freq, self.cpi_length, self.time_stamp, self.daq_block_index, self.cpi_index,
                  self.ext_integration_cntr, self.data_type, self.sample_bit_depth, self.adc_overdrive_flags]
        fields += [self.if_gains[m] for m in range(32)]
        fields += [self.delay_sync_flag, self.iq_sync_flag, self.sync_state, self.noise_source_state]
        fields += [(int(self.reserved[m]) & 0xFFFFFFFF) if m < len(self.reserved) else 0
                   for m in range(self.reserved_words)]
        fields.append(self.header_version)
        return HEADER_STRUCT.pack(*fields)

    def dump_header(self):
        """
            Prints out the content of the header in human readable format
        """
        self.logger.info("Sync word: {:d}".format(self.sync_word))        
        self.logger.info("Header version: {:d}".format(self.header_version))        
        self.logger.info("Frame type: {:d}".format(self.frame_type))
        self.logger.info("Hardware ID: {:16}".format(self.hardware_id))
        self.logger.info("Unit ID: {:d}".format(self.unit_id))
        self.logger.info("Active antenna channels: {:d}".format(self.active_ant_chs))
        self.logger.info("Illuminator type: {:d}".format(self.ioo_type))
        self.logger.info("RF center frequency: {:.2f} MHz".format(self.rf_center_freq/10**6))
        self.logger.info("ADC sampling frequency: {:.2f} MHz".format(self.adc_sampling_freq/10**6))
        self.logger.info("IQ sampling frequency {:.2f} MHz".format(self.sampling_freq/10**6))
        self.logger.info("CPI length: {:d}".format(self.cpi_length))
        self.logger.info("Unix Epoch timestamp: {:d}".format(self.time_stamp))
        self.logger.info("DAQ block index: {:d}".format(self.daq_block_index))
        self.logger.info("CPI index: {:d}".format(self.cpi_index))
        self.logger.info("Extended integration counter {:d}".format(self.ext_integration_cntr))
        self.logger.info("Data type: {:d}".format(self.data_type))
        self.logger.info("Sample bit depth: {:d}".format(self.sample_bit_depth))
        self.logger.info("ADC overdrive flags: {:d}".format(self.adc_overdrive_flags))    
        for m in range(32):
            self.logger.info("Ch: {:d} IF gain: {:.1f} dB".format(m, self.if_gains[m]/10))
        self.logger.info("Delay sync  flag: {:d}".format(self.delay_sync_flag))
        self.logger.info("IQ sync  flag: {:d}".format(self.iq_sync_flag))
        self.logger.info("Sync state: {:d}".format(self.sync_state))
        self.logger.info("Noise source state: {:d}".format(self.noise_source_state))
    
    # ---- v8 reserved-region accessors (RF front-end + orientation telemetry) ----
    # Wire encoding: every slot is a uint32. Signed quantities (gains, NF,
    # power, bearing) are stored two's-complement; the getters sign-extend so
    # consumers see the original signed value. Setters are unchanged (mask).
    @staticmethod
    def _sx32(value):
        """Sign-extend a uint32 reserved-slot value to a Python int."""
        value = int(value) & 0xFFFFFFFF
        return value - (1 << 32) if value >= (1 << 31) else value

    def _set_reserved_block(self, start, values):
        for i, v in enumerate(values):
            if start + i < self.reserved_words:
                self.reserved[start + i] = int(v) & 0xFFFFFFFF

    def _get_reserved_block(self, start, count, signed=True):
        vals = self.reserved[start:start + count]
        if signed:
            return [self._sx32(v) for v in vals]
        return [int(v) & 0xFFFFFFFF for v in vals]

    def set_ext_lna_gains(self, gains_tenths):
        """External LNA gains, tenths of dB, per channel."""
        self._set_reserved_block(self.RSV_EXT_LNA_GAINS, gains_tenths)

    def get_ext_lna_gains(self, num_ch=32):
        """External LNA gains, tenths of dB (signed), per channel."""
        return self._get_reserved_block(self.RSV_EXT_LNA_GAINS, num_ch)

    def set_total_system_gains(self, gains_tenths):
        """Total system gains, tenths of dB, per channel."""
        self._set_reserved_block(self.RSV_TOTAL_GAINS, gains_tenths)

    def get_total_system_gains(self, num_ch=32):
        """Total system gains, tenths of dB (signed), per channel."""
        return self._get_reserved_block(self.RSV_TOTAL_GAINS, num_ch)

    def set_system_nf_mdb(self, nf_mdb):
        """System noise figure, milli-dB, per channel."""
        self._set_reserved_block(self.RSV_SYSTEM_NF_MDB, nf_mdb)

    def get_system_nf_mdb(self, num_ch=32):
        """System noise figure, milli-dB (signed), per channel."""
        return self._get_reserved_block(self.RSV_SYSTEM_NF_MDB, num_ch)

    def set_compression_flags(self, flags):
        self.reserved[self.RSV_COMPRESSION_FLAGS] = int(flags) & 0xFFFFFFFF

    def get_compression_flags(self):
        """Per-channel compression bitmask (unsigned)."""
        return int(self.reserved[self.RSV_COMPRESSION_FLAGS]) & 0xFFFFFFFF

    def set_bias_tee_state(self, flags):
        self.reserved[self.RSV_BIAS_TEE_STATE] = int(flags) & 0xFFFFFFFF

    def get_bias_tee_state(self):
        """Per-channel bias-tee bitmask (unsigned)."""
        return int(self.reserved[self.RSV_BIAS_TEE_STATE]) & 0xFFFFFFFF

    def set_antenna_bearing(self, az_deg, el_deg):
        """Store antenna azimuth/elevation as centi-degrees (elevation +90 deg offset)."""
        self.reserved[self.RSV_ANTENNA_AZ_CDEG] = int(round(az_deg * 100.0)) & 0xFFFFFFFF
        self.reserved[self.RSV_ANTENNA_EL_CDEG] = int(round((el_deg + 90.0) * 100.0)) & 0xFFFFFFFF

    def get_antenna_bearing(self):
        """Return (azimuth_deg, elevation_deg) decoded from the reserved slots."""
        az = self._sx32(self.reserved[self.RSV_ANTENNA_AZ_CDEG]) / 100.0
        el = self._sx32(self.reserved[self.RSV_ANTENNA_EL_CDEG]) / 100.0 - 90.0
        return (az, el)

    def set_rotator_state(self, state):
        self.reserved[self.RSV_ROTATOR_STATE] = int(state) & 0xFFFFFFFF

    def get_rotator_state(self):
        return int(self.reserved[self.RSV_ROTATOR_STATE]) & 0xFFFFFFFF

    def set_aggregate_power_mdb(self, mdb):
        self.reserved[self.RSV_AGG_POWER_MDB] = int(mdb) & 0xFFFFFFFF

    def get_aggregate_power_mdb(self):
        """Aggregate channel power, milli-dB (signed)."""
        return self._sx32(self.reserved[self.RSV_AGG_POWER_MDB])

    def set_buffer_overrun_cnt(self, count):
        """Cumulative USB ring-buffer overrun events since start (stamped by rtl_daq)."""
        self.reserved[self.RSV_BUFFER_OVERRUN_CNT] = int(count) & 0xFFFFFFFF

    def get_buffer_overrun_cnt(self):
        """Cumulative USB ring-buffer overrun count (unsigned; 0 = none/unsupported)."""
        return int(self.reserved[self.RSV_BUFFER_OVERRUN_CNT]) & 0xFFFFFFFF

    def check_sync_word(self):
        """
            Check the sync word of the header
        """
        if self.sync_word != self.SYNC_WORD:
            return -1
        else:
            return 0
