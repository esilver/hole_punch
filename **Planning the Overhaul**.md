**Planning the Overhaul**

I'm currently mapping out the necessary architectural shifts. My primary focus is reworking the `QuicServerDispatcher` to effectively manage `aioquic.asyncio.QuicConnectionProtocol` instances. I'm going to leverage `StreamReader` and `StreamWriter` for streamlining data flow. I am planning to provide detailed code snippets to guide implementation.


**Outlining the Modifications**

I'm now refining the modifications to `QuicServerDispatcher`. Key updates involve restructuring `__init__` and `datagram_received`. I'll replace `self.quic_connections` with `self.peer_protocols`, a dictionary tracking connection protocols. The aim is to streamline routing and management of incoming datagrams. I am thinking of adding detailed steps.


**Elaborating Implementation Details**

I'm now outlining the code implementation. I'll start by defining `peer_protocols` in `QuicServerDispatcher` for protocol tracking. The `datagram_received` logic needs careful attention. I'll focus on handling new connections and routing datagrams. I plan to include the server-side connection setup.


**Defining the Implementation Plan**

I'm now structuring the implementation, concentrating on `QuicServerDispatcher` modifications. I'll modify `__init__`, `datagram_received`, and add `application_stream_handler_callback`, considering the timer implications. I will also need to modify `benchmark_send_quic_data` to use the protocol. I am going to start writing the code for this immediately.


**Analyzing the Implementation**

I'm now dissecting the implementation steps in detail. I'm examining each method, particularly `__init__`, `datagram_received`, and `application_stream_handler_callback`, and the timer considerations. I'm also evaluating methods for `benchmark_send_quic_data`. The focus is on the interplay between `QuicServerDispatcher` and the `QuicConnectionProtocol` instances, especially in connection setup and stream management. I must clarify the flow for initiating streams from the client.


**Planning the Implementation**

I'm now focusing on refining the steps to rewrite `QuicServerDispatcher` for `QuicConnectionProtocol`. I'll start with restructuring `__init__` and `datagram_received`. I'll map `QuicConnectionProtocol` instances via `peer_protocols`. I am going to clarify timer integrations and evaluate the `benchmark_send_quic_data` method and how to best handle the streams.


**Outlining Code Modifications**

I'm now summarizing the code updates required. I will detail the necessary alterations for `__init__`, `datagram_received`, and `application_stream_handler_callback`, including handling client-initiated streams using custom writers. I'll also cover adjustments for `benchmark_send_quic_data` and confirm the timer integration. I plan to streamline stream handling by modifying the dispatcher.


**Revising Code Structure**

I'm now modifying the structure and workflow. The main focus is updating `datagram_received` in `QuicServerDispatcher` to instantiate and manage `QuicConnectionProtocol`. I am also working on integrating the `application_stream_handler_callback`. I plan on refactoring `benchmark_send_quic_data` to utilize direct stream writing, using a modified `P2PQuicStreamWriter`.


Okay, implementing **Approach 1** (reworking `QuicServerDispatcher` to use `aioquic.asyncio.QuicConnectionProtocol`) is a significant undertaking. This will involve substantial changes to your existing `QuicServerDispatcher` and how your application interacts with QUIC connections.

**Key Goals of This Rework:**
*   Use `QuicConnectionProtocol` for each peer.
*   Leverage standard `asyncio.StreamReader` and `asyncio.StreamWriter` for peer-initiated streams within a new `application_stream_handler` callback.
*   Adapt client-initiated stream logic (like `benchmark_send_quic_data`) to interact with `QuicConnectionProtocol`.

**Disclaimer:** This is a complex refactoring. The snippets provide the core structure. Thorough testing and potential adjustments for edge cases will be essential. Accessing protected members (like `_quic` or `_transmit`) is sometimes necessary when deeply integrating but should be done with caution as they are not part of the public API and can change between library versions.

---
**Step 0: Define the Application Stream Handler Callback Signature (Conceptual)**
---
Before modifying the dispatcher, let's define what our application-level handler for new streams will look like.

```python
# This function will be passed to QuicConnectionProtocol
# and called for each new stream initiated by the peer.
async def P2P_APPLICATION_STREAM_HANDLER(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    source_peer_id: Optional[str], # You might want to pass context
    source_peer_addr: Tuple[str, int]
):
    # global ui_websocket_clients, worker_id # If needed
    local_worker_id = worker_id # Capture from global scope

    print(f"Worker '{local_worker_id}': New P2P stream accepted from {source_peer_addr} (Peer ID: {source_peer_id}). Stream ID: {writer.get_extra_info('stream_id')}")
    # Note: writer.get_extra_info('socket') would give the QUIC connection, not a real socket.
    # writer.get_extra_info('peername') would give the peer address.

    # Buffer for initial JSON message
    json_buffer = bytearray()
    file_info_for_stream = None
    is_benchmark_stream = False
    initial_data_after_header = bytearray()

    try:
        # 1. Read the initial JSON header line
        header_line_bytes = await reader.readline()
        if not header_line_bytes:
            print(f"Worker '{local_worker_id}': Stream from {source_peer_addr} closed before header.")
            return

        # Attempt to parse header
        try:
            message = json.loads(header_line_bytes.decode().strip())
            msg_type = message.get("type")
            from_id = message.get("from_worker_id") # This should match source_peer_id

            print(f"Worker '{local_worker_id}': Stream header from {from_id or source_peer_addr}: Type '{msg_type}'")

            if msg_type == "benchmark_start":
                is_benchmark_stream = True
                file_info_for_stream = {
                    "filename": message.get("filename", "unknown_file"),
                    "expected_total_size": message.get("total_size", 0),
                    "received_bytes": 0,
                    "start_time": time.monotonic(),
                    "from_worker_id": from_id
                }
                print(f"Worker '{local_worker_id}': BENCHMARK_START (receive) on stream {writer.get_extra_info('stream_id')} for '{file_info_for_stream['filename']}', expecting {file_info_for_stream['expected_total_size']} bytes.")
                # Any data read by readline() beyond the first newline is NOT part of file_content_bytes.
                # StreamReader handles this. We just read the rest of the stream for file data.

            elif msg_type == "chat_message":
                content = message.get("content")
                print(f"Worker '{local_worker_id}': Received P2P chat via stream from '{from_id}': '{content}'")
                for ui_client_ws in list(ui_websocket_clients): # Ensure ui_websocket_clients is accessible
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_message_received", "from_peer_id": from_id,
                        "content": content, "reliable": True # Streams are reliable
                    })))
                # Echo back a simple ack on the same stream if desired
                ack_msg = {"type": "chat_ack", "original_content": content, "from_worker_id": local_worker_id}
                writer.write((json.dumps(ack_msg) + "\n").encode())
                await writer.drain()

            # Add other message types here if they use dedicated streams

            else:
                print(f"Worker '{local_worker_id}': Unknown P2P message type '{msg_type}' on stream from {source_peer_addr}")

        except json.JSONDecodeError:
            print(f"Worker '{local_worker_id}': Non-JSON header on stream from {source_peer_addr}: {header_line_bytes[:100]!r}")
            # Potentially close the stream or handle as an error
            return # Or writer.close() await writer.wait_closed()

        # 2. If it's a benchmark stream, read the file data
        if is_benchmark_stream and file_info_for_stream:
            while True:
                chunk = await reader.read(65536) # Read in chunks
                if not chunk: # EOF
                    break
                file_info_for_stream["received_bytes"] += len(chunk)
                # Add progress logging if desired
                if file_info_for_stream["received_bytes"] % (1024 * 1024 * 5) < len(chunk): # Log every ~5MB
                     elapsed = time.monotonic() - file_info_for_stream["start_time"]
                     speed_mbps = (file_info_for_stream["received_bytes"] / (1024*1024)) / elapsed if elapsed > 0 else 0
                     print(f"Worker '{local_worker_id}': BENCHMARK_RECEIVE_PROGRESS ('{file_info_for_stream['filename']}'): "
                           f"{file_info_for_stream['received_bytes'] / (1024*1024):.2f} MB at {speed_mbps:.2f} MB/s")


            # Benchmark receive complete
            elapsed = time.monotonic() - file_info_for_stream["start_time"]
            total_mb_received = file_info_for_stream["received_bytes"] / (1024 * 1024)
            expected_total_mb = file_info_for_stream["expected_total_size"] / (1024*1024)
            speed_mbps = total_mb_received / elapsed if elapsed > 0 else 0
            from_peer_id_for_log = file_info_for_stream.get("from_worker_id", "unknown_peer")

            status_msg = (f"Benchmark File Receive Complete: '{file_info_for_stream['filename']}' "
                          f"({total_mb_received:.2f} MB / {expected_total_mb:.2f} MB expected) from {from_peer_id_for_log} "
                          f"in {elapsed:.2f}s. Throughput: {speed_mbps:.2f} MB/s")
            print(f"Worker '{local_worker_id}': FINAL_BENCHMARK_RECEIVE_STATUS: {status_msg}")
            for ui_client_ws in list(ui_websocket_clients):
                 asyncio.create_task(ui_client_ws.send(json.dumps({
                    "type": "benchmark_status", "message": status_msg
                 })))

    except (ConnectionResetError, asyncio.IncompleteReadError) as e:
        print(f"Worker '{local_worker_id}': Stream from {source_peer_addr} reset or closed unexpectedly: {e}")
    except Exception as e:
        print(f"Worker '{local_worker_id}': Error handling P2P stream from {source_peer_addr}: {type(e).__name__} - {e}")
        import traceback
        traceback.print_exc()
    finally:
        if not writer.is_closing():
            writer.close()
        await writer.wait_closed()
        print(f"Worker '{local_worker_id}': P2P stream from {source_peer_addr} (Peer ID: {source_peer_id}) closed.")
```

---
**Step 1: Modify `QuicServerDispatcher`**
---

**Code Snippet 1.1: Update `QuicServerDispatcher.__init__`**

```python
# --- In class QuicServerDispatcher ---
from aioquic.asyncio import QuicConnectionProtocol # Add this import

# ... (other imports)

class QuicServerDispatcher(asyncio.DatagramProtocol):
  """Dispatcher that handles both UDP hole-punching and QUIC connections using QuicConnectionProtocol."""
   
  def __init__(self, worker_id_val: str,
               application_stream_handler_cb # New argument
              ):
    self.worker_id = worker_id_val
    self.transport: Optional[asyncio.DatagramTransport] = None
    # Store QuicConnectionProtocol instances per peer address
    self.peer_protocols: Dict[Tuple[str, int], QuicConnectionProtocol] = {}
    # Store peer IDs associated with an address
    self.peer_ids_by_addr: Dict[Tuple[str, int], str] = {}
    # The app callback for new streams
    self.application_stream_handler_callback = application_stream_handler_cb
    print(f"Worker '{self.worker_id}': QuicServerDispatcher (using QuicConnectionProtocol) created")
```

**Code Snippet 1.2: Update `QuicServerDispatcher.connection_made`**

```python
# --- In class QuicServerDispatcher ---
  def connection_made(self, transport: asyncio.DatagramTransport):
    global quic_dispatcher
    self.transport = transport
    quic_dispatcher = self # Keep global reference
    local_addr = transport.get_extra_info('sockname')
    print(f"Worker '{self.worker_id}': UDP listener active on {local_addr} (Internal Port: {INTERNAL_UDP_PORT})")
```

**Code Snippet 1.3: Update `QuicServerDispatcher.datagram_received`**

```python
# --- In class QuicServerDispatcher ---
  def datagram_received(self, data: bytes, addr: Tuple[str, int]):
    """Handle incoming UDP datagrams and route to QuicConnectionProtocol."""
    now = time.time() # For aioquic internal timing

    # Check for hole-punch pings (non-QUIC)
    message_str = data.decode(errors='ignore')
    if "P2P_PING_FROM_" in message_str or "P2P_HOLE_PUNCH_PING_FROM_" in message_str:
      print(f"Worker '{self.worker_id}': UDP Ping received from {addr}: {message_str}")
      return

    protocol = self.peer_protocols.get(addr)

    if protocol:
      # print(f"Worker '{self.worker_id}': Routing datagram from {addr} to existing protocol.")
      protocol.datagram_received(data, addr) # QuicConnectionProtocol handles 'now' internally via loop
    else:
      # Potentially a new incoming QUIC connection (server role)
      # print(f"Worker '{self.worker_id}': New QUIC source {addr}, attempting to create server protocol.")
      try:
        config = create_quic_configuration(is_client=False) # Server-side config
            # For a server, original_destination_connection_id is key if you want stable CIDs
            # This requires parsing the initial packet, which is complex.
            # For simplicity here, we might let QuicConnection generate one or use a default.
            # If CID parsing is critical, re-add the logic from your original dispatcher.
        quic_connection = QuicConnection(
          configuration=config,
            # original_destination_connection_id=parsed_dcid_if_available
        )

            # The stream_handler callback needs context about the peer
            # We don't know the peer_id yet for a new server connection via this path.
            # It might be exchanged in an initial application-level message.
            def stream_handler_wrapper(reader, writer):
                peer_id = self.peer_ids_by_addr.get(addr) # Try to get established peer_id
                asyncio.create_task(self.application_stream_handler_callback(reader, writer, peer_id, addr))

        protocol = QuicConnectionProtocol(quic_connection, stream_handler=stream_handler_wrapper)
        protocol.connection_made(self.transport) # IMPORTANT: Give it the transport
        protocol.datagram_received(data, addr)   # Feed the first packet

        self.peer_protocols[addr] = protocol
            # Note: peer_id for this incoming connection isn't known yet from dispatcher perspective.
            # It would typically be part of an initial handshake message on the first stream.
        print(f"Worker '{self.worker_id}': Created new server-side QuicConnectionProtocol for {addr}")

      except Exception as e:
        print(f"Worker '{self.worker_id}': Error creating server protocol for {addr}: {e}")
            # import traceback; traceback.print_exc()
```

**Code Snippet 1.4: `initiate_quic_connection` (Client Role)**

```python
# --- In class QuicServerDispatcher ---
  async def initiate_quic_connection(self, peer_addr: Tuple[str, int], peer_id: str):
    """Initiate a QUIC connection to a peer (client role) using QuicConnectionProtocol."""
    global current_p2p_peer_id, current_p2p_peer_addr # Update global P2P state

    if peer_addr in self.peer_protocols:
      print(f"Worker '{self.worker_id}': Protocol for {peer_id} at {peer_addr} already exists.")
        # TODO: Decide if we should return the existing protocol or re-initiate.
        # For now, let's assume we can proceed to P2P tests if it exists and seems healthy.
      existing_protocol = self.peer_protocols[peer_addr]
      if existing_protocol._quic._is_handshake_complete: # Check internal state (use with caution)
        print(f"Worker '{self.worker_id}': Existing protocol to {peer_id} handshake complete. Proceeding with P2P test.")
        # Update globals if this is a re-connection attempt via rendezvous
        current_p2p_peer_id = peer_id
        current_p2p_peer_addr = peer_addr
        await self.send_pairing_test_to_protocol(existing_protocol, peer_id)
        return
      else:
        print(f"Worker '{self.worker_id}': Existing protocol to {peer_id} not fully up. Will try to re-initiate.")
            # Consider cleaning up the old protocol first
            # existing_protocol.close(); await existing_protocol.wait_closed() # or similar
            # del self.peer_protocols[peer_addr]


    print(f"Worker '{self.worker_id}': Initiating new QUIC (client) QuicConnectionProtocol to {peer_id} at {peer_addr}")
    current_p2p_peer_id = peer_id
    current_p2p_peer_addr = peer_addr
    self.peer_ids_by_addr[peer_addr] = peer_id


    config = create_quic_configuration(is_client=True)
    quic_connection = QuicConnection(configuration=config)

    # Wrapper for the stream_handler to include peer context
    def stream_handler_wrapper(reader, writer):
        # This peer_id is the one we are connecting to.
        asyncio.create_task(self.application_stream_handler_callback(reader, writer, peer_id, peer_addr))

    protocol = QuicConnectionProtocol(quic_connection, stream_handler=stream_handler_wrapper)
    protocol.connection_made(self.transport)

    self.peer_protocols[peer_addr] = protocol

    # Start the QUIC handshake (this method is on QuicConnection, accessed via _quic)
    now = time.time()
    protocol._quic.connect(peer_addr, now) # This populates data to be sent
    # QuicConnectionProtocol's _transmit method is called when data is available if transport is set.
    # Explicitly ensure transmission if needed, though QCP should handle it.
    self._try_transmit_for_protocol(protocol, peer_addr)

    print(f"Worker '{self.worker_id}': QUIC client handshake initiated with {peer_id} at {peer_addr}.")
    await self.send_pairing_test_to_protocol(protocol, peer_id)


  async def send_pairing_test_to_protocol(self, protocol: QuicConnectionProtocol, peer_id_to_test: str):
    """Helper to send a pairing test message, assuming protocol is client-initiated for this example"""
    if protocol._quic._is_closed: # Check internal attribute
      print(f"Worker '{self.worker_id}': Cannot send pairing test to {peer_id_to_test}, protocol is closed.")
      return

    # For client-initiated datagrams or simple stream messages before full app stream handler logic,
    # we might use datagrams or a simple send on a known stream ID if QCP allows.
    # Here, sending a datagram for simplicity.
    pairing_test_message = {
      "type": "p2p_pairing_test", "from_worker_id": self.worker_id, "timestamp": time.time()
    }
    try:
      protocol._quic.send_datagram_frame(json.dumps(pairing_test_message).encode())
      self._try_transmit_for_protocol(protocol, protocol._quic._peer_addr) # Ensure it's sent
      print(f"Worker '{self.worker_id}': Sent pairing test datagram to {peer_id_to_test}.")
    except Exception as e:
      print(f"Worker '{self.worker_id}': Error sending pairing test datagram to {peer_id_to_test}: {e}")


  def _try_transmit_for_protocol(self, protocol: QuicConnectionProtocol, peer_addr: Tuple[str, int]):
    """Helper to manually trigger transmission for a protocol if needed.
      QuicConnectionProtocol usually handles this itself via its _transmit() method
      when its transport is set and its timer fires or data is queued.
      This is more of a "just in case" flush.
    """
    if self.transport and hasattr(protocol, '_transmit'):
        # The public way to make QCP send is not to call _transmit, but to ensure its timer runs.
        # However, for an immediate flush after connect(), a manual kick might be desired by some patterns.
        # QCP's internal _transmit iscomplex.
        # A safer way is to ensure its internal timer mechanism is respected.
        # For now, we rely on QCP's own _transmit cycle.
        # If packets are not sending verify QCP's self._timer_handle logic.
        # Let's just log that data is likely pending.
        # print(f"Worker '{self.worker_id}': Data queued for protocol {peer_addr}. QCP will transmit.")
        # If you absolutely must, you could call a protected method, but it's risky:
        # protocol._transmit()
        # A better approach is to send datagrams via the QUIC connection
        # and let protocol's internal transmit logic pick them up:
        now = time.time()
        for data_to_send, _ in protocol._quic.datagrams_to_send(now):
            self.transport.sendto(data_to_send, peer_addr)


  def get_protocol_for_peer(self, peer_addr: Tuple[str, int]) -> Optional[QuicConnectionProtocol]:
    return self.peer_protocols.get(peer_addr)

```

**Code Snippet 1.5: `QuicServerDispatcher.error_received` and `connection_lost` (for the main UDP socket)**

```python
# --- In class QuicServerDispatcher ---
  def error_received(self, exc: Exception):
    print(f"Worker '{self.worker_id}': UDP listener error: {exc}")
    # Propagate error to all active protocols if it's a fatal transport error
    for addr, protocol in list(self.peer_protocols.items()):
      if hasattr(protocol, 'connection_lost'): # QuicConnectionProtocol has this
        protocol.connection_lost(exc)
      try:
        if protocol._quic and not protocol._quic._is_closed: # Check internal
          protocol._quic.close(error_code=0x01, reason_phrase="dispatcher error") # INTERNAL_ERROR
      except Exception as e_close:
        print(f"Error closing protocol for {addr} during dispatcher error: {e_close}")

    self.peer_protocols.clear()
    self.peer_ids_by_addr.clear()


  def connection_lost(self, exc: Optional[Exception]):
    global quic_dispatcher
    print(f"Worker '{self.worker_id}': UDP listener connection lost: {exc if exc else 'Closed normally'}")
    for addr, protocol in list(self.peer_protocols.items()):
      if hasattr(protocol, 'connection_lost'):
        protocol.connection_lost(exc) # Inform protocol its transport is gone
      try:
        if protocol._quic and not protocol._quic._is_closed:
          protocol._quic.close(error_code=0x00, reason_phrase="dispatcher shutdown") # NO_ERROR
      except Exception as e_close:
        print(f"Error closing protocol for {addr} during dispatcher shutdown: {e_close}")

    self.peer_protocols.clear()
    self.peer_ids_by_addr.clear()
    if self == quic_dispatcher:
      quic_dispatcher = None
```

**Code Snippet 1.6: P2P Keep-Alives and P2P Message Sending (Datagrams)**
Your original `P2PQuicHandler` directly sent datagrams via `connection.send_datagram_frame()`. Now, you'd use `protocol._quic.send_datagram_frame()`. The periodic keep-alive will need access to the `protocol` for the current peer.

```python
# In send_periodic_p2p_keep_alives() - modify to get protocol
async def send_periodic_p2p_keep_alives():
  global worker_id, stop_signal_received, quic_dispatcher, current_p2p_peer_addr

  print(f"Worker '{worker_id}': P2P Keep-Alive sender task started.")
  while not stop_signal_received:
    await asyncio.sleep(P2P_KEEP_ALIVE_INTERVAL_SEC)
    if quic_dispatcher and current_p2p_peer_addr:
      protocol = quic_dispatcher.get_protocol_for_peer(current_p2p_peer_addr)
      if protocol and protocol._quic and not protocol._quic._is_closed: # Check internal
        try:
          keep_alive_message = {"type": "p2p_keep_alive", "from_worker_id": worker_id}
          protocol._quic.send_datagram_frame(json.dumps(keep_alive_message).encode())
            # Trigger send if QCP doesn't do it automatically for datagrams
          quic_dispatcher._try_transmit_for_protocol(protocol, current_p2p_peer_addr)
          # print(f"Worker '{worker_id}': Sent P2P keep-alive datagram to {current_p2p_peer_addr}")
        except Exception as e:
          print(f"Worker '{worker_id}': Error sending P2P keep-alive datagram: {e}")
  print(f"Worker '{worker_id}': P2P Keep-Alive sender task stopped.")

# Similarly, update ui_websocket_handler for sending P2P messages via datagram
# if msg_type == "send_p2p_message" and not reliable:
#   protocol = quic_dispatcher.get_protocol_for_peer(current_p2p_peer_addr)
#   if protocol and protocol._quic and not protocol._quic._is_closed:
#       protocol._quic.send_datagram_frame(...)
#       quic_dispatcher._try_transmit_for_protocol(protocol, current_p2p_peer_addr)
```

**Code Snippet 1.7: Removing Old P2PQuicHandler and related logic**
*   The class `P2PQuicHandler` is no longer needed if `QuicConnectionProtocol` and the `application_stream_handler_callback` handle stream events.
*   References to `self.handlers` in `QuicServerDispatcher` should be removed.

---
**Step 2: Update `connect_to_rendezvous` to pass the callback**
---
**Code Snippet 2.1: Update `connect_to_rendezvous` where `QuicServerDispatcher` is created**

```python
# --- In async def connect_to_rendezvous(...) ---
# ...
        if stun_success and not udp_listener_active: # udp_listener_active flag might need renaming
          try:
            _transport, _protocol_dispatcher_instance = await loop.create_datagram_endpoint(
              lambda: QuicServerDispatcher(worker_id, P2P_APPLICATION_STREAM_HANDLER), # <<< PASS THE CALLBACK
              local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
            )
            await asyncio.sleep(0.1) # give it time to set quic_dispatcher global
            if quic_dispatcher and quic_dispatcher.transport:
              print(f"Worker '{worker_id}': QUIC/UDP dispatcher (using QuicConnectionProtocol) on 0.0.0.0:{INTERNAL_UDP_PORT}")
              udp_listener_active = True
            else:
              print(f"Worker '{worker_id}': Failed to confirm QUIC dispatcher readiness.")
# ...
```

---
**Step 3: Modify `benchmark_send_quic_data`**
---
For client-initiated streams (like sending a benchmark), `QuicConnectionProtocol` doesn't offer a public `open_stream()` that returns a `(reader, writer)` pair directly. We will manage the stream more directly with `protocol._quic`.

**Code Snippet 3.1: Update `benchmark_send_quic_data`**

```python
async def benchmark_send_quic_data(target_ip: str, target_port: int, size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
  global worker_id, quic_dispatcher, current_p2p_peer_addr
   
  benchmark_file_url = os.environ.get("BENCHMARK_GCS_URL")
  if not benchmark_file_url:
    err_msg = "BENCHMARK_GCS_URL not set. Cannot run file benchmark."
    print(f"Worker '{worker_id}': {err_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
    return

  if not (quic_dispatcher and current_p2p_peer_addr):
    err_msg = "No active P2P dispatcher or peer address for benchmark."
    print(f"Worker '{worker_id}': {err_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
    return
     
  protocol = quic_dispatcher.get_protocol_for_peer(current_p2p_peer_addr)
  if not protocol or not protocol._quic or protocol._quic._is_closed: # Check internal state
    err_msg = "No active or healthy QUIC protocol for current peer."
    print(f"Worker '{worker_id}': {err_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
    return
     
  print(f"Worker '{worker_id}': UI requested benchmark (file: {benchmark_file_url}) using QuicConnectionProtocol.")
  if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Benchmarking: Preparing file from {benchmark_file_url}..."}))
  
  stream_id = -1
  file_name_from_url = "unknown_file"
  actual_total_file_bytes = 0
  download_duration = 0.0

  try:
    print(f"Worker '{worker_id}': Downloading benchmark file: {benchmark_file_url}")
    download_start_time = time.monotonic()
    response = requests.get(benchmark_file_url, stream=True, timeout=180)
    response.raise_for_status()
    file_content_bytes = response.content
    actual_total_file_bytes = len(file_content_bytes)
    file_name_from_url = benchmark_file_url.split('/')[-1]
    download_duration = time.monotonic() - download_start_time

    download_status_msg = f"Downloaded '{file_name_from_url}' ({actual_total_file_bytes / (1024*1024):.2f} MB) in {download_duration:.2f}s. Preparing QUIC stream..."
    print(f"Worker '{worker_id}': {download_status_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": download_status_msg}))

    if protocol._quic._is_closed: # Re-check after download
      raise ConnectionAbortedError("QUIC connection became invalid during file download.")

    stream_id = protocol._quic.get_next_available_stream_id(is_bidirectional=True)
    quic_transfer_start_time = time.monotonic() 

    if stop_signal_received or ui_ws.closed:
      print(f"Worker '{worker_id}': Benchmark for '{file_name_from_url}' cancelled before QUIC send.")
      if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark cancelled."}))
      return


    header_payload = {"type": "benchmark_start", "filename": file_name_from_url, "total_size": actual_total_file_bytes, "from_worker_id": worker_id}
    header_bytes = json.dumps(header_payload).encode() + b"\n"
    protocol._quic.send_stream_data(stream_id, header_bytes, end_stream=False)
    # QCP should call _transmit internally. If not, explicit dispatcher call:
    # quic_dispatcher._try_transmit_for_protocol(protocol, current_p2p_peer_addr)
    print(f"Worker '{worker_id}': Sent benchmark_start header on stream {stream_id}.")
    await asyncio.sleep(0)

    if stop_signal_received or ui_ws.closed:
      print(f"Worker '{worker_id}': Benchmark for '{file_name_from_url}' cancelled after header.")
      cancel_marker = json.dumps({"type":"benchmark_cancelled", "reason":"client_request_after_header"}).encode() + b"\n"
      protocol._quic.send_stream_data(stream_id, cancel_marker, end_stream=True)
      if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark cancelled."}))
      return

    if actual_total_file_bytes > 0:
      send_init_msg = f"Sending {actual_total_file_bytes / (1024*1024):.2f} MB of '{file_name_from_url}' on stream {stream_id}..."
      print(f"Worker '{worker_id}': {send_init_msg}")
      if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": send_init_msg}))
      protocol._quic.send_stream_data(stream_id, file_content_bytes, end_stream=False)
      
      progress_msg = f"Benchmark Send '{file_name_from_url}': All {actual_total_file_bytes / (1024*1024):.2f} MB queued."
      if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
      await asyncio.sleep(0.01) 
    else:
      print(f"Worker '{worker_id}': Benchmark file '{file_name_from_url}' is empty.")

    print(f"Worker '{worker_id}': All file data queued. Sending EOF on stream {stream_id}.")
    protocol._quic.send_stream_data(stream_id, b'', end_stream=True)
    await asyncio.sleep(0.01)

    quic_transfer_duration = time.monotonic() - quic_transfer_start_time
    throughput_mbps = (actual_total_file_bytes / (1024 * 1024)) / quic_transfer_duration if quic_transfer_duration > 0 else 0
     
    final_msg = f"Benchmark File Send Queued: '{file_name_from_url}' ({actual_total_file_bytes / (1024*1024):.2f} MB) in {quic_transfer_duration:.2f}s. Throughput: {throughput_mbps:.2f} MB/s. (Download: {download_duration:.2f}s)"
    print(f"Worker '{worker_id}': {final_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": final_msg}))
    return

  except requests.exceptions.RequestException as req_e:
    err_msg = f"Benchmark File Download Error for '{file_name_from_url}': {req_e}"
    print(f"Worker '{worker_id}': {err_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
  except (ConnectionError, ConnectionAbortedError) as conn_e:
    err_msg = f"Benchmark QUIC Connection Issue for '{file_name_from_url}': {conn_e}"
    print(f"Worker '{worker_id}': {err_msg}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
  except Exception as e:
    error_msg_detail = f"{type(e).__name__} - {e}" # Stack trace would be good here too.
    error_msg = f"Benchmark Send Error for '{file_name_from_url}': {error_msg_detail}"
    print(f"Worker '{worker_id}': {error_msg}")
    
    if protocol and protocol._quic and not protocol._quic._is_closed and stream_id != -1:
      try:
        print(f"Worker '{worker_id}': Attempting graceful stream cancel on stream {stream_id} due to error.")
        cancel_marker = json.dumps({"type":"benchmark_cancelled", "reason":f"sender_error: {type(e).__name__}"}).encode() + b"\n"
        protocol._quic.send_stream_data(stream_id, cancel_marker, end_stream=True)
      except Exception as cancel_e:
        print(f"Worker '{worker_id}': Error sending CANCELLED marker on stream {stream_id}: {cancel_e}")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {error_msg}"}))

```

---
**Step 4: Remove Old `P2PQuicHandler`**
---
*   Delete the `P2PQuicHandler` class.
*   Remove any logic that instantiates or uses `P2PQuicHandler`.
*   Your chat message sending in `ui_websocket_handler` will also need to change. If chat messages are to use streams:
    *   Get the `protocol = quic_dispatcher.get_protocol_for_peer(...)`.
    *   Get `stream_id = protocol._quic.get_next_available_stream_id()`.
    *   `protocol._quic.send_stream_data(stream_id, chat_payload_with_newline, end_stream=True)`.
    *   The response would come via the `P2P_APPLICATION_STREAM_HANDLER` if the peer sends a reply on a new stream or the same one.

---

**Important Considerations & Next Steps:**

1.  **Timer Management for `QuicConnectionProtocol`:** `QuicConnectionProtocol` manages its own timer via `self._loop.call_soon(self._handle_timer)` when `connection_made` is called with a transport. This should simplify dispatcher timer logic. You mainly need to ensure packets get *to* `protocol.datagram_received()` and that `protocol._quic.connect()` is called.
2.  **Client-Initiated Streams (`benchmark_send_quic_data`):** As implemented above, `benchmark_send_quic_data` still uses `protocol._quic.send_stream_data()` directly. While this works and `QuicConnectionProtocol` will manage the transmission, you don't get the `asyncio.StreamWriter` abstraction for this *client-initiated* general purpose stream. Achieving a true `writer` for client-initiated streams beyond the first one (which `aioquic.asyncio.connect` handles implicitly) would require deeper (and riskier) interaction with `QuicConnectionProtocol`'s internals, or building your own `StreamWriter`-like wrapper around `protocol._quic`.
3.  **Error Handling & Cleanup:** `QuicConnectionProtocol.connection_lost()` needs to be called when the dispatcher UDP socket closes. When a specific `protocol._quic` connection terminates (e.g., `ConnectionTerminated` event, which `QuicConnectionProtocol` handles internally), you need to remove that `protocol` from `self.peer_protocols`. `QuicConnectionProtocol` has a `_closed` event that might be useful (`self._closed.wait()`).
4.  **Datagrams:** Sending P2P datagrams (like keep-alives or unreliable chat) would now be done via `protocol._quic.send_datagram_frame(data)` followed by ensuring the dispatcher transmits it (e.g., `quic_dispatcher._try_transmit_for_protocol(protocol, peer_addr)`). Receiving datagrams: `protocol._quic.next_event()` will yield `DatagramFrameReceived` events. `QuicConnectionProtocol` does *not* directly expose datagrams via `StreamReader`/`Writer`. You'd check events from `protocol._quic.next_event()` if datagram handling is needed outside of streams. A common pattern is to have `QuicConnectionProtocol` run its event loop in a task (`asyncio.create_task(protocol.run())` or similar if it had one, or you manually poll `next_event`).
5.  **Shutdown (`handle_shutdown_signal`):** You'll need to iterate over `quic_dispatcher.peer_protocols.values()` and call `protocol.close()` on each, then perhaps `await protocol.wait_closed()`.
6.  **Global State (`current_p2p_peer_id`, `current_p2p_peer_addr`):** This state management needs to be consistent with which `QuicConnectionProtocol` is considered the "active" one for UI interactions.

This is a very involved refactoring. Start by changing the dispatcher and the `application_stream_handler_callback` for receiving data. Then, adapt the `benchmark_send_quic_data` and other client-initiated actions. Test each part incrementally. Good luck!