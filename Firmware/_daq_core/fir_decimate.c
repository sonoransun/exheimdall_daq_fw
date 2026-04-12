/*
 *
 * Description
 * Implements FIR filter based decimator with pluggable transport and
 * offload engine support. Uses the Transport Abstraction Layer for
 * data I/O and the Offload Engine Abstraction for FIR processing.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : Tamás Peto
 *
 * Copyright (C) 2018-2026  Tamás Pető
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 */

#include <stdio.h>
#include <stdlib.h>
#include <malloc.h>
#include <unistd.h>
#include <stdbool.h>
#include <string.h>
#include <signal.h>
#include "log.h"
#include "ini.h"
#include "iq_header.h"
#include "sh_mem_util.h"
#include "rtl_daq.h"
#include "transport.h"
#include "offload.h"

static volatile sig_atomic_t sig_exit_flag = 0;

static void shutdown_handler(int sig)
{
    (void)sig;
    sig_exit_flag = 1;
}

static void install_signal_handlers(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = shutdown_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(64,      &sa, NULL);
}

#define DC 127.5
#define INI_FNAME "daq_chain_config.ini"
#define FIR_COEFF "_data_control/fir_coeffs.txt"
#define FATAL_ERR(l) log_fatal(l); return -1;
#define CHK_MALLOC(m) if(m==NULL){log_fatal("Malloc failed, exiting.."); return -1;}
/*
 * This structure stores the configuration parameters,
 * that are loaded from the ini file
 */
typedef struct
{
    int num_ch;
    int cpi_size;
    int cal_size;
    int decimation_ratio;
    int en_filter_reset;
    int tap_size;
    int log_level;
    int instance_id;
    int port_stride;
    /* Offload configuration */
    char decimator_transport[64];
    char fir_engine[64];
} configuration;

/*
 * Ini configuration parser callback function
*/
static int handler(void* conf_struct, const char* section, const char* name,
                   const char* value)
{
    configuration* pconfig = (configuration*) conf_struct;
    #define MATCH(s, n) strcmp(section, s) == 0 && strcmp(name, n) == 0
    if (MATCH("hw", "num_ch"))
    {pconfig->num_ch = atoi(value);}
    else if (MATCH("pre_processing", "cpi_size"))
    {pconfig->cpi_size = atoi(value);}
    else if (MATCH("calibration", "corr_size"))
    {pconfig->cal_size = atoi(value);}
    else if (MATCH("pre_processing", "decimation_ratio"))
    {pconfig->decimation_ratio = atoi(value);}
    else if (MATCH("pre_processing", "en_filter_reset"))
    {pconfig->en_filter_reset = atoi(value);}
    else if(MATCH("pre_processing", "fir_tap_size"))
    {pconfig->tap_size = atoi(value);}
    else if (MATCH("daq", "log_level"))
    {pconfig->log_level = atoi(value);}
    else if (MATCH("federation", "instance_id"))
    {pconfig->instance_id = atoi(value);}
    else if (MATCH("federation", "port_stride"))
    {pconfig->port_stride = atoi(value);}
    else if (MATCH("offload", "decimator_transport"))
    {strncpy(pconfig->decimator_transport, value, sizeof(pconfig->decimator_transport)-1);}
    else if (MATCH("offload", "fir_engine"))
    {strncpy(pconfig->fir_engine, value, sizeof(pconfig->fir_engine)-1);}
    else {return 0;  /* unknown section/name, error */}
    return 0;
}
int main(int argc, char **argv)
/*
 *
 * Parameters:
 * -----------
 * argv[1]: Drop mode [int]
 *
 */
{
    log_set_level(LOG_TRACE);
    install_signal_handlers();
    configuration config;
    config.instance_id = 0;
    config.port_stride = 100;
    strcpy(config.decimator_transport, "shm");
    strcpy(config.fir_engine, "auto");
    bool filter_reset;
    int ch_no,dec;
    int exit_flag=0;
    int active_buff_ind = 0, active_buff_ind_in=0;
    bool drop_mode = true;

    struct iq_header_struct* iq_header;
    uint8_t *input_data_buffer;
    /* Set drop mode from the command prompt*/
    if (argc == 2){drop_mode = atoi(argv[1]);}

    /* Set parameters from the config file*/
    if (ini_parse(INI_FNAME, handler, &config) < 0) {FATAL_ERR("Configuration could not be loaded, exiting ..")}

    ch_no = config.num_ch;
    dec = config.decimation_ratio;
    filter_reset = (bool) config.en_filter_reset;
    log_set_level(config.log_level);
    log_info("Config succesfully loaded from %s",INI_FNAME);
    log_info("Channel number: %d", ch_no);
    log_info("Decimation ratio: %d",dec);
    log_info("CPI size: %d", config.cpi_size);
    log_info("Calibration sample size : %d", config.cal_size);
    log_info("Transport: %s, FIR engine: %s", config.decimator_transport, config.fir_engine);


    /*
    *-------------------------------------
    *  Transport initialization
    *-------------------------------------
    */

    transport_type_t transport_type = transport_type_from_string(config.decimator_transport);

    /* Calculate input buffer size */
    size_t input_buf_size;
    if((config.cpi_size*dec)>=config.cal_size)
        input_buf_size = config.cpi_size*config.num_ch*dec*4*2+IQ_HEADER_LENGTH;
    else
        input_buf_size = config.cal_size*config.num_ch*4*2+IQ_HEADER_LENGTH;

    /* Initialize input transport (consumer) */
    struct transport_handle* input_transport = transport_create(
        "decimator_in", input_buf_size, false,
        FLOW_BACKPRESSURE, config.instance_id, transport_type);
    if (!input_transport) {FATAL_ERR("Failed to create input transport")}

    int succ = transport_init(input_transport);
    if (succ != 0) {FATAL_ERR("Input transport initialization failed")}
    else{log_info("Input transport interface succesfully initialized");}

    /* Initialize output transport (producer) */
    size_t output_buf_size = MAX_IQFRAME_PAYLOAD_SIZE*ch_no*4*2+IQ_HEADER_LENGTH;
    struct transport_handle* output_transport = transport_create(
        "decimator_out", output_buf_size, true,
        drop_mode ? FLOW_DROP : FLOW_BACKPRESSURE,
        config.instance_id, transport_type);
    if (!output_transport) {FATAL_ERR("Failed to create output transport")}

    succ = transport_init(output_transport);
    if(succ != 0){FATAL_ERR("Output transport initialization failed")}
    else{log_info("Output transport interface succesfully initialized");}

    /*
    *-------------------------------------
    *  Offload engine initialization
    *-------------------------------------
    */

    size_t tap_size = config.tap_size;
    offload_engine_t engine_type = offload_engine_from_string(config.fir_engine);

    /* Allocate FIR I/O buffers (platform-agnostic) */
    float* fir_input_buffer_i = malloc(config.cpi_size*dec*sizeof(float));
    float* fir_input_buffer_q = malloc(config.cpi_size*dec*sizeof(float));
    CHK_MALLOC(fir_input_buffer_i)
    CHK_MALLOC(fir_input_buffer_q)

    float* fir_output_buffer_i = malloc(config.cpi_size*sizeof(float));
    float* fir_output_buffer_q = malloc(config.cpi_size*sizeof(float));
    CHK_MALLOC(fir_output_buffer_i)
    CHK_MALLOC(fir_output_buffer_q)

    /* Read FIR coefficients */
    float* fir_coeffs = malloc(tap_size*sizeof(float));
    CHK_MALLOC(fir_coeffs)

    FILE * fir_coeff_fd = fopen(FIR_COEFF, "r");
    if (fir_coeff_fd == NULL) {FATAL_ERR("Failed to open FIR coefficient file")}
    int k=0;
    while (fscanf(fir_coeff_fd, "%f", &fir_coeffs[k++]) != EOF);
    fclose(fir_coeff_fd);
    if (k-1==(int)tap_size){log_info("FIR filter coefficients initialized, tap size: %d",k-1);}
    else{FATAL_ERR("FIR filter coefficients initialization failed")}

    /* Create and initialize FIR offload engine */
    struct fir_engine* fir_eng = fir_engine_create(engine_type);
    if (!fir_eng) {FATAL_ERR("Failed to create FIR engine")}

    succ = fir_eng->init(fir_eng, fir_coeffs, tap_size, dec, config.cpi_size, ch_no);
    if (succ != 0) {FATAL_ERR("FIR engine initialization failed")}

    /* Create convert engine for U8->F32 conversion */
    struct convert_engine* conv_eng = convert_engine_create(engine_type);
    if (!conv_eng) {FATAL_ERR("Failed to create convert engine")}

    uint64_t cpi_index=-1;
    void* frame_ptr;
    void* input_frame_ptr;
    void* output_frame_ptr;

    /* Main Processing loop*/
    while(!exit_flag && !sig_exit_flag){

        /* Acquire data buffer from input transport */
        active_buff_ind_in = transport_get_read_buf(input_transport, &input_frame_ptr);
        if (active_buff_ind_in < 0 ){exit_flag = 1; break;}
        if (active_buff_ind_in == TERMINATE) {exit_flag = TERMINATE; break;}
        iq_header = (struct iq_header_struct*) input_frame_ptr;
        input_data_buffer = ((uint8_t *) input_frame_ptr) + IQ_HEADER_LENGTH/sizeof(uint8_t);
        CHK_SYNC_WORD(check_sync_word(iq_header));

        cpi_index ++;

        /* Acquire buffer from the output transport */
        active_buff_ind = transport_get_write_buf(output_transport, &output_frame_ptr);
        switch(active_buff_ind)
        {
            case 0:
            case 1:
                log_trace("--> Frame received: type: %d, daq ind:[%d]",iq_header->frame_type, iq_header->daq_block_index);
                frame_ptr = output_frame_ptr;
                float* output_data_buffer = ((float *) output_frame_ptr) + IQ_HEADER_LENGTH/sizeof(float);
                /* Place IQ header into the output buffer*/
                memcpy(frame_ptr, iq_header, 1024);

                /* Update header fields */
                iq_header = (struct iq_header_struct*) frame_ptr;
                iq_header->data_type = 3; // Data type is decimated IQ
                iq_header->sample_bit_depth = 32; // Complex float 32
                iq_header->cpi_index = cpi_index;

                if (iq_header->frame_type==FRAME_TYPE_DATA && dec > 1)
                {
                    iq_header->sampling_freq = iq_header->adc_sampling_freq / (uint64_t) dec;
                    iq_header->cpi_length = (uint32_t) iq_header->cpi_length/dec;

                    /* Perform filtering on data type frames*/
                    if (iq_header->cpi_length > 0)
                    {
                        if (filter_reset) {
                            for(int ch_index=0; ch_index<(int)iq_header->active_ant_chs; ch_index++)
                                fir_eng->reset(fir_eng, ch_index);
                        }

                        for(int ch_index=0;ch_index<(int)iq_header->active_ant_chs;ch_index++)
                        {
                            /* De-interleave input data: U8 -> float I/Q */
                            conv_eng->u8_to_f32_deinterleave(conv_eng,
                                input_data_buffer,
                                fir_input_buffer_i, fir_input_buffer_q,
                                iq_header->cpi_length*dec);

                            /* Perform FIR decimation via offload engine */
                            fir_eng->decimate(fir_eng, ch_index,
                                fir_input_buffer_i, fir_input_buffer_q,
                                fir_output_buffer_i, fir_output_buffer_q,
                                iq_header->cpi_length*dec);

                            /* Re-interleave output data */
                            for(int sample_index=0; sample_index<(int)iq_header->cpi_length; sample_index++)
                            {
                                output_data_buffer[2*sample_index]   = fir_output_buffer_i[sample_index];
                                output_data_buffer[2*sample_index+1] = fir_output_buffer_q[sample_index];
                            }

                            input_data_buffer  += 2*iq_header->cpi_length*dec;
                            output_data_buffer += 2*iq_header->cpi_length;
                        }
                    }
                }
                else if (iq_header->frame_type==FRAME_TYPE_CAL || dec == 1)
                {
                    iq_header->sampling_freq = iq_header->adc_sampling_freq;
                    iq_header->cpi_length = (uint32_t) iq_header->cpi_length;

                    /* Convert cint8 to cfloat32 without filtering and decimation on cal type frames */
                    conv_eng->u8_to_f32_interleaved(conv_eng,
                        input_data_buffer, output_data_buffer,
                        iq_header->cpi_length * iq_header->active_ant_chs);
                }
                log_trace("<--Transfering frame type: %d, daq ind:[%d]",iq_header->frame_type, iq_header->daq_block_index);
                transport_submit_write(output_transport, active_buff_ind);
                break;
            case 3:
                /* Frame drop*/
                break;
            default:
                log_error("Failed to acquire free buffer");
                exit_flag = 1;
        }
        transport_release_read(input_transport, active_buff_ind_in);
    } // End of the main processing loop
    if (sig_exit_flag)
        log_info("Received shutdown signal");
    else
        error_code_log(exit_flag);
    transport_send_terminate(output_transport);
    sleep(3);
    transport_destroy(output_transport);
    transport_destroy(input_transport);
    free(output_transport);
    free(input_transport);
    if (fir_eng) fir_eng->destroy(fir_eng);
    if (conv_eng) free(conv_eng);
    free(fir_input_buffer_i);
    free(fir_input_buffer_q);
    free(fir_output_buffer_i);
    free(fir_output_buffer_q);
    free(fir_coeffs);
    log_info("Decimator exited");
    return 0;
}
