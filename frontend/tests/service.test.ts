import { createMockService, STORAGE_KEY, validateLead } from '../src/service';
import { memoryStorage, demoFixture, demoScreens } from '../src/demo';
import { copy, seedSessions } from '../src/data';
import { sortFiles } from '../src/Archive';

const contact = { fullName: 'Иванов Иван Иванович', phone: '+7 900 000-00-00', company: 'ООО «Вечерний свет»', consent: true };
describe('Моковый сервис', () => {
  it('проводит консультацию от вопроса до заявки и сохраняет данные после перезагрузки', async () => {
    const storage = memoryStorage(seedSessions());
    const service = createMockService(storage, 0);
    let session = await service.createSession();
    session = await service.sendMessage(session.id, copy.initial);
    expect(session.stage).toBe('clarify');
    session = await service.sendMessage(session.id, 'Не знаю. Где это посмотреть?');
    expect(session.stage).toBe('meter');
    session = await service.sendMessage(session.id, copy.meterUser);
    expect(session.stage).toBe('products');
    session = await service.sendMessage(session.id, 'Сколько это стоит?');
    expect(session.stage).toBe('offer');
    session = await service.sendMessage(session.id, 'Да, передайте');
    expect(session.stage).toBe('form');
    session = await service.submitLead(session.id, contact);
    expect(session.stage).toBe('sent');
    expect(session.status).toBe('request');
    expect(session.lead?.summary).toContain(copy.initial);
    const restored = createMockService(storage, 0);
    expect((await restored.listSessions())[0].lead).toEqual(session.lead);
    expect((await restored.submitLead(session.id, contact)).lead?.id).toBe(session.lead?.id);
  });
  it('сохраняет неотправленную реплику и повторяет её без дублей', async () => {
    const service = createMockService(memoryStorage([]), 0);
    const session = await service.createSession();
    let result = await service.sendMessage(session.id, copy.initial, { simulateError: true });
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].delivery).toBe('failed');
    const retryId = result.messages[0].id;
    const reloaded = await service.listSessions();
    expect(reloaded[0].messages[0].delivery).toBe('failed');
    result = await service.sendMessage(session.id, copy.initial, { retryId });
    expect(result.messages).toHaveLength(2);
    expect(result.messages[0].delivery).toBe('sent');
    result = await service.sendMessage(session.id, copy.initial, { retryId });
    expect(result.messages).toHaveLength(2);
  });
  it('архивирует сессию, сбрасывает контекст и запрещает изменение истории', async () => {
    const service = createMockService(memoryStorage([]), 0);
    const original = await service.createSession();
    await service.sendMessage(original.id, copy.initial);
    const next = await service.createSession();
    expect(next.messages).toEqual([]);
    expect(next.stage).toBe('start');
    await expect(service.sendMessage(original.id, 'Сообщение')).rejects.toThrow('только для чтения');
    await expect(service.submitLead(original.id, contact)).rejects.toThrow('только для чтения');
    const continued = await service.createSession(original.id);
    expect(continued.id).not.toBe(original.id);
    expect(continued.messages).toHaveLength(2);
    expect(continued.messages[0].text).toBe(copy.initial);
    expect(continued.archived).toBe(false);
  });
  it('отвечает по сценариям отсутствия информации и вне компетенции', async () => {
    const service = createMockService(memoryStorage([]), 0);
    const session = await service.createSession();
    expect((await service.sendMessage(session.id, 'Зарядные станции для электромобилей')).stage).toBe('nokb');
    expect((await service.sendMessage(session.id, 'Законно ли отключать без суда?')).stage).toBe('scope');
  });
  it('сохраняет факты при смене темы и возвращении', async () => {
    const service = createMockService(memoryStorage([]), 0);
    const session = await service.createSession();
    await service.sendMessage(session.id, copy.initial);
    await service.sendMessage(session.id, copy.meterUser);
    let result = await service.sendMessage(session.id, 'А солнечные панели?');
    expect(result.stage).toBe('solar');
    expect(result.context.meter).toBe(copy.meterUser);
    result = await service.sendMessage(session.id, 'Вернёмся к счёту');
    expect(result.stage).toBe('return');
    expect(result.messages.at(-1)?.note).toContain('Помню');
  });
  it('проверяет телефон, обязательные поля и согласие', () => {
    expect(validateLead(contact)).toEqual({});
    expect(validateLead({ ...contact, phone: '+7 900 000-00' })).toHaveProperty('phone');
    expect(validateLead({ ...contact, consent: false })).toHaveProperty('consent');
    expect(Object.keys(validateLead({ fullName: ' ', phone: '', company: '', consent: false }))).toHaveLength(4);
  });
  it('отклоняет некорректную заявку', async () => {
    const service = createMockService(memoryStorage(demoFixture('form')), 0);
    await expect(service.submitLead('demo-current', { ...contact, consent: false })).rejects.toThrow('Проверьте');
    expect((await service.listSessions())[0].lead).toBeUndefined();
  });
  it('восстанавливает повреждённый кеш', async () => {
    const storage = memoryStorage([]); storage.setItem(STORAGE_KEY, 'broken json');
    const sessions = await createMockService(storage, 0).listSessions();
    expect(sessions.length).toBe(4);
  });
  it('сортирует архив и создаёт файл с демонстрационными данными', async () => {
    const service = createMockService(memoryStorage([]), 0);
    const files = await service.listArchive();
    const sorted = sortFiles(files, 'name', true);
    expect(sorted[0].name.localeCompare(sorted[1].name, 'ru')).toBeLessThanOrEqual(0);
    expect(sortFiles(files, 'name', false).at(-1)).toEqual(sorted[0]);
    expect((await service.downloadArchive('1')).size).toBeGreaterThan(20);
    await expect(service.downloadArchive('missing')).rejects.toThrow();
  });
  it('содержит все 22 изолированных состояния Figma', () => {
    expect(demoScreens).toHaveLength(22);
    for (const screen of demoScreens) expect(demoFixture(screen[2]).length).toBeGreaterThan(0);
    expect(demoFixture('readonly').every(s => s.archived)).toBe(true);
    expect(demoFixture('network')[0].messages[0].delivery).toBe('failed');
  });
});
