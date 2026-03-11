/*
 * 
 * Description :
 * IQ frame Ethernet server
 * 
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : Tamas Peto
 * 
 * Copyright (C) 2018-2020  Tamás Pető
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
#include <unistd.h>
#include <stdio.h>
#include <string.h>

#include "eth_server.h"
#include "ini.h"
#include "log.h"
#include "sh_mem_util.h"
#include "iq_header.h"
#include "rtl_daq.h"
#include "transport.h"
#define INI_FNAME "daq_chain_config.ini" 

#define FATAL_ERR(l) log_fatal(l); return -1;

/*
 * This structure stores the configuration parameters, 
 * that are loaded from the ini file
 */ 
typedef struct
{
    int num_ch;
    int cpi_size;
    int log_level;
    int instance_id;
    int port_stride;
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
    {
        pconfig->num_ch = atoi(value);
    } 
    else if (MATCH("pre_processing", "cpi_size")) 
    {
        pconfig->cpi_size = atoi(value);
    }
    else if (MATCH("daq", "log_level"))
    {
        pconfig->log_level = atoi(value);
    }
    else if (MATCH("federation", "instance_id"))
    {
        pconfig->instance_id = atoi(value);
    }
    else if (MATCH("federation", "port_stride"))
    {
        pconfig->port_stride = atoi(value);
    }
    else {
        return 0;  /* unknown section/name, error */
    }
    return 0;
}

int send_iq_frame(struct iq_frame_struct_32* iq_frame, int socket)
{
    int transfer_size =iq_frame->payload_size*sizeof(*iq_frame->payload)*2+IQ_HEADER_LENGTH;
	
	// Sending header
	int size = send(socket, iq_frame->header, sizeof(struct iq_header_struct), 0);
	// Sending payload
	if (iq_frame->payload_size !=0){
		size += send(socket, iq_frame->payload, transfer_size-IQ_HEADER_LENGTH, 0);}
	// Check transfer
	if(size != transfer_size){log_error("Ethernet transfer failed"); return -1;}	
	//usleep(50000); // In some cases it is required to fully finish the sending from OS buffers
	return 0;
}

int main(int argc, char* argv[])
{
    log_set_level(LOG_TRACE);
    configuration config;
    config.instance_id = 0;
    config.port_stride = 100;
	int ret = 0;
    int active_buff_ind;
    char eth_cmd[1024]; // Ethernet command buffer   
	   
    /* Set parameters from the config file*/
    if (ini_parse(INI_FNAME, handler, &config) < 0)
    {FATAL_ERR("Configuration could not be loaded, exiting ..")}    
    
	log_set_level(config.log_level);          
    struct iq_frame_struct_32* iq_frame =calloc(1, sizeof(struct iq_frame_struct_32));

    /* Initializing input transport interface */
    size_t input_buf_size = MAX_IQFRAME_PAYLOAD_SIZE*config.num_ch*4*2+IQ_HEADER_LENGTH;
    struct transport_handle* input_transport = transport_create(
        "delay_sync_iq", input_buf_size, false,
        FLOW_BACKPRESSURE, config.instance_id, TRANSPORT_SHM);
    if (!input_transport) {FATAL_ERR("Failed to create input transport")}

    ret = transport_init(input_transport);
    if (ret !=0) {FATAL_ERR("Failed to init transport interface")}
    else{log_info("Transport interface succesfully initialized");}
	
    /* Starting IQ ethernet server */
	int run_server=1;
    while(run_server)
    {
		
		/* This function blocks until a client connects to the server */
		int * sockets = malloc(2*sizeof(int)); //[server, client] 
        iq_stream_con(sockets, compute_port(5000, config.instance_id, config.port_stride));
        // TODO: Check and handle success
        
        int exit_flag =0;
        while(!exit_flag) 
        {
        	// Acquire data buffer on the shared memory interface
        	void* buf_ptr;
        	active_buff_ind = transport_get_read_buf(input_transport, &buf_ptr);
       	    if (active_buff_ind < 0){exit_flag = active_buff_ind; break;}
            iq_frame->header = (struct iq_header_struct*) buf_ptr;
			iq_frame->payload = ((float *) buf_ptr) + IQ_HEADER_LENGTH/sizeof(float);
			CHK_SYNC_WORD(check_sync_word(iq_frame->header));
			iq_frame->payload_size=iq_frame->header->cpi_length * iq_frame->header->active_ant_chs;
			//dump_iq_header(iq_frame->header);

			ret=send_iq_frame(iq_frame, sockets[1]);
			transport_release_read(input_transport, active_buff_ind);
			if(ret !=0){log_error("Closing connection"); break;}
			
			/* Waiting for further download commands on the Ethernet link*/
			int bytes_recieved = recv(sockets[1],eth_cmd,1024,0);
			eth_cmd[bytes_recieved] = '\0';
			if (strcmp(eth_cmd, "IQDownload") !=0){exit_flag=1;}       
       }
        iq_stream_close(sockets);
    }
	transport_destroy(input_transport);
	free(input_transport);
	log_info("DAQ chain IQ server has exited.");
}
