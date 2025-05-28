#!/usr/bin/env python3
"""
Trino Response Proxy - Routes Trino responses back through the proxy
This allows the proxy to control response flow and prevent 503 errors
"""

import asyncio
import os
from typing import Dict, Optional, Tuple

class TrinoResponseProxy:
    """
    This proxy sits between Trino and the client, intercepting responses
    and routing them back through our main proxy for flow control.
    """
    
    def __init__(self, trino_port: int = 8081, proxy_port: int = 8082):
        self.trino_port = trino_port
        self.proxy_port = proxy_port
        self.connections: Dict[str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        
    async def start(self):
        """Start the response proxy server"""
        server = await asyncio.start_server(
            self.handle_client, '127.0.0.1', self.proxy_port
        )
        print(f"Response proxy listening on port {self.proxy_port}")
        async with server:
            await server.serve_forever()
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming connections from main proxy"""
        client_addr = writer.get_extra_info('peername')
        connection_id = f"{client_addr[0]}:{client_addr[1]}"
        
        try:
            # Read the request
            request_data = []
            while True:
                line = await reader.readline()
                request_data.append(line)
                if line == b'\r\n':
                    # End of headers
                    break
            
            # Check for Content-Length to read body
            headers = b''.join(request_data)
            content_length = 0
            for line in request_data:
                if line.lower().startswith(b'content-length:'):
                    content_length = int(line.split(b':')[1].strip())
                    break
            
            # Read body if present
            body = b''
            if content_length > 0:
                body = await reader.read(content_length)
            
            full_request = headers + body
            
            # Forward to Trino
            trino_reader, trino_writer = await asyncio.open_connection(
                '127.0.0.1', self.trino_port
            )
            
            trino_writer.write(full_request)
            await trino_writer.drain()
            
            # Read response with flow control
            response_chunks = []
            total_size = 0
            max_buffer_size = 1024 * 1024  # 1MB buffer
            
            while True:
                chunk = await trino_reader.read(4096)
                if not chunk:
                    break
                    
                response_chunks.append(chunk)
                total_size += len(chunk)
                
                # If buffer is getting large, flush to client
                if total_size > max_buffer_size:
                    for chunk in response_chunks:
                        writer.write(chunk)
                        await writer.drain()
                    response_chunks = []
                    total_size = 0
            
            # Send remaining chunks
            for chunk in response_chunks:
                writer.write(chunk)
                await writer.drain()
            
            # Close connections
            trino_writer.close()
            await trino_writer.wait_closed()
            
        except Exception as e:
            print(f"Error in response proxy: {e}")
            error_response = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 21\r\n\r\nResponse proxy error"
            writer.write(error_response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

if __name__ == "__main__":
    proxy = TrinoResponseProxy()
    asyncio.run(proxy.start())