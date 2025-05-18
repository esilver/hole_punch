import asyncio
import os
import json
import websockets
import requests
import time
from typing import Optional, Tuple, Callable, Any # Using Any for AppContext for now

# These would be imported in main.py and passed via AppContext or directly
# from network_utils import discover_stun_endpoint
# from quic_tunnel import QuicTunnel
# from ui_server import broadcast_to_all_ui_clients
# from p2p_protocol import P2PUDPProtocol # For type hint of factory

async def start_udp_hole_punch(app_context: Any, peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    # Access globals via app_context
    worker_id = app_context.worker_id
    stop_signal_received = app_context.get_stop_signal()
    p2p_udp_transport = app_context.get_p2p_transport()
    current_p2p_peer_id = app_context.get_current_p2p_peer_id()
    current_p2p_peer_addr = app_context.get_current_p2p_peer_addr()
    quic_engine = app_context.get_quic_engine()
    QuicTunnel_cls = app_context.QuicTunnel # Class itself
    broadcast_ui_func = app_context.broadcast_to_all_ui_clients

    if not p2p_udp_transport: 
        print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'."); 
        return

    print(f"Worker '{worker_id}': Initiating P2P connection. Previous peer ID: '{current_p2p_peer_id}', Previous peer addr: {current_p2p_peer_addr}.")
    
    if quic_engine and (quic_engine.peer_addr != (peer_udp_ip, peer_udp_port) or current_p2p_peer_id != peer_worker_id):
        print(f"Worker '{worker_id}': Peer changed or re-initiating. Closing existing QUIC tunnel with {quic_engine.peer_addr}.")
        old_engine = quic_engine
        app_context.set_quic_engine(None)
        proto_inst = app_context.get_p2p_protocol_instance()
        if proto_inst and proto_inst.associated_quic_tunnel is old_engine:
            proto_inst.associated_quic_tunnel = None
        asyncio.create_task(old_engine.close())
        quic_engine = None  # Local var update

    app_context.set_current_p2p_peer_id(peer_worker_id)
    app_context.set_current_p2p_peer_addr((peer_udp_ip, peer_udp_port))
    current_p2p_peer_id = peer_worker_id # Local var update
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port) # Local var update

    print(f"Worker '{worker_id}': Set new P2P target. Current peer ID: '{current_p2p_peer_id}', Current peer addr: {current_p2p_peer_addr}.")

    # local_bind_addr_tuple = p2p_udp_transport.get_extra_info('sockname') if p2p_udp_transport else ("unknown", "unknown") # Already checked p2p_udp_transport
    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    for i in range(1, 4):
        if app_context.get_stop_signal(): break # Check again
        try:
            message_content = f"P2P_HOLE_PUNCH_PING_FROM_{worker_id}_NUM_{i}"
            p2p_udp_transport.sendto(message_content.encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent UDP Hole Punch PING {i} to {current_p2p_peer_addr}")
        except Exception as e: print(f"Worker '{worker_id}': Error sending UDP Hole Punch PING {i}: {e}")
        await asyncio.sleep(0.5)
    print(f"Worker '{worker_id}': Finished UDP Hole Punch PING burst to '{peer_worker_id}'.")

    if not app_context.get_quic_engine(): # Check current global state via context
        is_quic_client_role = worker_id > current_p2p_peer_id 

        if is_quic_client_role:
            print(f"Worker '{worker_id}': Initializing QUIC tunnel (client) with peer {current_p2p_peer_addr}")

            def udp_sender_for_quic(data_to_send: bytes, destination_addr: Tuple[str, int]):
                # Need to get fresh p2p_udp_transport via app_context if it can change
                current_p2p_transport = app_context.get_p2p_transport()
                if current_p2p_transport and not current_p2p_transport.is_closing():
                    current_p2p_transport.sendto(data_to_send, destination_addr)
                else:
                    print(f"Worker '{worker_id}': QUIC UDP sender: P2P transport not available or closing. Cannot send.")

            new_quic_engine = QuicTunnel_cls(
                worker_id_val=worker_id,
                peer_addr_val=current_p2p_peer_addr,
                udp_sender_func=udp_sender_for_quic,
                is_client_role=True,
            )
            app_context.set_quic_engine(new_quic_engine)
            proto_inst = app_context.get_p2p_protocol_instance()
            if proto_inst:
                proto_inst.associated_quic_tunnel = new_quic_engine
            await asyncio.sleep(0.5)
            # Ensure new_quic_engine (now the global one) is connected
            engine_to_connect = app_context.get_quic_engine()
            if engine_to_connect:
                await engine_to_connect.connect_if_client()
            print(f"Worker '{worker_id}': QUIC engine setup initiated for peer {current_p2p_peer_addr}. Role: Client")
        else:
            print(f"Worker '{worker_id}': Acting as QUIC Server – will create tunnel on first inbound Initial packet.")

    await broadcast_ui_func({"type": "p2p_status_update", "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...", "peer_id": peer_worker_id})

async def attempt_hole_punch_when_ready(app_context: Any, peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str, max_wait_sec: float = 10.0, check_interval: float = 0.5):
    worker_id = app_context.worker_id
    waited = 0.0
    while not app_context.get_stop_signal() and waited < max_wait_sec:
        if app_context.get_p2p_transport():
            await start_udp_hole_punch(app_context, peer_udp_ip, peer_udp_port, peer_worker_id)
            return
        await asyncio.sleep(check_interval)
        waited += check_interval
    print(f"Worker '{worker_id}': Gave up waiting ({waited:.1f}s) for UDP listener to become active before initiating hole-punch to '{peer_worker_id}'.")

async def connect_to_rendezvous(app_context: Any, rendezvous_ws_url: str):
    worker_id = app_context.worker_id
    internal_udp_port = app_context.INTERNAL_UDP_PORT
    default_stun_host = app_context.DEFAULT_STUN_HOST
    default_stun_port = app_context.DEFAULT_STUN_PORT
    discover_stun_endpoint_func = app_context.discover_stun_endpoint
    p2p_protocol_factory = app_context.p2p_protocol_factory

    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    STUN_RECHECK_INTERVAL = float(os.environ.get("STUN_RECHECK_INTERVAL_SEC", "300"))
    udp_listener_active = False # This state might need to be part of app_context if it persists across calls
    loop = asyncio.get_running_loop()
    last_stun_recheck_time = 0.0
    
    active_rendezvous_ws_local: Optional[websockets.WebSocketClientProtocol] = None 

    while not app_context.get_stop_signal():
        p2p_listener_transport_local_ref = None 
        active_rendezvous_ws_local = None
        app_context.set_active_rendezvous_websocket(None) 
        try:
            async with websockets.connect(rendezvous_ws_url, 
                                        ping_interval=ping_interval, 
                                        ping_timeout=ping_timeout,
                                        proxy=None) as ws_to_rendezvous:
                active_rendezvous_ws_local = ws_to_rendezvous
                app_context.set_active_rendezvous_websocket(ws_to_rendezvous) 
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await active_rendezvous_ws_local.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                stun_result_initial = await discover_stun_endpoint_func(worker_id, internal_udp_port, default_stun_host, default_stun_port)
                stun_success_initial = False
                if stun_result_initial:
                    app_context.set_stun_discovered_ip(stun_result_initial[0])
                    app_context.set_stun_discovered_port(stun_result_initial[1])
                    try:
                        await active_rendezvous_ws_local.send(
                            json.dumps({
                                "type": "update_udp_endpoint",
                                "udp_ip": stun_result_initial[0],
                                "udp_port": stun_result_initial[1],
                            })
                        )
                        print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({stun_result_initial[0]}:{stun_result_initial[1]}) to Rendezvous.")
                        stun_success_initial = True
                    except Exception as send_e:
                        print(f"Worker '{worker_id}': Error sending STUN endpoint to Rendezvous: {type(send_e).__name__} - {send_e}")
                        stun_success_initial = True 
                else:
                    print(f"Worker '{worker_id}': Initial STUN discovery failed to get an endpoint.")
                    app_context.set_stun_discovered_ip(None) 
                    app_context.set_stun_discovered_port(None)

                last_stun_recheck_time = time.monotonic()

                if stun_success_initial and not udp_listener_active: 
                    if not app_context.get_p2p_transport():
                        try:
                            _transport, _protocol = await loop.create_datagram_endpoint(
                                p2p_protocol_factory,
                                local_addr=('0.0.0.0', internal_udp_port)
                            )
                            p2p_listener_transport_local_ref = _transport
                            app_context.set_p2p_protocol_instance(_protocol)
                            await asyncio.sleep(0.1)
                            if app_context.get_p2p_transport(): 
                                print(f"Worker '{worker_id}': Asyncio P2P UDP listener appears active on 0.0.0.0:{internal_udp_port}.")
                                udp_listener_active = True 
                            else: print(f"Worker '{worker_id}': P2P UDP listener transport not set globally after create_datagram_endpoint on 0.0.0.0:{internal_udp_port}.")
                        except Exception as e_udp_listen:
                            print(f"Worker '{worker_id}': Failed to create P2P UDP datagram endpoint on 0.0.0.0:{internal_udp_port}: {e_udp_listen}")
                
                while not app_context.get_stop_signal():
                    try:
                        message_raw = await asyncio.wait_for(active_rendezvous_ws_local.recv(), timeout=ping_interval) 
                        print(f"Worker '{worker_id}': Message from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")
                        if msg_type == "p2p_connection_offer":
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port:
                                print(f"Worker '{worker_id}': Received P2P offer for peer '{peer_id}' at {peer_ip}:{peer_port}")
                                asyncio.create_task(attempt_hole_punch_when_ready(app_context, peer_ip, int(peer_port), peer_id))
                        elif msg_type == "udp_endpoint_ack": print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                        elif msg_type == "echo_response": print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                        else: print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: pass
                    except websockets.exceptions.ConnectionClosed as e_conn_closed: 
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server during recv: {e_conn_closed}")
                        break 
                    except Exception as e_recv: 
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv}") 
                        break 
                    
                    current_time = time.monotonic()
                    if current_time - last_stun_recheck_time > STUN_RECHECK_INTERVAL:
                        print(f"Worker '{worker_id}': Performing periodic STUN UDP endpoint check (interval: {STUN_RECHECK_INTERVAL}s)...")
                        try:
                            old_ip = app_context.get_stun_discovered_ip()
                            old_port = app_context.get_stun_discovered_port()
                            stun_recheck_result = await discover_stun_endpoint_func(worker_id, internal_udp_port, default_stun_host, default_stun_port)
                            if stun_recheck_result:
                                new_ip, new_port = stun_recheck_result
                                if new_ip != old_ip or new_port != old_port or old_ip is None:
                                    app_context.set_stun_discovered_ip(new_ip)
                                    app_context.set_stun_discovered_port(new_port)
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint changed from {old_ip}:{old_port} to {new_ip}:{new_port}. Update will be sent.")
                                else:
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint re-verified and unchanged: {new_ip}:{new_port}. Update will be (re)sent.")
                                try:
                                    await active_rendezvous_ws_local.send(
                                        json.dumps({
                                            "type": "update_udp_endpoint",
                                            "udp_ip": new_ip, 
                                            "udp_port": new_port,
                                        })
                                    )
                                    print(f"Worker '{worker_id}': Sent updated STUN UDP endpoint ({new_ip}:{new_port}) to Rendezvous.")
                                except Exception as send_e:
                                    print(f"Worker '{worker_id}': Error sending updated STUN endpoint to Rendezvous: {type(send_e).__name__} - {send_e}")
                            else:
                                print(f"Worker '{worker_id}': Periodic STUN UDP endpoint discovery failed logically. Will retry after interval.")
                            last_stun_recheck_time = current_time
                        except websockets.exceptions.ConnectionClosed as e_stun_conn_closed:
                            print(f"Worker '{worker_id}': Connection closed during periodic STUN update: {e_stun_conn_closed}. Will attempt to reconnect.")
                            break 
                        except Exception as e_stun_general:
                            print(f"Worker '{worker_id}': Error during periodic STUN UDP endpoint check: {type(e_stun_general).__name__} - {e_stun_general}. Will retry STUN after interval.")
                            last_stun_recheck_time = current_time 
                if app_context.get_stop_signal(): break 
        except websockets.exceptions.ConnectionClosed as e_outer_closed:
            print(f"Worker '{worker_id}': Rendezvous WS connection closed before or during connect: {e_outer_closed}")
        except Exception as e_ws_connect: 
            print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
        finally:
            app_context.set_active_rendezvous_websocket(None)
            active_rendezvous_ws_local = None
            app_context.set_p2p_protocol_instance(None)
            if app_context.get_p2p_transport() is None:
                udp_listener_active = False

        if not app_context.get_stop_signal(): await asyncio.sleep(10)
        else: break