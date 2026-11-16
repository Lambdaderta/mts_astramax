import { render, screen } from '@testing-library/react';
import { TurnIndicator } from '../src/TurnIndicator';

it('показывает темы как текст, обновляет этап и убирает старые метки при отсутствии прогресса', () => {
  const { rerender } = render(<TurnIndicator progress={{ stage: 'analyzing', keywords: ['Газ', 'Тарифы'] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('Учитываю контекст диалога');
  expect(screen.getByText('Тарифы')).toBeVisible();
  rerender(<TurnIndicator progress={{ stage: 'finalizing', keywords: [] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('Сохраняю результат');
  expect(screen.queryByText('Газ')).not.toBeInTheDocument();
  rerender(<TurnIndicator />);
  expect(screen.getByRole('status')).toHaveTextContent('Готовлю ответ');
});
