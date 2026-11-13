import { expect, test } from '@playwright/test';
import { demoScreens } from '../src/demo';
import { copy } from '../src/data';

test('полная консультация, перезагрузка, сброс и чтение истории', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Здравствуйте! Я помощник МТС EnergyTool' })).toBeVisible();
  const input = page.getByRole('textbox', { name: 'Сообщение' });
  await input.fill(copy.initial);
  await expect(page.getByRole('button', { name: 'Отправить сообщение' })).toHaveCSS('background-color', 'rgb(255, 0, 50)');
  await expect(page.getByRole('button', { name: 'Отправить сообщение' }).locator('img')).toHaveAttribute('src', '/assets/0ae4f.svg');
  await input.press('Enter');
  await expect(page).toHaveURL(/\/chat\//);
  await page.getByRole('button', { name: 'Не знаю. Где это посмотреть?', exact: true }).click();
  await page.getByRole('button', { name: 'Обычный счётчик', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Облачная платформа учёта', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Подробнее', exact: true }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: 'Обсудить стоимость', exact: true }).click();
  await page.getByRole('button', { name: 'Да, передайте', exact: true }).click();
  await page.getByLabel('ФИО', { exact: true }).fill('Иванов Иван Иванович');
  await page.getByLabel('Телефон', { exact: true }).fill('+7 900 000-00-00');
  await page.getByLabel('Компания', { exact: true }).fill('ООО «Вечерний свет»');
  await page.getByLabel('Согласен на обработку персональных данных').check();
  await page.getByRole('button', { name: 'Отправить заявку', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Заявка отправлена' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Заявка отправлена' })).toBeVisible();
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click();
  await page.getByRole('button', { name: 'Начать заново', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Здравствуйте! Я помощник МТС EnergyTool' })).toBeVisible();
  await page.getByRole('button', { name: /Вырос счёт: вечерняя нагрузка/ }).click();
  await expect(page.getByText(/Прошлый диалог от/)).toBeVisible();
  await expect(input).toHaveCount(0);
  await page.getByRole('button', { name: 'Продолжить в новом диалоге' }).click();
  await expect(input).toBeVisible();
});

test('сбой сети сохраняется и повтор не дублирует сообщение', async ({ page, context }) => {
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Сообщение' }).fill('Вырос счёт');
  await context.setOffline(true);
  await page.getByRole('button', { name: 'Отправить сообщение' }).click();
  await expect(page.getByRole('button', { name: 'Повторить', exact: true })).toBeVisible();
  await context.setOffline(false);
  await page.getByRole('button', { name: 'Повторить', exact: true }).click();
  await expect(page.getByText(/Да, можем помочь разобраться/)).toBeVisible();
  await expect(page.getByText('Вырос счёт', { exact: true })).toHaveCount(1);
});

test('архив сортируется, перелистывается и скачивает csv', async ({ page }) => {
  await page.goto('/archive');
  await expect(page.getByText('devicelist_20092022')).toBeVisible();
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Скачать файл' }).first().click();
  expect((await downloading).suggestedFilename()).toBe('devicelist_20092022.csv');
  await page.getByRole('button', { name: 'Страница 2', exact: true }).click();
  await expect(page.getByText('devicelist_20092022')).toHaveCount(0);
  await page.getByRole('button', { name: 'Имя', exact: true }).click();
  await expect(page.getByRole('columnheader', { name: 'Имя', exact: true })).toHaveAttribute('aria-sort', 'ascending');
});

test('22 состояния: локальные ассеты, геометрия и скриншоты', async ({ page }) => {
  test.setTimeout(120_000);
  const errors: string[] = [];
  page.on('pageerror', err => errors.push(err.message));
  for (const [id, , state] of demoScreens) {
    await page.goto(`/demo?screen=${encodeURIComponent(id)}`);
    await page.evaluate(() => document.fonts.ready);
    await expect(page.getByRole('banner')).toBeVisible();
    if (state !== 'archive') await expect(page.getByRole('main', { name: 'Диалог с помощником' })).toBeVisible();
    const assets = await page.locator('img').evaluateAll(imgs => imgs.filter(img => img.getBoundingClientRect().width > 0).map(img => ({ url: img.getAttribute('src'), loaded: img.complete && img.naturalWidth > 0, width: img.getBoundingClientRect().width, height: img.getBoundingClientRect().height, naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight })));
    expect(assets.length).toBeGreaterThan(5);
    for (const asset of assets) {
      expect(asset.loaded, `${state}: ${asset.url}`).toBe(true);
      expect(asset.url).toMatch(/^\/assets\//);
      expect(asset.width).toBe(asset.naturalWidth); expect(asset.height).toBe(asset.naturalHeight);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/screens/desktop-${state}.png`, fullPage: true });
  }
  expect(errors).toEqual([]);
});

test('мобильная версия: история, отладка, форма, клавиатура', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/demo?screen=75%3A719');
  await page.getByRole('button', { name: 'Открыть историю диалогов' }).click();
  await expect(page.getByRole('complementary', { name: 'История диалогов' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть историю', exact: true }).last().click();
  await page.getByRole('button', { name: 'Открыть отладку' }).click();
  await expect(page.getByRole('complementary', { name: 'Режим отладки' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть отладку', exact: true }).last().click();
  for (const [id, , state] of demoScreens) {
    await page.goto(`/demo?screen=${encodeURIComponent(id)}`);
    await page.evaluate(() => document.fonts.ready);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), state).toBe(true);
    await page.screenshot({ path: `test-results/screens/mobile-${state}.png`, fullPage: true });
  }
});
