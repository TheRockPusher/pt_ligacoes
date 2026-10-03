import { expect, test } from '@playwright/test';

const personName = 'Pessoa Alfa — personagem fictícia';
const profile = '/entidades/e2e-pessoa-alfa-ficticia/';
const clientName = 'Casino Delta — entidade fictícia';

test('search, profile, graph and evidence explain the same fictional claim', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Nome ou instituição').fill('Pessoa Alfa');
  await page.getByRole('button', { name: 'Pesquisar', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Perfis públicos' }).getByRole('link')).toHaveCount(1);
  await page.getByRole('link', { name: personName, exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: personName })).toBeVisible();
  await expect(page.getByText('Emprego de demonstração fictício.', { exact: true })).toBeVisible();
  const graph = page.getByTestId('relationship-graph');
  await expect(graph).toHaveAttribute('data-state', 'ready');
  const graphResponse = await page.request.get(`${profile}grafo/`);
  const graphData = await graphResponse.json();
  expect(graphData.edges).toHaveLength(4);
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
  await page.getByRole('button', { name: 'Aplicar', exact: true }).click();
  await expect(page).toHaveURL(/at=2025-01-01/);
  await expect(page.getByText('Emprego de demonstração fictício.', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Participação fictícia com datas desconhecidas.', { exact: true })).toBeVisible();
  await expect(page.getByTestId('relationship-graph')).toHaveAttribute('data-state', 'ready');
  const graph = await page.request.get(`${profile}grafo/?at=2025-01-01`);
  expect((await graph.json()).edges).toHaveLength(3);
});

test('header search finds a profile by an official alias, ignoring accents and case', async ({ page }) => {
  await page.goto('/fontes/');
  const search = page.getByRole('searchbox', { name: 'Pesquisar pessoas e entidades' });
  await search.focus();
  await page.keyboard.type('ALFA conceicao');
  await page.keyboard.press('Enter');
  const results = page.getByRole('list', { name: 'Perfis públicos' });
  await expect(results.getByRole('link')).toHaveCount(1);
  await expect(results.getByRole('link', { name: personName, exact: true })).toBeVisible();
  await expect(results.getByText('Alfa Conceição Fictícia', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)).toBe(false);
});

test('a declared client is labelled as declared, dated and backed by evidence', async ({ page }) => {
  await page.goto(profile);
  await expect(page.getByText('Cargos públicos em curso')).toBeVisible();
  await expect(page.locator('.current-offices')).toContainText('Deputada fictícia');
  await expect(page.locator('.current-offices')).toContainText('Em curso');
  await expect(page.getByRole('heading', { name: 'Declarado pela própria pessoa' })).toBeVisible();
  const declared = page.locator('#tipo-declared_client-declarado');
  await expect(declared).toContainText(`${personName} declarou ${clientName} como cliente`);
  await expect(declared).toContainText('Declaração de interesses de 20/05/2024');
  const graph = await page.request.get(`${profile}grafo/?declarado=1`);
  const edges = (await graph.json()).edges;
  expect(edges.map((edge: { data: { kind: string; declared: boolean } }) => [edge.data.kind, edge.data.declared])).toEqual([
    ['declared_client', true],
  ]);
  await declared.locator('.evidence-link').first().click();
  await expect(page).toHaveURL(/\/evidencias\/[0-9a-f-]+\/$/);
  await expect(page.getByText(/Consta da declaração de interesses de Pessoa Alfa/)).toBeVisible();
  await expect(page.getByRole('link', { name: /Consultar fonte original/ })).toHaveAttribute(
    'href', 'https://example.org/e2e-declaracao-ficticia',
  );
});

test('name-only identities carry an honest provenance badge', async ({ page }) => {
  await page.goto('/entidades/e2e-casino-delta-ficticio/');
  await expect(page.getByText('Sem identificador oficial — nome como declarado na fonte', { exact: true })).toBeVisible();
  await expect(page.getByText('Identificadores públicos')).toHaveCount(0);
  await page.goto('/entidades/e2e-pessoa-omega-ficticia/');
  await expect(page.getByText('Identificada apenas pelo nome publicado na fonte', { exact: true })).toBeVisible();
  await page.goto(profile);
  await expect(page.locator('.provenance-badge')).toHaveCount(0);
});

test('path finder links a person to a company through an evidenced declared client', async ({ page }) => {
  await page.goto('/caminhos/');
  await page.getByLabel('Pessoa ou entidade').first().fill('conceicao');
  const from = page.getByRole('radio', { name: new RegExp(personName) });
  await from.check();
  await page.getByLabel('Pessoa ou entidade').last().fill('casino');
  const to = page.getByRole('radio', { name: new RegExp(clientName) });
  await to.focus();
  await page.keyboard.press('Space');
  await expect(to).toBeChecked();
  await page.getByRole('button', { name: 'Procurar caminhos' }).click();
  await expect(page.getByRole('heading', { name: /Caminho mais curto · 1 passo/ })).toBeVisible();
  const hop = page.locator('.path-hop').first();
  await expect(hop).toContainText('Cliente declarado');
  await expect(hop).toContainText(`${personName} declarou ${clientName} como cliente`);
  await expect(hop.locator('.evidence-link')).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)).toBe(false);
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
  await expect(page.getByRole('list', { name: 'Perfis públicos' }).getByRole('link')).toHaveCount(1);
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
