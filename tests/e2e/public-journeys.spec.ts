import { expect, test } from '@playwright/test';

const personName = 'Pessoa Alfa — personagem fictícia';
const profile = '/entidades/e2e-pessoa-alfa-ficticia/';

test('search, profile, graph and evidence explain the same fictional claim', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Nome ou instituição').fill('Pessoa Alfa');
  await page.getByRole('button', { name: 'Pesquisar', exact: true }).click();
  await page.getByRole('link', { name: personName, exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: personName })).toBeVisible();
  await expect(page.getByText('Emprego de demonstração fictício.', { exact: true })).toBeVisible();
  const graph = page.getByTestId('relationship-graph');
  await expect(graph).toHaveAttribute('data-state', 'ready');
  const graphResponse = await page.request.get(`${profile}grafo/`);
  const graphData = await graphResponse.json();
  expect(graphData.edges).toHaveLength(2);
  expect(graphData.truncated).toBe(false);
  await page.locator('.evidence-link').first().click();
  await expect(page).toHaveURL(/\/evidencias\/[0-9a-f-]+\/$/);
  await expect(page.getByText('Passagem inteiramente fictícia: não descreve pessoas reais.', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: /Documento de demonstração fictício/ })).toHaveAttribute(
    'href', 'https://example.org/e2e-documento-ficticio',
  );
  await expect(page.locator('body')).not.toContainText('PRIVATE_BROWSER_');
});

test('date filtering retains unknown bounds without inventing a dated claim', async ({ page }) => {
  await page.goto(profile);
  await page.getByLabel('Observar numa data').fill('2025-01-01');
  await page.getByRole('button', { name: 'Aplicar data', exact: true }).click();
  await expect(page).toHaveURL(/at=2025-01-01/);
  await expect(page.getByText('Emprego de demonstração fictício.', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Participação fictícia com datas desconhecidas.', { exact: true })).toBeVisible();
  await expect(page.getByTestId('relationship-graph')).toHaveAttribute('data-state', 'ready');
  const graph = await page.request.get(`${profile}grafo/?at=2025-01-01`);
  expect((await graph.json()).edges).toHaveLength(1);
});

test('keyboard navigation and mobile layout preserve the non-graph alternative', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('Tab');
  const skipLink = page.getByRole('link', { name: /Saltar para o conteúdo/ });
  await expect(skipLink).toBeFocused();
  await page.keyboard.press('Enter');
  const search = page.getByLabel('Nome ou instituição');
  await search.focus();
  await page.keyboard.type('Pessoa Alfa');
  await page.keyboard.press('Enter');
  const entityLink = page.getByRole('link', { name: personName, exact: true });
  await entityLink.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { level: 1, name: personName })).toBeVisible();
  const evidence = page.locator('.evidence-link').first();
  await evidence.focus();
  await expect(evidence).toBeFocused();
  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
  expect(horizontalOverflow).toBe(false);
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/evidencias\/[0-9a-f-]+\/$/);
});

test('hostile graph labels remain inert text and private profiles remain inaccessible', async ({ page }) => {
  const dialogs: string[] = [];
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss();
  });
  await page.goto(profile);
  await expect(page.getByTestId('relationship-graph')).toHaveAttribute('data-state', 'ready');
  await expect(page.getByText('<img src=x onerror="window.__xss=true"> — entidade fictícia', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => Reflect.get(window, '__xss'))).toBeUndefined();
  await expect(page.locator('img[onerror], script:not([src])')).toHaveCount(0);
  expect(dialogs).toEqual([]);
  const response = await page.goto('/entidades/e2e-pessoa-reservada-ficticia/');
  expect(response?.status()).toBe(404);
  await expect(page.locator('body')).not.toContainText('PRIVATE_BROWSER_DRAFT_CANARY');
});
