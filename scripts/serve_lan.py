"""Serve one workbench over loopback HTTP and direct LAN HTTPS."""

import argparse
import asyncio
import ipaddress
import signal
import ssl
from contextlib import ExitStack, nullcontext

import uvicorn
from starlette.responses import JSONResponse


PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def lan_access(app, address, network, port):
    allowed_hosts = {f"{address}:{port}"}
    if port == 443:
        allowed_hosts.add(str(address))

    async def restricted(scope, receive, send):
        if scope["type"] == "http":
            source = scope.get("client")
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            try:
                peer = ipaddress.ip_address(source[0]) if source else None
            except ValueError:
                peer = None
            if not isinstance(peer, ipaddress.IPv4Address) or peer not in network or host not in allowed_hosts:
                response = JSONResponse({"detail": "仅允许从指定内网网段访问"}, status_code=403)
                await response(scope, receive, send)
                return
            if scope["path"].startswith("/api/ai-settings"):
                response = JSONResponse({"detail": "请在本机工作台管理 AI 接口"}, status_code=403)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return restricted


class SharedSignalServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


async def serve(app, configs):
    servers = [SharedSignalServer(config) for config in configs]

    def stop(_signal, _frame):
        for server in servers:
            server.should_exit = True

    previous_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with ExitStack() as stack:
            sockets = [stack.enter_context(config.bind_socket()) for config in configs]
            async with app.router.lifespan_context(app):
                await asyncio.gather(*(server.serve(sockets=[listener]) for server, listener in zip(servers, sockets)))
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="本机 HTTP 端口")
    parser.add_argument("--lan-address", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--lan-network", type=ipaddress.IPv4Network, required=True)
    parser.add_argument("--lan-port", type=int, default=8443)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if args.lan_address not in args.lan_network or not any(args.lan_network.subnet_of(block) for block in PRIVATE_NETWORKS):
        parser.error("内网监听地址必须位于指定的私有网段")

    from zhiyu.app import api

    common = {"lifespan": "off", "proxy_headers": False, "ws": "none"}
    local = uvicorn.Config(api, host="127.0.0.1", port=args.port, **common)
    lan = uvicorn.Config(
        lan_access(api, args.lan_address, args.lan_network, args.lan_port),
        host=str(args.lan_address),
        port=args.lan_port,
        ssl_certfile=args.cert,
        ssl_keyfile=args.key,
        **common,
    )
    lan.load()
    lan.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    asyncio.run(serve(api, (local, lan)))


if __name__ == "__main__":
    main()
