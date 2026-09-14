export function appearanceBridge(server, backend, readJsonBody) {
  server.middlewares.use('/__serein/appearance/images', async (request, response) => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    const path = (request.url || '/').split('?')[0];
    const valid = request.method === 'GET' ? path === '/' : request.method === 'PUT' && /^\/(user|assistant|hero)$/.test(path);
    if (!valid) { response.statusCode = 405; response.end('{}'); return; }
    if (request.method === 'PUT') {
      let sameOrigin = false;
      try { sameOrigin = !request.headers.origin || new URL(request.headers.origin).host === request.headers.host; } catch {}
      if (!sameOrigin || !String(request.headers['content-type']).startsWith('application/json')) {
        response.statusCode = 403; response.end('{}'); return;
      }
    }
    try {
      const result = await backend('/v1/appearance/images' + (path === '/' ? '' : path), {
        method: request.method,
        ...(request.method === 'PUT' ? {body: await readJsonBody(request, 3 * 1024 * 1024)} : {}),
      });
      response.statusCode = result.status; response.end(JSON.stringify(result.payload));
    } catch (error) {
      response.statusCode = error.message === 'request_too_large' ? 413 : 502;
      response.end(JSON.stringify({detail: '图片未保存，请检查文件大小与后端连接后重试。'}));
    }
  });
}
