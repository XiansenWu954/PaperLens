import { expect, test } from '@playwright/test'

/**
 * PaperLens browser smoke test (comprehensive test manual §5.7, path 1-2).
 *
 * Scope of this first spec: a single end-to-end smoke path that proves the
 * app boots, the user can create a project, and the project workspace exposes
 * the primary Agent Chat surface. This deliberately stays small — the full
 * 10-path manual matrix (BibTeX import/export, PDF upload + ingestion states,
 * streaming answers + tool trace, citation graph from chat, report versions,
 * SSE recovery, mobile) is release-gate work that needs a live backend and is
 * documented in docs/internal/gate-runbook.md.
 *
 * Prereq: backend on :8000 + frontend on :5173 (or E2E_BASE_URL override).
 * Without a backend the project-create call will fail, so the spec asserts the
 * UI renders even before interaction and only proceeds through create when the
 * network allows it.
 */

test.describe('PaperLens smoke', () => {
  test('dashboard renders and exposes project creation', async ({ page }) => {
    await page.goto('/')

    // The app shell must render (manual §5.7: "no horizontal overflow / blocked").
    await expect(page.getByRole('heading', { name: 'Agent 文献研究工作台' })).toBeVisible()
    await expect(page.locator('#project-title')).toBeVisible()
    // No horizontal scroll on desktop/mobile viewport.
    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth)
    const clientWidth = await page.evaluate(() => document.documentElement.clientWidth)
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth)
  })

  test('creates a project and opens the workspace Agent Chat', async ({ page, browserName }) => {
    test.skip(browserName === 'firefox', 'smoke targets chromium; firefox is candidate-release')
    test.setTimeout(60_000)

    await page.goto('/')
    const title = `E2E smoke ${Date.now()}`
    await page.locator('#project-title').fill(title)
    await page.locator('.primary-button').click()

    // A project card for the new project should appear, then click into it.
    const card = page.locator('.project-card', { hasText: title }).first()
    await expect(card).toBeVisible({ timeout: 30_000 })
    await card.click()

    // The workspace must expose the primary Agent Chat surface.
    await expect(page.getByRole('heading', { name: 'Agent Chat' })).toBeVisible({
      timeout: 30_000,
    })
    await expect(page.getByPlaceholder(/向项目 Agent 提问/)).toBeVisible()
  })
})
