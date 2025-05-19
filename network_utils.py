import aiohttp
import asyncio
import os
import json
import time
import stun # pystun3
import socket # For socket.gaierror
from typing import Optional, Tuple, Any, Dict # Added Dict & Any
import websockets # For ui_ws type hint in benchmark_send_udp_data

def is_probable_quic_datagram(blob: bytes) -> bool:
    """Guess whether *blob* is a QUIC packet.

    This avoids feeding JSON / ASCII hole-punch pings into ``aioquic`` which
    previously produced errors such as "first packet must be INITIAL".

    Heuristic rules (cheap, no deps):

      • If the MSB of the first byte is 1, the packet uses a long header and
        is definitely QUIC.
      • Otherwise accept it as QUIC only if the *fixed* bit (0x40) is set,
        which every short-header packet must have.
    """
    if not blob:
        return False
    first = blob[0]

    # Long-header packets – MSB must be 1
    if first & 0x80:
        return True

    # Short-header packets must have the fixed bit set, *and* the two reserved
    # bits 5-4 cannot both be 1 (0x30).  ASCII '{' is 0x7B which has the fixed
    # bit (0x40) plus 0x30, so it is rejected by the extra check below.
    if first & 0x40 and (first & 0x30) != 0x30:
        return True

    return False

async def download_benchmark_file(benchmark_file_url: str) -> bytes:
    """Download the benchmark file from GCS asynchronously."""
    if not benchmark_file_url:
        raise RuntimeError("Benchmark file URL not provided for download.")

    async with aiohttp.ClientSession() as session:
        async with session.get(benchmark_file_url) as resp:
            resp.raise_for_status()
            return await resp.read()

async def discover_stun_endpoint(
    worker_id_val: str,
    internal_udp_port_val: int,
    default_stun_host_val: str,
    default_stun_port_val: int
) -> Optional[Tuple[str, int]]:
    """Perform STUN discovery to find the public UDP IP and port."""
    max_attempts = int(os.environ.get("STUN_MAX_ATTEMPTS", "5"))
    base_delay = float(os.environ.get("STUN_RETRY_DELAY_SEC", "2"))
    attempt = 0

    while attempt < max_attempts:
        try:
            stun_host_to_use = os.environ.get("STUN_HOST", default_stun_host_val)
            stun_port_to_use = int(os.environ.get("STUN_PORT", default_stun_port_val))
            print(
                f"Worker '{worker_id_val}': STUN attempt {attempt+1}/{max_attempts} via {stun_host_to_use}:{stun_port_to_use} for local port {internal_udp_port_val}."
            )

            try:
                nat_type, external_ip, external_port = stun.get_ip_info(
                    source_ip="0.0.0.0",
                    source_port=internal_udp_port_val,
                    stun_host=stun_host_to_use,
                    stun_port=stun_port_to_use,
                )
                print(
                    f"Worker '{worker_id_val}': STUN result: NAT='{nat_type}', External IP='{external_ip}', Port={external_port}"
                )
            except Exception as stun_e:
                print(
                    f"Worker '{worker_id_val}': STUN get_ip_info failed on attempt {attempt+1}: {type(stun_e).__name__} - {stun_e}"
                )
                external_ip, external_port = None, None

            if external_ip and external_port:
                return external_ip, external_port

            attempt += 1
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"Worker '{worker_id_val}': STUN discovery failed, will retry in {delay:.1f}s…"
                )
                await asyncio.sleep(delay)
        except socket.gaierror as e_gaierror:
            print(f"Worker '{worker_id_val}': STUN host DNS resolution error: {e_gaierror}")
            break # Do not retry on DNS failure
        except Exception as e_outer:
            print(
                f"Worker '{worker_id_val}': Error in STUN attempt {attempt}/{max_attempts}: {type(e_outer).__name__} - {e_outer}"
            )
            attempt += 1
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

    print(
        f"Worker '{worker_id_val}': STUN discovery failed after {max_attempts} attempts. Giving up for now."
    )
    return None

async def benchmark_send_udp_data(app_context: Any, target_ip: str, target_port: int, _size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
    """Download a file from GCS and send it to the peer over UDP."""
    worker_id = app_context.worker_id
    p2p_udp_transport = app_context.get_p2p_transport()
    
    BENCHMARK_FILE_URL = app_context.BENCHMARK_FILE_URL # Get from context
    BENCHMARK_CHUNK_SIZE = app_context.BENCHMARK_CHUNK_SIZE # Get from context
    
    current_p2p_peer_addr = app_context.get_current_p2p_peer_addr()
    # stop_signal_received = app_context.get_stop_signal() # stop_signal check is done per iteration

    if not (p2p_udp_transport and current_p2p_peer_addr):
        err_msg = "P2P UDP transport or peer address not available for benchmark."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return

    if not BENCHMARK_FILE_URL:
        err_msg = "BENCHMARK_FILE_URL not set (via AppContext)."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": err_msg}))
        return

    print(f"Worker '{worker_id}': Starting benchmark download from {BENCHMARK_FILE_URL}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Downloading benchmark file..."}))

    start_download = time.monotonic()
    try:
        file_bytes = await download_benchmark_file(BENCHMARK_FILE_URL) 
    except Exception as e:
        err_msg = f"Download failed: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": err_msg}))
        return
    download_time = time.monotonic() - start_download
    file_size_kb = len(file_bytes) / 1024
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Downloaded {file_size_kb/1024:.2f} MB in {download_time:.2f}s"}))

    num_chunks = (len(file_bytes) + BENCHMARK_CHUNK_SIZE - 1) // BENCHMARK_CHUNK_SIZE

    print(f"Worker '{worker_id}': Sending {len(file_bytes)} bytes in {num_chunks} chunks to {target_ip}:{target_port}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Starting UDP send of {file_size_kb/1024:.2f} MB..."}))

    start_time = time.monotonic()
    bytes_sent = 0

    try:
        for i in range(num_chunks):
            if app_context.get_stop_signal() or ui_ws.closed:
                print(f"Worker '{worker_id}': Benchmark send cancelled (stop_signal or UI disconnected).")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark send cancelled."}))
                break

            chunk = file_bytes[i * BENCHMARK_CHUNK_SIZE : (i + 1) * BENCHMARK_CHUNK_SIZE]
            seq_header = i.to_bytes(4, "big")
            packet = seq_header + chunk
            current_p2p_transport = app_context.get_p2p_transport()
            if current_p2p_transport:
                current_p2p_transport.sendto(packet, (target_ip, target_port))
                bytes_sent += len(packet)
            else:
                print(f"Worker '{worker_id}': Benchmark send error: P2P UDP transport is not available.")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Error: P2P UDP transport lost."}))
                break    
            if (i + 1) % (num_chunks // 10 if num_chunks >= 10 else 1) == 0:
                progress_msg = f"Benchmark Send: Sent {i+1}/{num_chunks} chunks ({bytes_sent / 1024:.2f} KB)..."
                print(f"Worker '{worker_id}': {progress_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
        else: 
            current_p2p_transport = app_context.get_p2p_transport()
            if current_p2p_transport: 
                end_marker = (0xFFFFFFFF).to_bytes(4, "big")
                current_p2p_transport.sendto(end_marker, (target_ip, target_port))
                print(f"Worker '{worker_id}': Sent benchmark_end marker to {target_ip}:{target_port}")
                end_time = time.monotonic()
                duration = end_time - start_time
                throughput_kbps = (bytes_sent / 1024) / duration if duration > 0 else 0
                final_msg = (
                    f"Benchmark Send Complete: {file_size_kb/1024:.2f} MB sent in {duration:.2f}s (download {download_time:.2f}s)." \
                    f" Throughput: {throughput_kbps:.2f} KB/s"
                )
                print(f"Worker '{worker_id}': {final_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": final_msg}))
    except Exception as e:
        error_msg = f"Benchmark Send Error: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {error_msg}")
        if not ui_ws.closed:
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {error_msg}"})) 