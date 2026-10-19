import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  use: { baseURL: 'http://localhost:3000' },
  webServer: [
    {
      command:
        'cd ../backend && DAYBRIEF_MODE=mock DATABASE_URL=sqlite:////tmp/daybrief-smoke.db ../.venv/bin/uvicorn daybrief.api:app --port 8000',
      url: 'http://localhost:8000/health',
      reuseExistingServer: false,
    },
    { command: 'npm start', url: 'http://localhost:3000', reuseExistingServer: false },
  ],
});
