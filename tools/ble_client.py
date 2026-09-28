"""Desktop stand-in for the watch: exercises the garmin_ble integration over real BLE.

    uv run --no-project --with bleak tools/ble_client.py SECRET_HEX [--entity ENTITY_ID]

Runs: scan -> connect -> HELLO -> LIST -> toggle ENTITY on/off (if given) ->
GET -> BATTERY -> replay/wrong-key checks -> BYE. Prints one JSON line per step
and exits non-zero on any failed check.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time

from bleak import BleakClient, BleakScanner

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "gb_protocol", os.path.join(_here, "..", "custom_components", "garmin_ble", "protocol.py"))
p = importlib.util.module_from_spec(_spec)
sys.modules["gb_protocol"] = p
_spec.loader.exec_module(p)

SVC = "6a1e0001-4c7d-4b4e-9a2b-3c8f1d2e5a01"
CMD = "6a1e0002-4c7d-4b4e-9a2b-3c8f1d2e5a01"
EVT = "6a1e0003-4c7d-4b4e-9a2b-3c8f1d2e5a01"
FAIL = []


def out(**kw):
    print(json.dumps({"t": round(time.time(), 3), **kw}, ensure_ascii=False), flush=True)


def check(name, cond, **kw):
    out(check=name, ok=bool(cond), **kw)
    if not cond:
        FAIL.append(name)


class Link:
    def __init__(self, client: BleakClient):
        self.c = client
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.re = p.Reassembler()
        self.ctr = 0
        self.nonce = b""

    def _on_notify(self, _h, data: bytearray):
        m = self.re.feed(bytes(data))
        if m is not None:
            self.q.put_nowait(m)

    async def start(self):
        await self.c.start_notify(EVT, self._on_notify)

    async def recv(self, timeout=5.0) -> bytes:
        return await asyncio.wait_for(self.q.get(), timeout)

    async def recv_until(self, pred, timeout=5.0) -> list[bytes]:
        got, deadline = [], time.monotonic() + timeout
        while True:
            m = await asyncio.wait_for(self.q.get(), max(0.01, deadline - time.monotonic()))
            got.append(m)
            if pred(m):
                return got

    async def write(self, frame: bytes):
        assert len(frame) <= 20, len(frame)
        await self.c.write_gatt_char(CMD, frame, response=True)

    async def cmd(self, key, op, payload=b""):
        self.ctr += 1
        t0 = time.monotonic()
        await self.write(p.build_command(key, self.nonce, self.ctr, op, payload))
        return t0


async def find(timeout):
    ev, hit = asyncio.Event(), {}

    def cb(d, a):
        if SVC in a.service_uuids:
            hit["d"], hit["rssi"] = d, a.rssi
            ev.set()

    async with BleakScanner(cb):
        await asyncio.wait_for(ev.wait(), timeout)
    return hit["d"], hit["rssi"]


async def main(a):
    key = bytes.fromhex(a.secret)
    t0 = time.monotonic()
    dev, rssi = await find(a.scan)
    out(step="found", addr=dev.address, rssi=rssi, ms=round((time.monotonic() - t0) * 1000))
    t1 = time.monotonic()
    async with BleakClient(dev, timeout=20) as c:
        out(step="connected", ms=round((time.monotonic() - t1) * 1000), mtu=c.mtu_size)
        L = Link(c)
        await L.start()

        # unauthenticated command before HELLO -> NO_SESSION
        await L.write(p.build_command(key, bytes(8), 1, p.OP_LIST))
        check("no_session_rejected", (await L.recv()) == p.encode_result(1, p.ST_NO_SESSION))

        th = time.monotonic()
        await L.write(p.build_hello())
        ch = await L.recv()
        check("challenge", ch[0] == p.MSG_CHALLENGE and ch[9] == p.PROTOCOL_VERSION,
              ms=round((time.monotonic() - th) * 1000), count=ch[10])
        L.nonce = ch[1:9]

        tl = await L.cmd(key, p.OP_LIST)
        msgs = await L.recv_until(lambda m: m[0] == p.MSG_LIST_END)
        ents = [p.decode_entity(m) for m in msgs if m[0] == p.MSG_ENTITY]
        check("list", msgs[-1] == p.encode_list_end(len(ents)) and len(ents) == ch[10],
              ms=round((time.monotonic() - tl) * 1000), entities=[(e["idx"], e["entity_id"], e.get("state")) for e in ents])

        if a.entity:
            idx = next((e["idx"] for e in ents if e["entity_id"] == a.entity), None)
            check("entity_exposed", idx is not None, entity=a.entity)
            if idx is not None:
                for action, want in ((p.ACT_TURN_ON, "on"), (p.ACT_TURN_OFF, "off")):
                    ta = await L.cmd(key, p.OP_ACTION, bytes([idx, action]))
                    ctr = L.ctr
                    msgs = await L.recv_until(
                        lambda m: m[0] == p.MSG_ENTITY and p.decode_entity(m).get("state") == want, timeout=8)
                    check(f"action_{want}", p.encode_result(ctr, p.ST_OK) in msgs,
                          ms=round((time.monotonic() - ta) * 1000))
                # wrong action for the domain
                await L.cmd(key, p.OP_ACTION, bytes([idx, p.ACT_UNLOCK]))
                check("not_allowed", (await L.recv()) == p.encode_result(L.ctr, p.ST_NOT_ALLOWED))

                tg = await L.cmd(key, p.OP_GET, bytes([idx]))
                e = p.decode_entity(await L.recv())
                check("get", e["entity_id"] == a.entity, ms=round((time.monotonic() - tg) * 1000), state=e.get("state"))

        await L.cmd(key, p.OP_BATTERY, bytes([77, 0]))
        check("battery", (await L.recv()) == p.encode_result(L.ctr, p.ST_OK))

        # replay of the first authenticated frame
        await L.write(p.build_command(key, L.nonce, 1, p.OP_LIST))
        check("replay_rejected", (await L.recv()) == p.encode_result(1, p.ST_BAD_AUTH))
        # wrong key
        await L.write(p.build_command(bytes(16), L.nonce, L.ctr + 1, p.OP_LIST))
        check("wrong_key_rejected", (await L.recv()) == p.encode_result(L.ctr + 1, p.ST_BAD_AUTH))

        await L.cmd(key, p.OP_BYE)
    out(step="done", total_ms=round((time.monotonic() - t0) * 1000), failed=FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("secret")
    ap.add_argument("--entity")
    ap.add_argument("--scan", type=float, default=20)
    sys.exit(asyncio.run(main(ap.parse_args())))
