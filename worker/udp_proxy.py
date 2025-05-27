import asyncio, itertools, time, socket, os, logging
from udp_http import build_get, build_resp, build_ack, parse, MAX

log = logging.getLogger("proxy")

class UDPHTTPProxy:
    def __init__(self, loop, udp_sock, peer_addr, local_tcp_port=8080):
        self.loop, self.sock, self.peer = loop, udp_sock, peer_addr
        self.mid  = itertools.count().__next__
        self.todo = {}          # mid -> Future
        self.local_port = local_tcp_port

    # ── UDP side ──────────────────────────────────────────────
    async def _udp_read(self):
        while True:
            pkt, _ = await self.loop.sock_recvfrom(self.sock, MAX)
            flags, mid, payload = parse(pkt)
            if flags & 0x80:               # ACK only
                fut = self.todo.pop(mid, None)
                if fut and not fut.done(): fut.set_result(b"")
                continue
            self.sock.sendto(build_ack(mid), self.peer)
            fut = self.todo.get(mid)
            if fut: fut.set_result(payload)

    # ── TCP side ──────────────────────────────────────────────
    async def _handle_tcp(self, r, w):
        req = await r.read(-1)
        mid = self.mid()
        fut = self.loop.create_future()
        self.todo[mid] = fut
        self.sock.sendto(build_get(mid, "/echo") + req.split(b"\r\n\r\n",1)[1], self.peer)
        resp = await fut             # HTTP/1.1 payload from peer
        w.write(resp)
        await w.drain(); w.close()

    async def run(self):
        self.loop.create_task(self._udp_read())
        srv = await asyncio.start_server(self._handle_tcp, "127.0.0.1", self.local_port)
        await srv.serve_forever()