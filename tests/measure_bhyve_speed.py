import asyncio
import json
import time
import urllib.request
import uuid
import tornado.websocket

TOKEN = "b57a598d09c577a53bce6e40c614e1fc7ded873520841708"
BASE_URL = "http://127.0.0.1:8888"
WS_URL = "ws://127.0.0.1:8888"

async def run() -> None:
    headers = {
        "Authorization": f"token {TOKEN}",
        "Content-Type": "application/json",
    }
    
    t0 = time.monotonic()
    print("Requesting freebsd-python-bhyve kernel...")
    req = urllib.request.Request(
        f"{BASE_URL}/api/kernels",
        data=json.dumps({"name": "freebsd-python-bhyve"}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
    kernel_id = resp["id"]
    t_created = time.monotonic() - t0
    print(f"Kernel {kernel_id} created in {t_created:.2f}s")
    
    ws_endpoint = f"{WS_URL}/api/kernels/{kernel_id}/channels?token={TOKEN}"
    conn = await tornado.websocket.websocket_connect(ws_endpoint)
    
    msg_id = uuid.uuid4().hex
    msg = {
        "header": {
            "msg_id": msg_id,
            "username": "freebsd",
            "session": uuid.uuid4().hex,
            "msg_type": "execute_request",
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": "import sys, os\nprint(f\"PROBE_OK: {sys.platform} {os.uname().nodename} Python {sys.version}\")",
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
        },
        "channel": "shell",
    }
    await conn.write_message(json.dumps(msg))
    
    while True:
        raw_msg = await conn.read_message()
        if raw_msg is None:
            break
        data = json.loads(raw_msg)
        msg_type = data.get("msg_type")
        content = data.get("content", {})
        if msg_type == "stream":
            print("STREAM:", content.get("text", "").strip())
        elif msg_type == "execute_reply":
            t_total = time.monotonic() - t0
            print(f"EXECUTE STATUS: {content.get('status')} (Total time to first output: {t_total:.2f}s)")
            break
            
    conn.close()
    
    print(f"Deleting kernel {kernel_id}...")
    req_del = urllib.request.Request(
        f"{BASE_URL}/api/kernels/{kernel_id}",
        headers=headers,
        method="DELETE",
    )
    urllib.request.urlopen(req_del)
    print("Kernel deleted successfully.")

if __name__ == "__main__":
    asyncio.run(run())
