import type { TurnProgress } from './types';
import { Icon } from './ui';
import s from './App.module.css';

const labels: Record<TurnProgress['stage'], string> = {
  queued: 'Запрос принят',
  analyzing: 'Учитываю контекст диалога',
  preparing_answer: 'Готовлю ответ',
  finalizing: 'Сохраняю результат',
};

export function TurnIndicator({ progress }: { progress?: TurnProgress }) {
  const label = progress && labels[progress.stage] || labels.preparing_answer;
  const keywords = [...new Set((progress?.keywords ?? []).filter(word => typeof word === 'string' && word.trim()))];
  return <div className={s.message}>
    <span className={`${s.avatar} ${s.messageAvatar}`}><Icon name="bot" /></span>
    <div className={s.turnIndicator} role="status" aria-live="polite" aria-atomic="true">
      <div className={s.turnStage}><span className={s.typing} aria-hidden="true"><span /><span /><span /></span><span>{label}</span></div>
      {keywords.length > 0 && <div className={s.turnKeywords}>{keywords.map(word => <span key={word}>{word}</span>)}</div>}
    </div>
  </div>;
}
