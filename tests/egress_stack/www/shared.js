const T = new URL(location).searchParams.get('t');
fetch(T + '/v/shared').catch(() => {});
