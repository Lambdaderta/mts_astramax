import { copy, makeSession, msg, seedSessions } from './data';
import type { Session, Stage } from './types';

export const demoScreens = [
  ['75:364', 'Новый диалог', 'start'], ['74:389', 'Вопрос клиента, бот печатает', 'typing'],
  ['75:531', 'Уточняющий вопрос и быстрые ответы', 'meter'], ['75:719', 'Подбор продуктов', 'products'],
  ['75:963', 'Предложение передать менеджеру', 'offer'], ['75:1139', 'Форма заявки: пустая', 'form'],
  ['75:1358', 'Форма заявки: ошибка телефона', 'phone-error'], ['75:1618', 'Заявка отправлена и резюме', 'sent'],
  ['75:1879', 'Меню свёрнуто: подбор продуктов', 'collapsed'], ['77:1038', 'Отладка: уточняющий вопрос', 'debug-meter'],
  ['77:1300', 'Отладка: подбор продуктов', 'debug-products'], ['77:1598', 'Отладка: нет в базе', 'debug-nokb'],
  ['78:1352', 'Нет в базе: предложение специалиста', 'nokb'], ['78:1517', 'Вне компетенции', 'scope'],
  ['78:1753', 'Смена темы: солнечные станции поверх счёта', 'solar'], ['78:1996', 'Возврат к первой теме: счёт', 'return'],
  ['78:2175', 'Ошибка сети: повторить', 'network'], ['79:1758', 'Подтверждение сброса', 'reset'],
  ['79:1994', 'Чистый диалог после сброса', 'clean'], ['79:2177', 'Прошлая сессия: только чтение', 'readonly'],
  ['79:2369', 'Пустая история', 'empty'], ['91:2214', 'Архив результатов', 'archive'],
] as const;
export type DemoState = typeof demoScreens[number][2];
export function demoFixture(state: DemoState): Session[] {
  const base = { ...makeSession('demo-current'), title: 'Вырос счёт: вечерняя нагрузка', createdAt: '2026-11-12T14:32:00' };
  let stage: Stage = 'products';
  if (['start', 'clean', 'empty'].includes(state)) stage = 'start';
  else if (state.includes('meter')) stage = 'meter';
  else if (state.includes('nokb')) stage = 'nokb';
  else if (['scope', 'solar', 'return', 'offer', 'form', 'sent'].includes(state)) stage = state as Stage;
  else if (state === 'phone-error') stage = 'form';
  if (state === 'readonly') return [{ ...makeSession('previous-bill'), title: 'Вырос счёт: вечерняя нагрузка', createdAt: '2026-11-12T14:32:00', status: 'request', archived: true, stage: 'products', messages: [msg('user', copy.meterUser), msg('bot', copy.products, 'products')] }, ...seedSessions()];
  let messages = [msg('user', copy.meterUser), msg('bot', copy.products, 'products'), msg('bot', 'Вот что подойдёт', 'products')];
  if (stage === 'start') messages = [];
  if (state === 'typing') messages = [msg('user', copy.initial)];
  if (state === 'debug-products') messages = [msg('bot', copy.products, 'products'), msg('bot', 'Вот что подойдёт', 'products')];
  if (stage === 'meter') messages = [msg('user', copy.initial), msg('bot', copy.clarify, 'clarify'), msg('user', 'Не знаю. Где это посмотреть?'), msg('bot', copy.meter, 'meter')];
  if (stage === 'offer') messages = [msg('user', 'Сколько это стоит?'), msg('bot', copy.offer, 'offer')];
  if (stage === 'form') messages = [msg('user', 'Да, передайте'), msg('bot', copy.form, 'form')];
  if (stage === 'sent') messages = [msg('user', 'Да, передайте')];
  if (stage === 'nokb') { messages = [msg('user', 'Ставите ли вы зарядные станции для электромобилей?'), msg('bot', copy.nokb, 'nokb')]; base.title = 'Зарядные станции для электромобилей'; }
  if (stage === 'scope') { messages = [msg('user', 'А отключать должников без суда законно? Нас потом не накажут?'), msg('bot', copy.scope, 'scope')]; base.title = 'Отключение должников: законно ли'; }
  if (stage === 'solar') messages = [msg('user', copy.meterUser), msg('user', 'А солнечные панели нам вообще подойдут? Нагрузка в основном вечером'), { ...msg('bot', copy.solar, 'solar'), note: 'Новая тема: солнечные станции' }, msg('bot', 'Вот два варианта для сравнения', 'solar')];
  if (stage === 'return') messages = [msg('user', 'Вернёмся к счёту за свет в нашем кафе. Так что с вечерней нагрузкой?'), { ...msg('bot', copy.return, 'return'), note: 'Помню, о чём мы говорили: вечерняя нагрузка, обычный счётчик' }];
  if (state === 'network') messages = [{ ...msg('user', 'Сколько это стоит?'), delivery: 'failed' }];
  const current: Session = { ...base, stage, messages, status: stage === 'sent' ? 'request' : 'none' };
  if (stage === 'sent') current.lead = { id: 'demo-lead', sessionId: current.id, fullName: 'Иванов Иван Иванович', phone: '+7 900 000-00-00', company: 'ООО «Вечерний свет»', consent: true, createdAt: current.createdAt, summary: 'Вырос счёт: вечерняя нагрузка' };
  if (state === 'clean') return [current, { ...demoFixture('products')[0], id: 'old-demo', archived: true }, ...seedSessions()];
  return [current, ...(state === 'empty' ? [] : seedSessions().filter(s => s.title !== current.title))];
}
export function memoryStorage(sessions: Session[]): Storage {
  const map = new Map([['energytool.mock.v1', JSON.stringify(sessions)]]);
  return { get length() { return map.size; }, clear: () => map.clear(), getItem: k => map.get(k) ?? null, setItem: (k, v) => { map.set(k, v); }, removeItem: k => { map.delete(k); }, key: i => [...map.keys()][i] ?? null };
}
