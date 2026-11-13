import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: '**/api.spec.ts',
  outputDir: 'test-results-api',
  use: { ...base.use, baseURL: 'http://127.0.0.1:5173' },
  webServer: { command: 'npm run preview -- --outDir dist-api --host 127.0.0.1 --port 5173', url: 'http://127.0.0.1:5173', reuseExistingServer: false },
});
