"""Isolated loopback WebRTC test server with measured RTP loss and jitter."""
import argparse
import asyncio
import os
import random
from contextlib import suppress

import uvicorn
from aiortc import clock
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtp import is_rtcp


def install(api):
    send_original = RTCDtlsTransport._send_rtp
    receive_original = RTCDtlsTransport._handle_rtp_data
    counters = {name: {"total": 0, "drop": 0, "scheduled": 0} for name in ("uplink", "downlink")}
    jitter = {"uplink": random.Random(19), "downlink": random.Random(29)}
    delay = 80
    stddev = 12
    loss = .02

    async def after(seconds, operation, *args):
        await asyncio.sleep(seconds)
        with suppress(ConnectionError):
            await operation(*args)

    async def receive_at(transport, data):
        await receive_original(transport, data, clock.current_ms())

    def enqueue(direction, operation, *args):
        stats = counters[direction]
        rng = jitter[direction]
        stats["total"] += 1
        if rng.random() < loss:
            stats["drop"] += 1
            return
        stats["scheduled"] += 1
        asyncio.create_task(after(max(0, rng.gauss(delay, stddev)) / 1000, operation, *args))

    async def sending(transport, data):
        if is_rtcp(data):
            await send_original(transport, data)
        else:
            enqueue("downlink", send_original, transport, data)

    async def receiving(transport, data, arrival_time_ms):
        enqueue("uplink", receive_at, transport, data)

    RTCDtlsTransport._send_rtp = sending
    RTCDtlsTransport._handle_rtp_data = receiving

    @api.get("/_test/media-shaping")
    async def statistics():
        return {"delay_ms": delay, "stdev_ms": stddev, "target_loss": loss, "packets": counters}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=8766, type=int)
    args = parser.parse_args()
    os.environ["ZHIYU_RUNTIME"] = "runtime/shaped-server"
    os.environ.pop("ZHIYU_LLM_URL", None)
    from zhiyu.app import api

    install(api)
    uvicorn.run(api, host="127.0.0.1", port=args.port)
