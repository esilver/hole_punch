import asyncio
import json
import sys
import types
import unittest
from importlib.machinery import ModuleSpec

# Stub websockets module with minimal features for ui_server import
ws_mod = types.ModuleType('websockets')
ws_mod.__path__ = []
ws_mod.__spec__ = ModuleSpec('websockets', loader=None, is_package=True)
class ConnectionClosed(Exception):
    pass
ws_mod.exceptions = types.SimpleNamespace(ConnectionClosed=ConnectionClosed)

class WebSocketServerProtocol:
    def __init__(self):
        self.sent = []
        self.recv_queue = asyncio.Queue()
        self.remote_address = ('test', 0)
        self.closed = False
    async def send(self, data):
        self.sent.append(data)
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self.closed and self.recv_queue.empty():
            raise ConnectionClosed()
        return await self.recv_queue.get()
    async def close(self, code=1000, reason=""):
        self.closed = True

ws_mod.WebSocketServerProtocol = WebSocketServerProtocol
ws_mod.WebSocketClientProtocol = WebSocketServerProtocol

http_mod = types.ModuleType('websockets.http')
http_mod.__package__ = 'websockets'
http_mod.__spec__ = ModuleSpec('websockets.http', loader=None)
ws_mod.http = http_mod
class Headers(list):
    pass
http_mod.Headers = Headers

sys.modules['websockets'] = ws_mod
sys.modules['websockets.http'] = http_mod

from ui_server import ui_websocket_handler, process_http_request, broadcast_to_all_ui_clients


def dummy_get_p2p_info():
    return (None, None, None)

def dummy_get_rendezvous_ws():
    return None

async def dummy_benchmark_sender(*a, **kw):
    pass

async def dummy_delayed_exit(*a, **kw):
    pass

async def dummy_discover_stun(*a, **kw):
    return None

def dummy_update_main(*a, **kw):
    pass

def dummy_get_quic_engine():
    return None

class FakeWebSocket(WebSocketServerProtocol):
    pass

class TestUIServer(unittest.IsolatedAsyncioTestCase):
    async def test_process_http_request_health(self):
        code, headers, body = await process_http_request('/health', Headers())
        self.assertEqual(code, 200)
        self.assertIn(b'OK', body)

    async def test_ui_websocket_handler_basic(self):
        ws = FakeWebSocket()
        await ws.recv_queue.put(json.dumps({'type': 'ui_client_hello'}))
        handler_task = asyncio.create_task(
            ui_websocket_handler(
                ws,
                '/ui_ws',
                'worker1',
                dummy_get_p2p_info,
                dummy_get_rendezvous_ws,
                8081,
                'stun.example',
                19302,
                dummy_benchmark_sender,
                dummy_delayed_exit,
                dummy_discover_stun,
                dummy_update_main,
                dummy_get_quic_engine,
            )
        )
        await asyncio.sleep(0.05)
        ws.closed = True
        handler_task.cancel()
        try:
            await handler_task
        except asyncio.CancelledError:
            pass
        self.assertTrue(ws.sent)
        first = json.loads(ws.sent[0])
        self.assertEqual(first['type'], 'init_info')

    async def test_broadcast(self):
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        from ui_server import ui_websocket_clients
        ui_websocket_clients.add(ws1)
        ui_websocket_clients.add(ws2)
        await broadcast_to_all_ui_clients({'foo': 'bar'})
        self.assertEqual(json.loads(ws1.sent[0]), {'foo': 'bar'})
        self.assertEqual(json.loads(ws2.sent[0]), {'foo': 'bar'})
        ws1.closed = True
        ws2.closed = True
        ui_websocket_clients.clear()

    async def test_quic_echo_command_format(self):
        class DummyEngine:
            def __init__(self):
                self.sent = []
                self.handshake_completed = True
                self.is_client = True

            async def send_app_data(self, data):
                self.sent.append(data)

        engine = DummyEngine()
        ws = FakeWebSocket()
        await ws.recv_queue.put(json.dumps({
            'type': 'quic_echo_command',
            'payload': 'hello'
        }))
        handler_task = asyncio.create_task(
            ui_websocket_handler(
                ws,
                '/ui_ws',
                'worker1',
                dummy_get_p2p_info,
                dummy_get_rendezvous_ws,
                8081,
                'stun.example',
                19302,
                dummy_benchmark_sender,
                dummy_delayed_exit,
                dummy_discover_stun,
                dummy_update_main,
                lambda: engine,
            )
        )
        await asyncio.sleep(0.05)
        ws.closed = True
        handler_task.cancel()
        try:
            await handler_task
        except asyncio.CancelledError:
            pass
        self.assertTrue(engine.sent)
        parts = engine.sent[0].decode().split(' ', 3)
        self.assertGreaterEqual(len(parts), 4)
        self.assertEqual(parts[0], 'QUIC_ECHO_REQUEST')
        self.assertEqual(parts[1], 'worker1')
        self.assertEqual(parts[-1], 'hello')
