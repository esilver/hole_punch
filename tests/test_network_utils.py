import sys
import types

# Stub out external dependencies so network_utils can be imported without them
for mod_name in ("aiohttp", "stun", "websockets"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

if not hasattr(sys.modules["websockets"], "WebSocketServerProtocol"):
    sys.modules["websockets"].WebSocketServerProtocol = object

import unittest
import network_utils

class TestIsProbableQuicDatagram(unittest.TestCase):
    def test_long_header(self):
        self.assertTrue(network_utils.is_probable_quic_datagram(b"\x80\x00"))

    def test_short_header(self):
        # Fixed bit set, reserved bits clear, pn length non-zero. Use a second
        # byte that makes the payload invalid UTF-8 so the ASCII heuristic does
        # not short-circuit the check.
        self.assertTrue(network_utils.is_probable_quic_datagram(b"\x45\xff"))

    def test_prefix_exclusion(self):
        blob = b"P2P_HOLE_PUNCH_PING_FROM_1234"
        self.assertFalse(network_utils.is_probable_quic_datagram(blob))

    def test_ascii_text(self):
        self.assertFalse(network_utils.is_probable_quic_datagram(b"HELLO"))

    def test_non_quic_binary(self):
        self.assertFalse(network_utils.is_probable_quic_datagram(b"\x05\x01\x02"))

if __name__ == "__main__":
    unittest.main()
