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
        self.associated_quic_tunnel: Optional[QuicTunnel] = None # For robustly handling the tunnel for this protocol instance
        print(f"Worker '{self.worker_id}': P2PUDPProtocol instance created.")

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        self.set_main_p2p_udp_transport(transport) # Update main.py's global transport
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': P2P UDP listener active on {local_addr} (Internal Port: {self.internal_udp_port}).")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        # Use the tunnel specifically associated with this protocol instance if active.
        active_tunnel = self.associated_quic_tunnel
        
        if active_tunnel and active_tunnel.is_terminated_or_transport_lost(): # Check QuicTunnel's state
            print(f"Worker '{self.worker_id}': Associated QuicTunnel for {active_tunnel.peer_addr} is terminated/lost. Clearing reference.")
            self.associated_quic_tunnel = None # It might have already called its on_close to do this.
            active_tunnel = None

        if active_tunnel:
            # If there's an active associated tunnel, route probable QUIC packets to it.
            # This assumes one P2PUDPProtocol instance handles one peer relationship at a time.
            if is_probable_quic_datagram(data):
                # Update peer address if it has changed (e.g. NAT rebinding)
                if addr != active_tunnel.peer_addr:
                    old_addr = active_tunnel.peer_addr
                    active_tunnel.peer_addr = addr # Update the QuicTunnel instance directly
                    print(
                        f"Worker '{self.worker_id}': Detected peer UDP address change for associated tunnel "
                        f"{old_addr} ➜ {addr}. Updated QuicTunnel.peer_addr."
                    )
                
                asyncio.create_task(active_tunnel.feed_datagram(data, addr))
                return
            # If not a QUIC datagram, it falls through to JSON/benchmark processing below,
            # using the peer context (get_current_p2p_peer_id/addr) for those messages.
        else:
            # No active associated tunnel for this P2PUDPProtocol instance.
            # Try to create a server-side tunnel if it's an Initial packet from the expected peer.
            current_p2p_peer_addr = self.get_current_p2p_peer_addr()
            is_initial_quic_packet = (data[0] & 0x80) and ((data[0] & 0x30) == 0x00) # QUIC Initial packet check

            if (current_p2p_peer_addr is not None and 
                addr == current_p2p_peer_addr and 
                is_initial_quic_packet):
                
                odcid: Optional[bytes] = None
                if data and (data[0] & 0x80): # Long header
                    try:
                        dcid_len_offset = 5
                        dcid_len = data[dcid_len_offset]
                        odcid = data[dcid_len_offset + 1 : dcid_len_offset + 1 + dcid_len]
                    except IndexError: odcid = None

                def udp_sender_for_quic(data_to_send: bytes, destination_addr: Tuple[str, int]):
                    if self.transport and not self.transport.is_closing():
                        self.transport.sendto(data_to_send, destination_addr)
                    else:
                        print(f"Worker '{self.worker_id}': QUIC UDP sender: transport closing/gone – cannot send.")
                
                # Define the callback for when this new server tunnel closes
                # Need to ensure new_server_tunnel is accessible in the closure. Best to pass it.
                # This callback will be specific to *this* new_server_tunnel instance.
                def server_tunnel_on_close_callback_factory(tunnel_instance_being_closed: QuicTunnel):
                    def server_tunnel_on_close_callback():
                        if self.associated_quic_tunnel is tunnel_instance_being_closed:
                            print(f"Worker '{self.worker_id}': Associated server-side QuicTunnel for {tunnel_instance_being_closed.peer_addr} closed. Clearing reference.")
                            self.associated_quic_tunnel = None
                        # Note: This does not automatically set the global AppContext engine to None.
                        # That should be handled by a callback provided by main.py/AppContext if needed,
                        # when this tunnel was set as the global one.
                    return server_tunnel_on_close_callback

                # Create the new server-side tunnel instance first
                new_server_tunnel = QuicTunnel(
                    worker_id_val=self.worker_id,
                    peer_addr_val=addr,
                    udp_sender_func=udp_sender_for_quic,
                    is_client_role=False,
                    original_destination_cid=odcid,
                    on_close_callback=None # Placeholder, will be set below by factory
                )
                # Now set its specific on_close_callback
                new_server_tunnel._on_close_callback = server_tunnel_on_close_callback_factory(new_server_tunnel)

                self.associated_quic_tunnel = new_server_tunnel # Associate with this protocol instance
                self.set_quic_engine(new_server_tunnel) # Inform main context this is the current/new engine
                
                asyncio.create_task(new_server_tunnel.feed_datagram(data, addr))
                new_server_tunnel._start_timer_loop() 
                print(f"Worker '{self.worker_id}': Lazy-created server-side QuicTunnel (associated with this protocol) upon first packet from {addr}.")
                return
            
            elif is_probable_quic_datagram(data):
                # Received a QUIC packet, but no associated tunnel for this P2PUDPProtocol instance,
                # and it wasn't an Initial packet for the configured P2P peer.
                # This could be a late packet for a tunnel that was associated but now gone,
                # or a packet for a tunnel managed by a different P2P protocol instance or context.
                # The global get_quic_engine() might know about it, but relying on that was the original issue.
                # For robustness of *this* protocol instance, if it doesn't own the tunnel, it shouldn't guess.
                print(f"Worker '{self.worker_id}': Received probable QUIC datagram from {addr}, but no QUIC engine is specifically associated with this P2P protocol instance and it wasn't an Initial packet for its designated peer. Datagram not processed by QUIC here.")

        # Fall through for non-QUIC P2P messages (JSON, benchmarks)
        # These use self.get_current_p2p_peer_id() and self.get_current_p2p_peer_addr() for context.
        current_p2p_peer_addr_for_msg = self.get_current_p2p_peer_addr()
        current_p2p_peer_id = self.get_current_p2p_peer_id()

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
        
        # Notify the current QUIC engine, if any, that its transport is gone.
        current_quic_engine = self.get_quic_engine()
        if current_quic_engine:
            # It's possible the QUIC engine is already closing or closed, 
            # but calling this method ensures it knows the transport is definitively gone.
            asyncio.create_task(current_quic_engine.notify_transport_lost())

        # Inform main.py (or the AppContext) about the transport loss.
        # The callback itself (set_main_p2p_udp_transport) is responsible for checking if the 
        # lost transport instance matches the one it holds globally before nullifying it.
        if self.transport: # Check if this protocol instance *had* a transport associated with it.
             self.set_main_p2p_udp_transport(None) # Signal that this transport is gone.
        
        self.transport = None # Clear this protocol instance's own reference to the transport. 