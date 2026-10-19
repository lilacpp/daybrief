import { test, expect } from '@playwright/test';
test('generates three source-backed mock stories and narration', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('MOCK · FICTIONAL DEMO STORIES')).toBeVisible();
  await page.getByRole('combobox').selectOption('ai_technology');
  await page.getByRole('button', { name: 'Generate brief →' }).click();
  await expect(page.locator('article')).toHaveCount(3);
  await expect(page.getByRole('heading', { name: 'Your narration' })).toBeVisible();
  await expect(page.locator('.script')).not.toContainText('unsupported prediction');
  await page.locator('summary').first().click();
  await expect(page.locator('article').first().getByRole('link')).toHaveCount(4);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});
