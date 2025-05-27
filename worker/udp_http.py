"""
Minimal HTTP-over-UDP framing.
Each datagram = [flags 1B][msg-id 4B][raw HTTP/1.1 message]
"""

import http, struct
_FMT = "!B I"         # flags, msg-id
ACK  = 0x80
MAX  = 1200           # fits any path under common MTU

def _pack(f, mid, payload=b""): return struct.pack(_FMT, f, mid) + payload
def build_get(mid, path="/"):   return _pack(0, mid, f"GET {path} HTTP/1.1\r\n\r\n".encode())
def build_resp(mid, body):      return _pack(0, mid, f"HTTP/1.1 200 OK\r\nContent-Length:{len(body)}\r\n\r\n".encode()+body)
def build_ack(mid):             return _pack(ACK, mid)
def parse(d):                   return struct.unpack_from(_FMT, d)+ (d[5:],)