"""A stand-in Moonraker for the container tests: an idle printer on an external endpoint.

    python3 fake_moonraker.py [port]

Serves the read-only HTTP endpoints the listener and doctor query and the /websocket JSON-RPC
subscription, always reporting an idle (standby) printer. It never accepts a print. Runs with the
release image's own Python and websockets, so CI needs no extra image.
"""
import asyncio
import json
import sys
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

IDLE = {"print_stats": {"state": "standby", "filename": "", "message": "", "info": {}},
        "virtual_sdcard": {"progress": 0.0, "is_active": False},
        "gcode_move": {"gcode_position": [0, 0, 0, 0]}, "toolhead": {"position": [0, 0, 0, 0]}}
ROUTES = {
    "/printer/info": {"state": "ready", "state_message": "Printer is ready", "hostname": "fake-moonraker",
                      "software_version": "fake"},
    "/server/info": {"klippy_connected": True, "klippy_state": "ready", "moonraker_version": "fake"},
    "/printer/objects/query": {"eventtime": 0, "status": IDLE},
    "/server/history/list": {"count": 0, "jobs": []},
    "/server/history/totals": {"job_totals": {"total_jobs": 0, "total_time": 0, "total_print_time": 0,
                                              "total_filament_used": 0, "longest_job": 0, "longest_print": 0}},
    "/server/job_queue/status": {"queued_jobs": [], "queue_state": "ready"},
    "/server/files/directory": {"dirs": [], "files": [], "disk_usage": {}, "root_info": {}},
    "/server/spoolman/spool_id": {"spool_id": None},
}


def respond(path):
    if path not in ROUTES:
        return Response(404, "Not Found", Headers([("Content-Type", "application/json")]),
                        json.dumps({"error": {"code": 404, "message": f"{path} not faked"}}).encode())
    body = json.dumps({"result": ROUTES[path]}).encode()
    return Response(200, "OK", Headers([("Content-Type", "application/json"),
                                        ("Content-Length", str(len(body)))]), body)


def process_request(connection, request):
    path = urlsplit(request.path).path
    if path == "/websocket":
        return None
    print(f"http {path}", flush=True)
    return respond(path)


async def session(ws):
    print("websocket connected", flush=True)
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("method") == "printer.objects.subscribe":
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                      "result": {"eventtime": 0, "status": IDLE}}))
        elif "id" in msg:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}))


async def main(port):
    async with serve(session, "0.0.0.0", port, process_request=process_request) as server:
        print(f"fake moonraker on :{port}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7125))
