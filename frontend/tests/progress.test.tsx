import { render, screen } from '@testing-library/react';
import { TurnIndicator } from '../src/TurnIndicator';

it('показывает метки сразу после приёма запроса и обновляет их вместе с этапом', () => {
  const { rerender } = render(<TurnIndicator progress={{ stage: 'queued', keywords: ['Вода', 'Передача показаний', 'Счётчики'] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('Запрос принят');
  expect(screen.getByText('Вода')).toBeVisible();
  expect(screen.getByText('Передача показаний')).toBeVisible();
  expect(screen.getByText('Счётчики')).toBeVisible();
  rerender(<TurnIndicator progress={{ stage: 'finalizing', keywords: [] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('Сохраняю результат');
  expect(screen.queryByText('Передача показаний')).not.toBeInTheDocument();
  rerender(<TurnIndicator />);
  expect(screen.getByRole('status')).toHaveTextContent('Готовлю ответ');
});
