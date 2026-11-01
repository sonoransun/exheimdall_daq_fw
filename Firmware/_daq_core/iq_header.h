/*
 * Desctiption: IQ Frame header definition
 *	For header field description check the corresponding documentation
 *	Project: HeIMDALL DAQ Firmware
 *	Author: Tamás Pető
 */

#define __STDC_FORMAT_MACROS
#include <stdio.h>
#include <inttypes.h>
#include <stddef.h>

#define FRAME_TYPE_DATA  0
#define FRAME_TYPE_DUMMY 1
#define FRAME_TYPE_RAMP  2
#define FRAME_TYPE_CAL   3
#define FRAME_TYPE_TRIGW 4

#define SYNC_WORD 0x2bf7b95a

#define IQ_HEADER_LENGTH 1024

/* Header layout version. v8 names slots inside the previously-zero reserved[]
 * region for RF front-end (amplification) and antenna-orientation telemetry.
 * The struct size (1024 bytes) is unchanged; only reserved[] slots are used. */
#define IQ_HEADER_VERSION 8

/* Named uint32 slot indices inside iq_header_struct.reserved[192].
 * Stamped by delay_sync.py (gain budget + aggregate power) and, for the
 * antenna bearing, relayed from hw_controller.py. See iq_header.py for the
 * Python-side accessors that must stay in lockstep with these offsets. */
#define IQH_RSV_EXT_LNA_GAINS      0   /* [0..31]  external LNA gain, dB x10 */
#define IQH_RSV_TOTAL_GAINS        32  /* [32..63] total system gain, dB x10 */
#define IQH_RSV_SYSTEM_NF_MDB      64  /* [64..95] system noise figure, milli-dB */
#define IQH_RSV_COMPRESSION_FLAGS  96  /* per-channel compression bitmask */
#define IQH_RSV_BIAS_TEE_STATE     97  /* per-channel bias-tee bitmask */
#define IQH_RSV_ANTENNA_AZ_CDEG    98  /* antenna azimuth, centi-deg (0..36000) */
#define IQH_RSV_ANTENNA_EL_CDEG    99  /* antenna elevation, centi-deg (+9000 offset) */
#define IQH_RSV_ROTATOR_STATE      100 /* orientation controller state enum */
#define IQH_RSV_AGG_POWER_MDB      101 /* aggregate channel power, milli-dB (scan objective) */
#define IQH_RSV_BUFFER_OVERRUN_CNT 102 /* cumulative USB ring-buffer overrun events since start,
                                          stamped by rtl_daq; 0 = none/unsupported */

/* Scaling / clustering telemetry slots (still header_version 8). Every slot
 * below is zero unless its own opt-in key or flag is set; a header produced
 * with no new key or flag keeps all of them 0. Byte offset of slot k inside
 * the 1024-byte header = 252 + 4*k. */
#define IQH_RSV_ROUTER_SEQ         103 /* IQ router per-source forwarded-frame counter, u32
                                          wraps (federation_iq_router --stamp-seq) */
#define IQH_RSV_ROUTER_RX_TS_LO    104 /* router receive time, Unix ms, low 32 bits  (--stamp-rx-ts) */
#define IQH_RSV_ROUTER_RX_TS_HI    105 /* router receive time, Unix ms, high 32 bits (--stamp-rx-ts) */
#define IQH_RSV_SOURCE_INSTANCE    106 /* source instance_id + 1; 0 = untagged (router --tag-mode node) */
#define IQH_RSV_SOURCE_KEY_CRC32   107 /* crc32 of the source node key "host:N" (router --tag-mode node) */
#define IQH_RSV_NODE_ID_CRC32      108 /* crc32 of [federation] node_id (delay_sync,
                                          [federation] stamp_node_id = 1) */
#define IQH_RSV_DECIMATOR_DROP_CNT 109 /* cumulative decimator output frames dropped (decimate.out,
                                          [monitoring] stamp_decimator_drops = 1) */
#define IQH_RSV_CAL_QUALITY_MDB    110 /* calibration quality, milli-dB, SIGNED two's complement
                                          (delay_sync, [monitoring] stamp_cal_telemetry = 1) */
#define IQH_RSV_IQ_COMP_CNT        111 /* IQ (amplitude/phase) compensation count (stamp_cal_telemetry) */
#define IQH_RSV_SAMPLE_COMP_CNT    112 /* sample-delay compensation count (stamp_cal_telemetry) */
#define IQH_RSV_RECAL_GEN          113 /* recalibration generation (stamp_cal_telemetry) */
struct iq_frame_struct 
{
	struct iq_header_struct* header;
	uint8_t* payload;
	int payload_size; // Used when the channel buffer is not equal to the CPI size
};

struct iq_frame_struct_32 // Complex float 32 compatible
{
	struct iq_header_struct* header;
	float* payload; //TODO: Modifiy this to CF32 type
	int payload_size; // Used when the channel buffer is not equal to the CPI size
};

struct iq_header_struct {
	uint32_t sync_word;            //Updates: RTL-DAQ - Static   
	uint32_t frame_type;           //Updates: RTL-DAQ - Static
	char hardware_id [16];         //Updates: RTL-DAQ - Static
	uint32_t unit_id;              //Updates: RTL-DAQ - Static
	uint32_t active_ant_chs;       //Updates: RTL-DAQ - Static
	uint32_t ioo_type;             //Updates: RTL-DAQ - Static
	uint64_t rf_center_freq;       //Updates: RTL-DAQ - Static
	uint64_t adc_sampling_freq;    //Updates: RTL-DAQ - Static
	uint64_t sampling_freq;        //Updates: Decimator - Static
	uint32_t cpi_length;           //Updates: Rebuffer / Decimator
	uint64_t time_stamp;           //Updates: RTL-DAQ
	uint32_t daq_block_index;      //Updates: RTL-DAQ      
	uint32_t cpi_index;            //Updates: Decimator
	uint64_t ext_integration_cntr; //Updates: RTL-DAQ   
	uint32_t data_type;            //Updates: Decimator - Static
	uint32_t sample_bit_depth;     //Updates: RTL-DAQ->Decimator - Static
	uint32_t adc_overdrive_flags;  //Updates: RTL-DAQ -> Rebuffer
	uint32_t if_gains[32];         //Updates: RTL-DAQ
	uint32_t delay_sync_flag;      //Updates: Delay synchronizer
	uint32_t iq_sync_flag;         //Updates: Delay synchronizer
	uint32_t sync_state;           //Updates: Delay synchronizer
	uint32_t noise_source_state;   //Updates: RTL-DAQ	
	uint32_t reserved[192];        //Updates: RTL-DAQ - Static
	uint32_t header_version;       //Updates: RTL-DAQ - Static
};

/* Compile-time enforcement of the wire ABI. The header must be exactly 1024
 * bytes with native alignment, and the field offsets are frozen (they are
 * mirrored byte-for-byte by iq_header.py - edit the two files in lockstep). */
_Static_assert(sizeof(struct iq_header_struct) == IQ_HEADER_LENGTH,
               "IQ header ABI broken: struct iq_header_struct must be exactly 1024 bytes");
_Static_assert(offsetof(struct iq_header_struct, reserved) == 252,
               "IQ header ABI broken: reserved[] must start at byte offset 252");
_Static_assert(offsetof(struct iq_header_struct, header_version) == 1020,
               "IQ header ABI broken: header_version must be at byte offset 1020");
_Static_assert((size_t)IQH_RSV_RECAL_GEN < sizeof(((struct iq_header_struct *)0)->reserved) / sizeof(uint32_t),
               "IQ header ABI broken: a named reserved slot lies outside reserved[]");

void dump_iq_header(struct iq_header_struct* iq_header);
int check_sync_word(struct iq_header_struct* iq_header);
