const T = new URL(location).searchParams.get('t');
fetch(T + '/v/worker').catch(() => {});
try { const s = new WebSocket(T.replace('http', 'ws') + '/v/ws'); s.onerror = () => {}; } catch (e) {}
