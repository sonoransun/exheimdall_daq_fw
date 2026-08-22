/*
 *
 * Description: Manages the Ethernet server socket for IQ streaming
 *
 * Project: HeIMDALL DAQ Firmware
 * License: GNU GPL V3
 * Authors: Tamas Peto
 *
 *  Copyright (C) 2018-2020  Tamás Pető
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
#pragma once
#include <string.h>
// Ethernet server libraries
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include "log.h"

#define IQ_SERVER_PORT  	    5000

/* Functions are static: this header is included by the iq_server translation
 * unit only, and non-static definitions in a header are an ODR/link hazard. */

static int iq_stream_listen(int port, const char* listen_address)
{
   /*
	*	Description:
	*		Creates the listening socket of the IQ streaming server. The socket
	*		is bound to the configured listen address ([daq] listen_address,
	*		default 0.0.0.0) and kept open across client sessions.
	*
	*		Returns the listening socket descriptor, or -1 on failure.
	*/
	int sock, true_value = 1;
	struct sockaddr_in server_addr;

	if ((sock = socket(AF_INET, SOCK_STREAM, 0)) == -1) { // Create server socket
		log_fatal("Create server socket failed ");
		return(-1);
	}
	 /* Applying socket options*/
	if (setsockopt(sock,SOL_SOCKET,SO_REUSEADDR,&true_value,sizeof(int)) == -1) {
	    log_fatal("Setting socket options failed");
		close(sock);
		return(-1);
	}
	// Set server address parameters
	memset(&server_addr, 0, sizeof(server_addr));
	server_addr.sin_family = AF_INET; // Address family
	server_addr.sin_port = htons(port);
	server_addr.sin_addr.s_addr = INADDR_ANY;
	if (listen_address != NULL && listen_address[0] != '\0') {
		struct in_addr bind_addr;
		if (inet_pton(AF_INET, listen_address, &bind_addr) == 1)
			server_addr.sin_addr = bind_addr;
		else
			log_warn("Invalid listen_address '%s', binding to 0.0.0.0", listen_address);
	}

	if (bind(sock, (struct sockaddr *)&server_addr, sizeof(server_addr)) == -1)
	{
		log_fatal("Unable to bind the server socket");
		close(sock);
		return(-1);
	}
	if (listen(sock, 5) == -1)
	{
		log_fatal("Unable to start listenning");
		close(sock);
		return(-1);
	}

	log_info("IQ Data TCP Server waiting for client on port %d",port);
	return sock;
}

static int iq_stream_accept(int listen_sock)
{
   /*
	*	Description:
	*		Accepts one connection from an IQ sample sink, such as a signal
	*		processing or data recorder module, then waits for the "streaming"
	*		command. When the command has arrived, the descriptor of the client
	*		is returned to the calling process (generally a double buffered iq
	*		streaming process). The listening socket stays open so further
	*		clients can connect after this session ends.
	*
	*		Returns the connected client descriptor, or -1 on failure.
	*/
	int connected, bytes_recieved;
	socklen_t sin_size;
	char recv_data[1024];
	struct sockaddr_in client_addr;

	sin_size = sizeof(struct sockaddr_in);
	connected = accept(listen_sock, (struct sockaddr *)&client_addr,&sin_size);
	if (connected == -1)
	{
		log_error("Accepting client connection failed");
		return -1;
	}
	log_info("New connection from (%s , %d)",  inet_ntoa(client_addr.sin_addr), ntohs(client_addr.sin_port));

	log_info("Waiting for client streaming request");
	bytes_recieved = recv(connected, recv_data, sizeof(recv_data)-1, 0);

	/* User may close the socket unexpectedly */
	if(bytes_recieved <= 0)
	{
		log_error("Unexpected connection close");
		close(connected);
		return -1;
	}
	recv_data[bytes_recieved] = '\0';

	/* Streaming start command */
	if (strcmp(recv_data , "streaming") == 0)
	{
		log_info("Streaming request received");
		return connected;
	}
	close(connected);
	return -1; //Not expected signaling
}
