const T = new URL(location).searchParams.get('t');
fetch(T + '/v2/worker-fetch').catch(() => {});
try { importScripts(T + '/v2/worker-import'); } catch (e) {}
try { new Worker('data:,0'); } catch (e) {}
try { const s = new WebSocket(T.replace('http', 'ws') + '/v2/worker-ws'); s.onerror = () => {}; } catch (e) {}
