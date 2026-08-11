import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright browser gate config for PaperLens (comprehensive test manual §5.7).
 *
 * Projects:
 *   - desktop-chromium  @ 1440x900  (default; candidate-release adds Firefox)
 *   - mobile-chromium   @ 390x844
 *
 * The browser layer needs a running backend (Django on :8000) and frontend
 * (Vite dev on :5173) to exercise real user paths. Those services are NOT
 * started automatically here — start them (or the Docker stack) first, then
 * run:  npx playwright test
 *
 * baseURL and the frontend port are overridable via env so CI/local setups
 * can point at a different host without editing this file.
 */
const FRONTEND_BASE = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'e2e-report' }]],
  // Failures keep a screenshot + full trace for triage; manual §5.7.
  use: {
    baseURL: FRONTEND_BASE,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'mobile-chromium',
      use: {
        ...devices['Pixel 5'],
        viewport: { width: 390, height: 844 },
      },
    },
  ],
})
