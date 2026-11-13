import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Review from '../src/Review';
import { ApiError } from '../src/api/client';
import { createMockService } from '../src/service';
import { memoryStorage } from '../src/demo';
import { makeSession } from '../src/data';
import type { ChatService } from '../src/types';

function service(): ChatService {
  const session = { ...makeSession('server-session'), source: 'api' as const, archived: true, title: 'Проверочный диалог', messages: [{ id: 'server-answer', role: 'bot' as const, text: 'Ответ из серверной истории' }] };
  return {
    ...createMockService(memoryStorage([]), 0), mode: 'api', reviewAvailable: true,
    listReviewSessions: vi.fn().mockResolvedValue([session]), listReviewLeads: vi.fn().mockResolvedValue([]),
    getReviewSession: vi.fn().mockResolvedValue(session),
    getTrace: vi.fn().mockResolvedValue({ responseMs: 1200, confidence: 'не указана', clarification: false, documents: [{ path: 'server-doc', score: 89, chunkId: 'chunk-1', quote: 'Серверный фрагмент' }] }),
  };
}
describe('Защищённый review', () => {
  it('не делает запросов без включённого доступа', () => {
    const api = { ...service(), reviewAvailable: false };
    render(<MemoryRouter><Review service={api} /></MemoryRouter>);
    expect(screen.getByRole('heading', { name: 'Раздел проверки закрыт' })).toBeVisible();
    expect(api.listReviewSessions).not.toHaveBeenCalled();
  });
  it('читает историю и запрашивает trace выбранного ответа', async () => {
    const user = userEvent.setup(); const api = service();
    render(<MemoryRouter><Review service={api} /></MemoryRouter>);
    await user.click(await screen.findByRole('button', { name: /Проверочный диалог/ }));
    expect(await screen.findByText('Ответ из серверной истории')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Показать источники ответа' }));
    expect(await screen.findByText('server-doc')).toBeVisible();
    expect(api.getTrace).toHaveBeenCalledWith('server-answer');
    expect(screen.queryByRole('textbox', { name: 'Сообщение' })).not.toBeInTheDocument();
  });
  it('закрывает review и очищает видимые данные после отказа в доступе', async () => {
    const user = userEvent.setup(); const api = service();
    api.getTrace = vi.fn().mockRejectedValue(new ApiError('Нет доступа', 'FORBIDDEN', 403, false));
    render(<MemoryRouter><Review service={api} /></MemoryRouter>);
    await user.click(await screen.findByRole('button', { name: /Проверочный диалог/ }));
    await user.click(await screen.findByRole('button', { name: 'Показать источники ответа' }));
    expect(await screen.findByRole('heading', { name: 'Раздел проверки закрыт' })).toBeVisible();
    expect(screen.queryByText('Ответ из серверной истории')).not.toBeInTheDocument();
  });
});
