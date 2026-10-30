const T = new URL(location).searchParams.get('t');
fetch(T + '/v2/sw-fetch').catch(() => {});
