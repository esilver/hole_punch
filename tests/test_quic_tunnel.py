import asyncio
import sys
import types
import unittest

# Stub external modules so quic_tunnel can be imported without optional deps
for mod_name in [
    "aioquic",
    "aioquic.quic",
    "aioquic.quic.configuration",
    "aioquic.quic.connection",
    "aioquic.quic.events",
    "aioquic.quic.packet",
    "cryptography",
    "cryptography.x509",
    "cryptography.x509.oid",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.rsa",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

crypt_mod = sys.modules['cryptography']
crypt_mod.__path__ = []
crypt_mod.__spec__ = types.SimpleNamespace(submodule_search_locations=[])
x509_mod = sys.modules['cryptography.x509']
x509_mod.__package__ = 'cryptography'
sys.modules['cryptography'].x509 = x509_mod
oid_mod = sys.modules['cryptography.x509.oid']
class NameOID:
    COMMON_NAME = object()
oid_mod.NameOID = NameOID

# Stub websockets so ui_server can be imported
ws_mod = types.ModuleType('websockets')
from importlib.machinery import ModuleSpec
ws_mod.__path__ = []
ws_mod.__spec__ = ModuleSpec('websockets', loader=None, is_package=True)
ws_mod.WebSocketServerProtocol = object
ws_mod.WebSocketClientProtocol = object
ws_mod.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
http_mod = types.ModuleType('websockets.http')
http_mod.__package__ = 'websockets'
http_mod.__spec__ = ModuleSpec('websockets.http', loader=None)
ws_mod.http = http_mod
class Headers(list):
    pass
http_mod.Headers = Headers
sys.modules['websockets'] = ws_mod
sys.modules['websockets.http'] = http_mod

# Minimal stubs used by quic_tunnel
class QuicConfiguration:
    def __init__(self, is_client=False, alpn_protocols=None, idle_timeout=0):
        self.is_client = is_client
        self.connection_id_length = 8
    def load_cert_chain(self, *a, **kw):
        pass

class QuicConnection:
    def __init__(self, *a, **kw):
        self.sent = []
        self.next_id = 0
    def connect(self, addr, now=None):
        pass
    def datagrams_to_send(self, now=None):
        return []
    def receive_datagram(self, data, addr, now=None):
        pass
    def next_event(self):
        return None
    def get_next_available_stream_id(self, is_unidirectional=False):
        self.next_id += 1
        return self.next_id - 1
    def send_stream_data(self, stream_id, data, end_stream=False):
        self.sent.append((stream_id, data, end_stream))
    def get_timer(self):
        return None
    def handle_timer(self, now=None):
        pass
    def close(self, error_code=0, reason_phrase=""):
        pass

sys.modules["aioquic.quic.configuration"].QuicConfiguration = QuicConfiguration
sys.modules["aioquic.quic.connection"].QuicConnection = QuicConnection

class DummyEvent: pass
for name in ["StreamDataReceived", "HandshakeCompleted", "ConnectionTerminated", "PingAcknowledged"]:
    setattr(sys.modules["aioquic.quic.events"], name, DummyEvent)

class QuicErrorCode:
    INTERNAL_ERROR = 1
sys.modules["aioquic.quic.packet"].QuicErrorCode = QuicErrorCode

from ui_server import broadcast_to_all_ui_clients  # uses stub websockets if not installed
from quic_tunnel import QuicTunnel


class TestQuicTunnelSendData(unittest.TestCase):
    def test_send_app_data_opens_stream(self):
        qt = QuicTunnel("worker", ("127.0.0.1", 9999), lambda d, a: None, True)
        self.assertIsNone(qt._quic_stream_id)
        asyncio.run(qt.send_app_data(b"hello"))
        self.assertIsNotNone(qt._quic_stream_id)
        self.assertEqual(qt.quic_connection.sent[0][1], b"hello")

class EventQueueQuicConnection(QuicConnection):
    """QuicConnection stub that returns predefined events."""
    def __init__(self, events=None):
        super().__init__()
        self.events = list(events or [])
    def next_event(self):
        return self.events.pop(0) if self.events else None

class TestQuicTunnelEvents(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_completion_sets_flag(self):
        HandshakeCompleted = sys.modules["aioquic.quic.events"].HandshakeCompleted
        ev = HandshakeCompleted()
        ev.alpn_protocol = "test"
        qc = EventQueueQuicConnection([ev])
        qt = QuicTunnel("worker", ("127.0.0.1", 9999), lambda d, a: None, False)
        qt.quic_connection = qc
        qt._process_quic_events()
        await asyncio.sleep(0)
        self.assertTrue(qt.handshake_completed)

    async def test_connection_terminated_triggers_close(self):
        TermClass = type("TermEvent", (), {})
        sys.modules["aioquic.quic.events"].ConnectionTerminated = TermClass
        import quic_tunnel
        quic_tunnel.ConnectionTerminated = TermClass
        ev = TermClass()
        ev.error_code = 0
        ev.reason_phrase = "bye"
        qc = EventQueueQuicConnection([ev])
        qt = QuicTunnel("worker", ("127.0.0.1", 9999), lambda d, a: None, False)
        qt.quic_connection = qc
        closed = False
        async def dummy_close():
            nonlocal closed
            closed = True
        qt.close = dummy_close
        qt._process_quic_events()
        await asyncio.sleep(0)
        self.assertTrue(qt._connection_terminated)
        self.assertTrue(closed)
