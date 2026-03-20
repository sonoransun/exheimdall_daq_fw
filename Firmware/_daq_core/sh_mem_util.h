#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#define ERR_FCNTL -20

/*
*-------------------------------------
*       Control FIFO Parameters
*-------------------------------------
*/
#define INGORE_FRAME_DROP_WARNINGS 1

#define GEN_FRAME_SM_NAME_A "HEIMDALL_DAQ_FW_GEN_STD_FRAME_A"
#define GEN_FRAME_SM_NAME_B "HEIMDALL_DAQ_FW_GEN_STD_FRAME_B"
#define GEN_FRAME_FW_FIFO "_data_control/fw_ctr_gen_frame"
#define GEN_FRAME_BW_FIFO "_data_control/bw_ctr_gen_frame"

#define DECIMATOR_IN_SM_NAME_A "decimator_in_A"
#define DECIMATOR_IN_SM_NAME_B "decimator_in_B"
#define DECIMATOR_IN_FW_FIFO "_data_control/fw_decimator_in"
#define DECIMATOR_IN_BW_FIFO "_data_control/bw_decimator_in"
        
#define DECIMATOR_OUT_SM_NAME_A "decimator_out_A"
#define DECIMATOR_OUT_SM_NAME_B "decimator_out_B"
#define DECIMATOR_OUT_FW_FIFO "_data_control/fw_decimator_out"
#define DECIMATOR_OUT_BW_FIFO "_data_control/bw_decimator_out"

#define DELAY_SYNC_IQ_SM_NAME_A "delay_sync_iq_A"
#define DELAY_SYNC_IQ_SM_NAME_B "delay_sync_iq_B"
#define DELAY_SYNC_IQ_FW_FIFO "_data_control/fw_delay_sync_iq"
#define DELAY_SYNC_IQ_BW_FIFO "_data_control/bw_delay_sync_iq"

//const unsigned char fw_cmd_init_ready[1] = 0x0A;
#define INIT_READY    10
#define A_BUFF_READY   1
#define B_BUFF_READY   2
#define TERMINATE    255

/*
*-------------------------------------
*       Batched Control Protocol
*-------------------------------------
*/
// Optimized control structure for reduced syscall overhead
struct shmem_control_batch {
    uint32_t sequence;      // Sequence number for ordering
    uint32_t frame_count;   // Number of frames processed
    uint64_t timestamp_ns;  // High-precision timestamp
    uint8_t buffer_mask;    // Bitmask for multiple buffer status (A=0x01, B=0x02)
    uint8_t priority_flags; // Priority indicators for urgent operations
    uint16_t payload_size;  // Size of additional payload data
} __attribute__((packed, aligned(64))); // Cache-line aligned

/*
*-------------------------------------
*       Shared memory transfer
*-------------------------------------
*/

struct shmem_transfer_struct {    
    char shared_memory_names[2][512];
    char fw_ctr_fifo_name[512];
    char bw_ctr_fifo_name[512];
    size_t shared_memory_size;
    bool buffer_free[2];
    bool io_type; // 0-Output, 1-Input
    bool drop_mode; // If enabled, frames are dropped
    int dropped_frame_cntr;
    FILE* fw_ctr_fifo;
    FILE* bw_ctr_fifo;
    void* shm_ptr[2];
    int shm_fd[2];
};

/*
*-------------------------------------
*    Shared memory util functions
*-------------------------------------
*/

int init_out_sm_buffer(struct shmem_transfer_struct*);
int init_in_sm_buffer(struct shmem_transfer_struct*);

int destory_sm_buffer(struct shmem_transfer_struct*);

void send_ctr_init_ready(struct shmem_transfer_struct*);
void send_ctr_buff_ready(struct shmem_transfer_struct*, int);
void send_ctr_buff_free(struct shmem_transfer_struct*, int);
void send_ctr_terminate(struct shmem_transfer_struct*);

// Optimized batched control functions
void send_ctr_batch(struct shmem_transfer_struct*, struct shmem_control_batch*);
int init_ctr_batch(struct shmem_control_batch*, uint32_t initial_sequence);
void update_ctr_batch_buffer_ready(struct shmem_control_batch*, int buffer_index);
void update_ctr_batch_timestamp(struct shmem_control_batch*);

// Backward compatibility wrappers
void send_ctr_buff_ready_batch(struct shmem_transfer_struct*, int, struct shmem_control_batch*);
void send_ctr_terminate(struct shmem_transfer_struct*);

int wait_buff_free(struct shmem_transfer_struct*);
int wait_buff_ready(struct shmem_transfer_struct*);
int wait_ctr_init_read(struct shmem_transfer_struct*);

/*
*-------------------------------------
*    Federation instance helpers
*-------------------------------------
*/

void build_shmem_name(char* dest, int instance_id, const char* base_name);
void build_fifo_path(char* dest, int instance_id, const char* base_path);
int  compute_port(int base_port, int instance_id, int stride);




