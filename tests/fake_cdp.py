"""A scripted Chrome DevTools Protocol server for browser-tier tests: /json/version plus the handful of Target,
Page, Network, Fetch and Runtime methods fastweb's browser client uses. Each page URL maps to a scenario; the fake
records every paused request and what the client decided."""
import asyncio
import contextlib
import itertools
import json
import re
import threading
import time
from http import HTTPStatus

from websockets.asyncio.server import serve

TAG = re.compile(r"<[^>]+>")


class FakeCDP:
    def __init__(self):
        self.pages = {}         # url -> scenario dict (see scenario())
        self.decisions = []     # (url, resourceType, "continue" | "fail:<reason>")
        self.navigations = []
        self.contexts = set()   # live browser contexts
        self.created = 0
        self.targets = 0
        self.max_targets = 0
        self.browser_closes = 0
        self.connections = 0
        self.ua = []
        self.bypass_sw = []
        self.calls = []         # (method, params, sessionId) of every command
        self.unseen = []        # child-target requests sent while the client had no auto-attach (nobody saw them)
        self.ids = itertools.count(1)
        self.hang = set()       # methods that never answer ("Target.disposeBrowserContext", ...)
        self.hang_once = set()  # methods that never answer the next time only
        self.eval_hang = False  # Runtime.evaluate of innerText never answers
        self.loop = None
        self.port = None

    @staticmethod
    def scenario(html="", status=200, redirects=(), subs=(), timeline=None, xhr=(), dcl_ms=0, crash=False,
                 nav_delay=0.0, final_url=None, children=(), nav_error=None, headers=None):
        """timeline: [(ms after DCL, html)], the page as script changes it; xhr: [(start_ms, end_ms)] after DCL.
        final_url: the document URL Chrome reports (not paused); children: [(url, resourceType)] requested by a worker
        target, seen only through auto-attach (else recorded in `unseen`). nav_error: Page.navigate's errorText (e.g.
        Chrome's net::ERR_PROXY_CONNECTION_FAILED); headers: the document response's headers."""
        return {"status": status, "redirects": list(redirects), "subs": list(subs),
                "timeline": timeline or [(0, html)], "xhr": list(xhr), "dcl_ms": dcl_ms, "crash": crash,
                "nav_delay": nav_delay, "final_url": final_url, "children": list(children),
                "nav_error": nav_error, "headers": headers or {}}

    # ---------------------------------------------------------------- server
    def start(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()

            async def main():
                async with serve(self.handler, "127.0.0.1", 0, process_request=self.http,
                                 max_size=64 * 1024 * 1024) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    self.stop_ev = asyncio.Event()
                    ready.set()
                    await self.stop_ev.wait()
            self.loop.run_until_complete(main())
        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.stop_ev.set)

    def http(self, connection, request):
        if request.path == "/json/version":
            return connection.respond(HTTPStatus.OK, json.dumps({
                "Browser": "HeadlessChrome/157.0.8103.27",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/fake"}))
        return None

    async def handler(self, ws):
        self.connections += 1
        conn = {"ws": ws, "pending": {}, "sessions": {}, "tasks": set()}
        try:
            async for raw in ws:
                m = json.loads(raw)
                await self.dispatch(conn, m)
        except Exception:
            pass
        finally:  # background work of this connection ends with it (no "Task was destroyed" at loop close)
            tasks = list(conn["tasks"])
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def spawn(conn, coro):
        """Run coro in the background, owned by conn."""
        task = asyncio.ensure_future(coro)
        conn["tasks"].add(task)
        task.add_done_callback(conn["tasks"].discard)

    async def send(self, conn, msg):
        with contextlib.suppress(Exception):
            await conn["ws"].send(json.dumps(msg))

    async def reply(self, conn, m, result):
        await self.send(conn, {"id": m["id"], "result": result, **({"sessionId": m["sessionId"]}
                                                                    if "sessionId" in m else {})})

    async def event(self, conn, sid, method, params):
        await self.send(conn, {"method": method, "params": params, "sessionId": sid})

    async def dispatch(self, conn, m):
        meth, p, sid = m.get("method"), m.get("params") or {}, m.get("sessionId")
        self.calls.append((meth, p, sid))
        if meth in self.hang or meth in self.hang_once:
            self.hang_once.discard(meth)
            return
        if meth == "Target.createBrowserContext":
            ctx = f"ctx{next(self.ids)}"
            self.contexts.add(ctx)
            self.created += 1
            return await self.reply(conn, m, {"browserContextId": ctx})
        if meth == "Target.disposeBrowserContext":
            self.contexts.discard(p.get("browserContextId"))
            return await self.reply(conn, m, {})
        if meth == "Target.createTarget":
            tid = f"T{next(self.ids)}"
            self.targets += 1
            self.max_targets = max(self.max_targets, self.targets)
            return await self.reply(conn, m, {"targetId": tid})
        if meth == "Target.attachToTarget":
            s = f"S-{p['targetId']}"
            conn["sessions"][s] = {"tid": p["targetId"], "html": "", "url": "about:blank", "t0": None}
            return await self.reply(conn, m, {"sessionId": s})
        if meth == "Target.closeTarget":
            self.targets -= 1
            return await self.reply(conn, m, {"success": True})
        if meth == "Browser.close":
            self.browser_closes += 1
            await self.reply(conn, m, {})
            return await conn["ws"].close()
        if meth in ("Fetch.continueRequest", "Fetch.failRequest"):
            f = conn["pending"].pop(p.get("requestId"), None)
            if f and not f.done():
                f.set_result("continue" if meth == "Fetch.continueRequest" else f"fail:{p.get('errorReason')}")
            return await self.reply(conn, m, {})
        if meth == "Network.setUserAgentOverride":
            self.ua.append(p.get("userAgent"))
        if meth == "Network.setBypassServiceWorker":
            self.bypass_sw.append(p.get("bypass"))
        page = conn["sessions"].get(sid)
        if meth == "Page.navigate" and page is not None:
            self.spawn(conn, self.navigate(conn, m, page, p["url"]))
            return
        if meth == "Runtime.evaluate" and page is not None:
            html = self.current(page)
            expr = p.get("expression", "")
            if "outerHTML" in expr:
                cap = re.search(r"> (\d+)", expr)
                val = {"big": len(html)} if cap and len(html) > int(cap.group(1)) else html
            elif "innerText" in expr:
                if self.eval_hang:
                    return
                val = len(TAG.sub("", html))
            elif "location.href" in expr:
                val = page["url"]
            else:
                val = None
            return await self.reply(conn, m, {"result": {"type": "string", "value": val}})
        return await self.reply(conn, m, {})

    def current(self, page):
        if page["t0"] is None:
            return page["html"]
        el = (time.monotonic() - page["t0"]) * 1000
        html = ""
        for ms, h in page["sc"]["timeline"]:
            if ms <= el:
                html = h
        return html

    async def pause(self, conn, sid, frame, url, rtype):
        rid = f"r{next(self.ids)}"
        f = asyncio.get_running_loop().create_future()
        conn["pending"][rid] = f
        await self.event(conn, sid, "Fetch.requestPaused", {"requestId": rid, "request": {"url": url, "method": "GET"},
                                                            "frameId": frame, "resourceType": rtype})
        try:
            d = await asyncio.wait_for(f, 3)
        except TimeoutError:
            d = "timeout"
        self.decisions.append((url, rtype, d))
        return d

    async def navigate(self, conn, m, page, url):
        self.navigations.append(url)
        sc = self.pages.get(url) or self.scenario("<html><body><p>default</p></body></html>")
        page["sc"] = sc
        sid, tid = m["sessionId"], page["tid"]
        if sc["nav_delay"]:
            await asyncio.sleep(sc["nav_delay"])
        for u in [url] + sc["redirects"]:
            d = await self.pause(conn, sid, tid, u, "Document")
            if d != "continue":
                return await self.reply(conn, m, {"frameId": tid, "errorText": "net::ERR_BLOCKED_BY_CLIENT"})
            page["url"] = u
        if sc["crash"]:
            return await conn["ws"].close()
        if sc["nav_error"]:
            return await self.reply(conn, m, {"frameId": tid, "errorText": sc["nav_error"]})
        await self.event(conn, sid, "Network.responseReceived", {
            "requestId": "doc", "frameId": tid, "type": "Document",
            "response": {"url": sc["final_url"] or page["url"], "status": sc["status"], "headers": sc["headers"]}})
        if sc["final_url"]:
            page["url"] = sc["final_url"]
        await self.reply(conn, m, {"frameId": tid, "loaderId": "L1"})
        await asyncio.sleep(sc["dcl_ms"] / 1000)
        page["t0"] = time.monotonic()
        await self.event(conn, sid, "Page.domContentEventFired", {"timestamp": 0})
        for u, rtype in sc["subs"]:
            self.spawn(conn, self.pause(conn, sid, tid, u, rtype))
        for i, (a, b) in enumerate(sc["xhr"]):
            self.spawn(conn, self.xhr(conn, sid, f"x{i}", a, b))
        if sc["children"]:
            self.spawn(conn, self.worker(conn, sid, sc["children"]))

    async def worker(self, conn, sid, children):
        """A dedicated worker the page starts: paused only when the client auto-attaches with waitForDebuggerOnStart;
        its requests are paused only when the child session itself sent Fetch.enable (as in Chrome)."""
        if not any(c[0] == "Target.setAutoAttach" and c[2] == sid and c[1].get("autoAttach")
                   and c[1].get("waitForDebuggerOnStart") is True for c in self.calls):
            self.unseen.extend(children)
            return
        child = f"W-{sid}"
        conn["sessions"][child] = {"tid": "W", "html": "", "url": "", "t0": None, "worker": True}
        await self.event(conn, sid, "Target.attachedToTarget", {
            "sessionId": child, "targetInfo": {"targetId": "W", "type": "worker", "url": "http://127.0.0.1/w.js"},
            "waitingForDebugger": True})
        for _ in range(100):  # the worker runs only after Runtime.runIfWaitingForDebugger
            if any(c[0] == "Runtime.runIfWaitingForDebugger" and c[2] == child for c in self.calls):
                break
            await asyncio.sleep(0.01)
        else:
            return
        if not any(c[0] == "Fetch.enable" and c[2] == child for c in self.calls):
            self.unseen.extend(children)
            return
        for u, rtype in children:
            self.spawn(conn, self.pause(conn, child, None, u, rtype))

    async def xhr(self, conn, sid, rid, a, b):
        await asyncio.sleep(a / 1000)
        await self.event(conn, sid, "Network.requestWillBeSent", {"requestId": rid, "type": "XHR",
                                                                 "request": {"url": "http://127.0.0.1/api"}})
        await asyncio.sleep((b - a) / 1000)
        await self.event(conn, sid, "Network.loadingFinished", {"requestId": rid})
