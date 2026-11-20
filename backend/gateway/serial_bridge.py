"""Reads the hub's USB serial port and forwards lines to the Adrishta server."""
import argparse
import asyncio
import os
import threading

import serial            # pip install pyserial
import websockets        # pip install "websockets>=14"

PFX = bytes([67, 83, 73]).decode() + "_DATA,"
KEEP = (PFX, "ACK,", "DBG,")
ALLOWED_CMDS = {"STATUS", "MAC_FILTER:1", "MAC_FILTER:0", "RESET_STATS"}


def serial_reader(ser, put, stop):
    buf = b""
    while not stop.is_set():
        data = ser.read(4096)
        if not data:
            continue
        buf += data
        if len(buf) > 65536 and b"\n" not in buf:
            buf = b""
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            line = raw.decode(errors="ignore").strip()
            if line.startswith(KEEP):
                put(line)


async def run(args):
    token = os.environ.get("ADRISHTA_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("Set ADRISHTA_INGEST_TOKEN first")
    ser = serial.Serial(args.port, args.baud, timeout=0.05)
    loop = asyncio.get_running_loop()
    q = asyncio.Queue(maxsize=5000)

    def put(line):
        def _p():
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass
        loop.call_soon_threadsafe(_p)

    stop = threading.Event()
    threading.Thread(target=serial_reader, args=(ser, put, stop), daemon=True).start()
    backoff = 1
    while True:
        try:
            async with websockets.connect(args.url, additional_headers={"Authorization": f"Bearer {token}"},
                                          max_size=65536, ping_interval=20) as ws:
                print("connected to server")
                backoff = 1
                while not q.empty():
                    q.get_nowait()                       # drop stale data

                async def sender():
                    while True:
                        lines = [await q.get()]
                        await asyncio.sleep(0.02)
                        while not q.empty() and len(lines) < 200:
                            lines.append(q.get_nowait())
                        await ws.send("\n".join(lines))

                async def receiver():
                    async for msg in ws:
                        cmd = str(msg).strip()
                        if cmd in ALLOWED_CMDS:
                            ser.write((cmd + "\n").encode())

                done, pending = await asyncio.wait([asyncio.create_task(sender()), asyncio.create_task(receiver())],
                                                   return_when=asyncio.FIRST_EXCEPTION)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.result()
        except Exception as e:
            print(f"connection lost ({e}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="e.g. COM7 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--url", required=True, help="e.g. wss://adrishta.example.com/ws/ingest")
    asyncio.run(run(ap.parse_args()))