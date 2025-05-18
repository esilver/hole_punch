import asyncio
import json
import time
from typing import Optional, Tuple, Dict, Callable

# Assuming these are in sibling modules or installed based on project structure
from quic_tunnel import QuicTunnel
from network_utils import is_probable_quic_datagram
from ui_server import broadcast_to_all_ui_clients

class P2PUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self,
                 worker_id_val: str,
                 internal_udp_port_val: int, # For logging purposes
                 get_current_p2p_peer_id_func: Callable[[], Optional[str]],
                 get_current_p2p_peer_addr_func: Callable[[], Optional[Tuple[str,int]]],
                 get_quic_engine_func: Callable[[], Optional[QuicTunnel]],
                 set_quic_engine_func: Callable[[Optional[QuicTunnel]], None],
                 set_main_p2p_udp_transport_func: Callable[[Optional[asyncio.DatagramTransport]], None]
                ):
        self.worker_id = worker_id_val
        self.internal_udp_port = internal_udp_port_val
        
        self.get_current_p2p_peer_id = get_current_p2p_peer_id_func
        self.get_current_p2p_peer_addr = get_current_p2p_peer_addr_func
        self.get_quic_engine = get_quic_engine_func
        self.set_quic_engine = set_quic_engine_func
        self.set_main_p2p_udp_transport = set_main_p2p_udp_transport_func

        self.transport: Optional[asyncio.DatagramTransport] = None
        self.benchmark_sessions: Dict[str, Dict] = {} # Manages its own benchmark sessions
        print(f"Worker '{self.worker_id}': P2PUDPProtocol instance created.")

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        self.set_main_p2p_udp_transport(transport) # Update main.py's global transport
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': P2P UDP listener active on {local_addr} (Internal Port: {self.internal_udp_port}).")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        current_p2p_peer_addr = self.get_current_p2p_peer_addr()
        current_p2p_peer_id = self.get_current_p2p_peer_id()
        quic_engine_current = self.get_quic_engine()

        if (
            quic_engine_current is None
            and current_p2p_peer_addr is not None
            and addr == current_p2p_peer_addr
            and data
            and (data[0] & 0x80)  # Long header only
            and (data[0] & 0x30) == 0x00  # Packet type = Initial
        ):
            odcid: Optional[bytes] = None
            if data and (data[0] & 0x80):
                try:
                    dcid_len = data[5]
                    odcid = data[6 : 6 + dcid_len]
                except Exception as _: odcid = None

            def udp_sender_for_quic(data_to_send: bytes, destination_addr: Tuple[str, int]):
                if self.transport and not self.transport.is_closing():
                    self.transport.sendto(data_to_send, destination_addr)
                else:
                    print(f"Worker '{self.worker_id}': QUIC UDP sender: transport closing – cannot send.")

            new_quic_engine = QuicTunnel(
                worker_id_val=self.worker_id, # Use self.worker_id passed during __init__
                peer_addr_val=addr,
                udp_sender_func=udp_sender_for_quic,
                is_client_role=False,
                original_destination_cid=odcid,
            )
            self.set_quic_engine(new_quic_engine) # Update main.py's global quic_engine
            new_quic_engine.feed_datagram(data, addr)
            new_quic_engine._start_timer_loop() # Ensure timer loop is started for server-side engine
            print(f"Worker '{self.worker_id}': Lazy-created server-side QuicTunnel upon first packet from {addr} (ODCID len={len(odcid) if odcid else 'n/a'}).")
            return

        quic_engine_current = self.get_quic_engine() # Re-fetch in case it was just set
        if quic_engine_current and addr == quic_engine_current.peer_addr:
            # Only feed packets that appear to be QUIC.  Previously we forwarded
            # every packet once the handshake was done, which meant JSON chat
            # messages were accidentally given to aioquic and silently
            # discarded.  Restrict forwarding to real QUIC-looking datagrams.
            should_feed = is_probable_quic_datagram(data)
            if should_feed:
                try:
                    quic_engine_current.feed_datagram(data, addr)
                    return 
                except Exception as e_quic_feed:
                    print(f"Worker '{self.worker_id}': Error feeding datagram to QUIC engine for {addr}: {type(e_quic_feed).__name__} – {e_quic_feed}")

        try:
            message_str = data.decode()
            p2p_message = json.loads(message_str)
            is_json = True
        except (UnicodeDecodeError, json.JSONDecodeError):
            is_json = False

        if not is_json:
            if len(data) < 4: return
            seq = int.from_bytes(data[:4], "big")
            peer_addr_str = str(addr)
            if seq == 0xFFFFFFFF:
                if peer_addr_str in self.benchmark_sessions:
                    session = self.benchmark_sessions[peer_addr_str]
                    session["total_chunks"] = session["received_chunks"]
                    duration = time.monotonic() - session["start_time"]
                    throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                    status_msg = (
                        f"Benchmark Receive from {session.get('from_worker_id', 'peer')} Complete: "
                        f"Received {session['received_chunks']} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. "
                        f"Throughput: {throughput_kbps:.2f} KB/s"
                    )
                    print(f"Worker '{self.worker_id}': {status_msg}")
                    asyncio.create_task(broadcast_to_all_ui_clients({"type": "benchmark_status", "message": status_msg}))
                    del self.benchmark_sessions[peer_addr_str]
                return

            if peer_addr_str not in self.benchmark_sessions:
                self.benchmark_sessions[peer_addr_str] = {
                    "received_bytes": 0, "received_chunks": 0,
                    "start_time": time.monotonic(), "from_worker_id": current_p2p_peer_id,
                }
            session = self.benchmark_sessions[peer_addr_str]
            session["received_bytes"] += len(data)
            session["received_chunks"] += 1
            if session["received_chunks"] % 100 == 0:
                print(f"Worker '{self.worker_id}': Benchmark data received from peer@{peer_addr_str}: "
                      f"{session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB")
            return

        msg_type = p2p_message.get("type")
        from_id = p2p_message.get("from_worker_id")

        if from_id and current_p2p_peer_id and from_id != current_p2p_peer_id:
            print(f"Worker '{self.worker_id}': WARNING - Received P2P message from '{from_id}' but current peer is '{current_p2p_peer_id}'. Addr: {addr}")
        elif not from_id and msg_type not in ["benchmark_chunk", "benchmark_end"]:
            print(f"Worker '{self.worker_id}': WARNING - Received P2P message of type '{msg_type}' without 'from_worker_id'. Addr: {addr}")

        if msg_type == "chat_message":
            content = p2p_message.get("content")
            print(f"Worker '{self.worker_id}': Received P2P chat from '{from_id}' (expected: '{current_p2p_peer_id}'): '{content}'")
            asyncio.create_task(broadcast_to_all_ui_clients({"type": "p2p_message_received", "from_peer_id": from_id, "content": content}))
        
        elif msg_type == "p2p_ping_request":
            original_timestamp = p2p_message.get("timestamp")
            # from_id is already extracted
            if original_timestamp is not None and from_id is not None:
                response_payload = {
                    "type": "p2p_ping_response",
                    "from_worker_id": self.worker_id, # This peer is sending the response
                    "original_timestamp": original_timestamp,
                    "responding_to_worker_id": from_id # ID of the ping initiator
                }
                if self.transport:
                    self.transport.sendto(json.dumps(response_payload).encode(), addr)
                    print(f"Worker '{self.worker_id}': Sent P2P Ping Response to '{from_id}' at {addr}.")
            else:
                print(f"Worker '{self.worker_id}': Received malformed p2p_ping_request from {addr}.")

        elif msg_type == "p2p_ping_response": # This worker initiated the ping
            # This message should be broadcast to UI clients so the originating UI can calculate RTT.
            # The UI client that sent the original p2p_ping_command will look for this.
            from_worker = p2p_message.get("from_worker_id")
            print(f"Worker '{self.worker_id}': Received P2P Ping Response originally from '{from_worker}' (I am '{self.worker_id}'). Broadcasting to UI.")
            asyncio.create_task(broadcast_to_all_ui_clients({
                "type": "p2p_ping_response", # UI handler looks for this type
                "original_timestamp": p2p_message.get("original_timestamp"),
                "from_peer_id": p2p_message.get("from_worker_id"), # The peer who responded
                "responder_addr": str(addr) # For logging/debug in UI if needed
            }))

        elif msg_type == "p2p_test_data":
            test_data_content = p2p_message.get("data")
            print(f"Worker '{self.worker_id}': +++ P2P_TEST_DATA RECEIVED from '{from_id}': '{test_data_content}' +++")
        elif msg_type == "benchmark_chunk": 
            seq = p2p_message.get("seq", -1)
            payload_b64 = p2p_message.get("payload", "")
            peer_addr_str = str(addr)
            if peer_addr_str not in self.benchmark_sessions:
                self.benchmark_sessions[peer_addr_str] = {"received_bytes": 0, "received_chunks": 0, "start_time": time.monotonic(), "total_chunks": -1, "from_worker_id": from_id}
            session = self.benchmark_sessions[peer_addr_str]
            session["received_bytes"] += len(message_str) 
            session["received_chunks"] += 1
            if session["received_chunks"] % 100 == 0: 
                print(f"Worker '{self.worker_id}': Benchmark data received from {from_id}@{peer_addr_str}: {session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB")
        elif msg_type == "benchmark_end": 
            total_chunks_sent = p2p_message.get("total_chunks", 0)
            peer_addr_str = str(addr)
            if peer_addr_str in self.benchmark_sessions:
                session = self.benchmark_sessions[peer_addr_str]
                session["total_chunks"] = total_chunks_sent
                duration = time.monotonic() - session["start_time"]
                throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                status_msg = f"Benchmark Receive from {session['from_worker_id']} Complete: Received {session['received_chunks']}/{total_chunks_sent} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. Throughput: {throughput_kbps:.2f} KB/s"
                print(f"Worker '{self.worker_id}': {status_msg}")
                asyncio.create_task(broadcast_to_all_ui_clients({"type": "benchmark_status", "message": status_msg}))
                del self.benchmark_sessions[peer_addr_str] 
            else:
                print(f"Worker '{self.worker_id}': Received benchmark_end from unknown session/peer {addr}")     
        elif "P2P_PING_FROM_" in message_str: print(f"Worker '{self.worker_id}': !!! P2P UDP Ping (legacy) received from {addr} !!!")
    
    def error_received(self, exc: Exception):
        print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    
    def connection_lost(self, exc: Optional[Exception]): 
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        # If this specific transport instance was the one main.py knew about, clear it.
        # This is determined by the set_main_p2p_udp_transport callback logic in main.py
        # which should only clear its global if the transport being lost is the current global one.
        # For safety, we always call to inform main.py that *this* transport instance is lost.
        # Main.py will decide if its global p2p_udp_transport needs to be set to None.
        if self.transport: # Check if this instance had a transport
             self.set_main_p2p_udp_transport(None) # Inform main.py, it should check identity before nullifying global. 