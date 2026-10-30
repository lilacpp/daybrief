import { test, expect } from '@playwright/test';
test('generates three source-backed mock stories and narration', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('MOCK · FICTIONAL DEMO STORIES')).toBeVisible();
  await page.getByRole('combobox').selectOption('ai_technology');
  await page.getByRole('button', { name: 'Generate brief →' }).click();
  await expect(page.locator('article')).toHaveCount(3);
  await expect(page.getByRole('heading', { name: 'Your narration' })).toBeVisible();
  await expect(page.locator('section').filter({ has: page.getByRole('heading', { name: 'Your narration', exact: true }) })).not.toContainText('unsupported prediction');
  await page.locator('summary').first().click();
  await expect(page.locator('article').first().getByRole('link')).toHaveCount(6);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});

for (const count of [1, 2]) {
  test(`shows a shorter ${count}-story edition transparently`, async ({ page }) => {
    await page.route('**/api/briefs/generate', route => route.fulfill({
      json: {
        id: 'short-edition', mode: 'mock', sources: [],
        coverage_note: `Only ${count} safely corroborated stories were available; this edition is shorter to preserve evidence quality.`,
        selected_events: Array.from({ length: count }, (_, i) => ({
          id: `event-${i}`, headline: `Verified occurrence ${i}`, category: 'science',
          what_happened: 'A verified occurrence.', why_it_matters: '', uncertainties: [],
          claims: [], source_urls: [], latest_published_at: '2026-10-14T12:00:00Z',
          selection_reason: 'Selected for corroboration.', sensitivity: 'normal',
          corroboration_level: 'independently_corroborated',
        })),
        narration: 'This is DayBrief. Verified developments. Sources below.',
        estimated_duration_seconds: count * 20,
      },
    }));
    await page.goto('/');
    await page.getByRole('button', { name: 'Generate brief →' }).click();
    await expect(page.locator('article')).toHaveCount(count);
    await expect(page.getByRole('heading', { name: `Your ${count} ${count === 1 ? 'story' : 'stories'}`, exact: true })).toBeVisible();
    await expect(page.getByRole('status')).toContainText(`Only ${count} safely corroborated stories`);
    await expect(page.getByText('Independently corroborated', { exact: true })).toHaveCount(count);
    await expect(page.getByRole('heading', { name: 'Your narration' })).toBeVisible();
  });
}
