import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { loadEnv, type Plugin } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.API_PROXY_TARGET;
  const reviewEnabled = env.VITE_ENABLE_REVIEW === 'true';
  const protectReview: Plugin = {
    name: 'protect-local-review-proxy',
    configureServer(server) { server.middlewares.use('/review-api', (req, res, next) => {
      if (!reviewEnabled || !env.REVIEW_TOKEN || !isLoopback(req.socket.remoteAddress)) { res.statusCode = 403; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ error: { code: 'FORBIDDEN', message: 'Review недоступен.', retryable: false, request_id: 'local-gateway' } })); return; }
      next();
    }); },
    configurePreviewServer(server) { server.middlewares.use('/review-api', (req, res, next) => {
      if (!reviewEnabled || !env.REVIEW_TOKEN || !isLoopback(req.socket.remoteAddress)) { res.statusCode = 403; res.end(); return; }
      next();
    }); },
  };
  return {
    plugins: [react(), protectReview],
    server: { proxy: target ? {
      '/api': { target, changeOrigin: true },
      '/review-api/v1': { target, changeOrigin: true, rewrite: path => path.replace(/^\/review-api\/v1/, '/api/v1/review'), headers: { Authorization: `Bearer ${env.REVIEW_TOKEN || ''}` } },
    } : undefined },
    test: { environment: 'jsdom', globals: true, setupFiles: './tests/setup.ts', restoreMocks: true, include: ['tests/**/*.test.{ts,tsx}'] },
  };
});
function isLoopback(address?: string) { return ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address ?? ''); }
