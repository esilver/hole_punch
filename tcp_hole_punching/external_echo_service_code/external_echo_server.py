#!/usr/bin/env python3
import asyncio
import json
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(module)s:%(lineno)d - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

async def handle_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer_addr_tuple = writer.get_extra_info('peername')
    if not peer_addr_tuple:
        logger.error("Could not get peer address.")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    client_ip, client_port = peer_addr_tuple
    logger.info(f"Connection from {client_ip}:{client_port}")

    try:
        # Optionally, read a bit of data to ensure the connection is fully established
        # and to clear any data the client might send (like the newline from our worker)
        try:
            await asyncio.wait_for(reader.read(100), timeout=1.0)
        except asyncio.TimeoutError:
            logger.debug(f"No data received from {client_ip}:{client_port} within timeout, proceeding.")
        except ConnectionResetError:
            logger.warning(f"Connection reset by {client_ip}:{client_port} during initial read.")
            return # Exit if connection reset
        except Exception as e_read:
            logger.warning(f"Error reading initial data from {client_ip}:{client_port}: {e_read}")
            # Continue to try and send response anyway

        response_data = {
            "public_ip": client_ip,
            "mapped_public_port": client_port
        }
        # Ensure response is JSON and newline-terminated for client's readuntil
        response_json_nl = json.dumps(response_data) + "\n"

        logger.info(f"Responding to {client_ip}:{client_port} with: {response_json_nl.strip()}")
        writer.write(response_json_nl.encode('utf-8'))
        await writer.drain()

    except ConnectionResetError:
        logger.warning(f"Connection reset by {client_ip}:{client_port} during write/drain.")
    except Exception as e:
        logger.exception(f"Error handling client {client_ip}:{client_port}: {e}")
    finally:
        logger.info(f"Closing connection with {client_ip}:{client_port}")
        if not writer.is_closing():
            writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.debug(f"Exception during writer.wait_closed() for {client_ip}:{client_port}, already closed or error.")

async def main():
    # Cloud Run provides the PORT environment variable for the container to listen on
    listen_port = int(os.getenv("PORT", "8080"))
    listen_host = "0.0.0.0"

    server = await asyncio.start_server(
        handle_echo, listen_host, listen_port)

    if server.sockets:
        server_addr = server.sockets[0].getsockname()
        logger.info(f'External Echo Server listening on {server_addr}')
    else:
        logger.critical("External Echo Server: Failed to get socket information. Cannot listen.")
        return # Should not happen

    async with server:
        await server.serve_forever()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("External Echo Server shutting down.")
    except Exception as e_main:
        logger.critical(f"External Echo Server critical error in main: {e_main}", exc_info=True) 