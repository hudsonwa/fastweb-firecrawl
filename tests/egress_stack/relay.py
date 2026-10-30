"""TCP relay: LISTEN_PORT -> HOST:PORT (publishes CDP and the proxy's /stats to the host from the internal network)."""

import asyncio
import sys

LP, TH, TP = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])


async def pump(r, w):
    try:
        while d := await r.read(65536):
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()


async def h(r, w):
    r2, w2 = await asyncio.open_connection(TH, TP)
    await asyncio.gather(pump(r, w2), pump(w))


async def main():
    s = await asyncio.start_server(h, "0.0.0.0", LP)
    async with s:
        await s.serve_forever()


asyncio.run(main())
