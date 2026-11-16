import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { ArchiveFile, ChatService } from './types';
import { Button, Header, Icon } from './ui';
import s from './App.module.css';
import { errorWithRequest } from './api/client';

export function sortFiles(files: ArchiveFile[], key: 'name' | 'date' | 'user' | undefined, ascending: boolean) {
  if (!key) return files;
  return [...files].sort((a, b) => a[key].localeCompare(b[key], 'ru') * (ascending ? 1 : -1));
}
export default function Archive({ service, demo = false }: { service: ChatService; demo?: boolean }) {
  const navigate = useNavigate();
  const [files, setFiles] = useState<ArchiveFile[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sort, setSort] = useState<'name' | 'date' | 'user'>();
  const [ascending, setAscending] = useState(true);
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState<string>();
  const api = service.mode === 'api';
  useEffect(() => { let alive = true; service.listArchive().then(items => { if (alive) setFiles(items); }).catch(err => { if (alive) setError(errorWithRequest(err)); }).finally(() => { if (alive) setLoading(false); }); return () => { alive = false; }; }, [service]);
  const sorted = useMemo(() => sortFiles(files, sort, ascending), [files, sort, ascending]);
  const count = Math.max(1, Math.ceil(files.length / pageSize));
  const visible = sorted.slice((page - 1) * pageSize, page * pageSize);
  const pages = [...new Set([1, 2, 3, 4, 5, count, ...(page > 5 ? [page] : [])])].filter(p => p <= count).sort((a, b) => a - b);
  const goAssistant = async () => { const sessions = await service.listSessions(); const active = sessions.find(item => !item.archived); navigate(active ? `/chat/${active.id}` : '/'); };
  async function download(file: ArchiveFile) {
    setDownloading(file.id); setError(undefined);
    try {
      const blob = await service.downloadArchive(file.id);
      const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `${file.name.replace(/[\\/:*?"<>|]/g, '_').slice(0, 100)}.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setError('Не удалось скачать файл. Попробуйте ещё раз.'); }
    finally { setDownloading(undefined); }
  }
  const setOrder = (key: 'name' | 'date' | 'user') => { setAscending(sort === key ? !ascending : true); setSort(key); setPage(1); };
  return <div className={`${s.app} ${s.archiveApp}`}>
    <Header active={false} onAssistant={() => void goAssistant()} />
    <div className={s.archiveWorkspace}><nav className={s.serviceNav} aria-label="Разделы Energytool">
      {api ? <><div className={s.navActive}><Icon name="document" /><span>Консультации</span></div><button className={`${s.navSub} ${s.navSubActive}`} onClick={() => { setPage(1); setSort(undefined); }}><span />Диалоги</button><button className={s.navMain} onClick={() => void goAssistant()}><Icon name="assistant" /><span>Помощник</span></button></> : <><div className={s.navActive}><Icon name="activity" /><span>Аномалии</span><Icon name="down" /></div>
      <button className={s.navSub} onClick={() => setError('Список устройств не входит в демонстрационный прототип. Доступны архив и помощник.')}><span />Устройства</button><button className={`${s.navSub} ${s.navSubActive}`} onClick={() => { setPage(1); setSort(undefined); setError(undefined); }}><span />Архив</button>
      <button className={s.navMain} onClick={() => setError('Управление не входит в демонстрационный прототип. Доступны архив и помощник.')}><Icon name="settings" /><span>Управление</span><Icon name="chevron" /></button></>}
    </nav><main className={s.archiveContent}>
      <div className={s.archiveHeading}><nav className={s.breadcrumbs} aria-label="Хлебные крошки"><span>{api ? 'Консультации' : 'Аномалии'}</span><Icon name="breadcrumb" /><span>Архив</span></nav><h1>{api ? 'Архив диалогов' : 'Архив результатов'}</h1></div>
      {error && <div role="alert" className={s.archiveNotice}>{error}<button aria-label="Закрыть уведомление" onClick={() => setError(undefined)}>×</button></div>}
      {api && <p className={s.archiveNotice}>Здесь доступны диалоги, открытые в этом сеансе работы. Полная история хранится на сервере и доступна в защищённом разделе проверки. {service.reviewAvailable && <Link to="/review">Все диалоги и заявки</Link>}</p>}
      <div className={s.tableScroll}><table className={s.table}><thead><tr>{(['name', 'date', 'user'] as const).map((key, i) => <th key={key} scope="col" aria-sort={sort === key ? ascending ? 'ascending' : 'descending' : 'none'}><button onClick={() => setOrder(key)}>{(api ? ['Диалог', 'Создан', 'Статус'] : ['Имя', 'Дата и время загрузки', 'Пользователь'])[i]}<span className={sort === key && !ascending ? s.reverse : ''}><Icon name="smallDown" /></span></button></th>)}<th scope="col"><span className={s.srOnly}>Скачать</span></th></tr></thead><tbody>
        {loading ? <tr><td colSpan={4}>Загружаем архив…</td></tr> : visible.map(file => <tr key={file.id}><td>{file.name}</td><td><div className={s.dateCell}><span>{new Date(file.date).toLocaleDateString('ru-RU')}</span><span>{new Date(file.date).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}</span></div></td><td><span className={s.userCell}>{!api && <span className={s.smallAvatar}><Icon name="user" /></span>}{file.user}</span></td><td><button className={s.download} onClick={() => void download(file)} disabled={downloading === file.id}>{downloading === file.id ? 'Загрузка…' : 'Скачать файл'}</button></td></tr>)}
        {!loading && !error && !files.length && <tr><td colSpan={4}>Пока нет сохранённых диалогов. <Button onClick={() => void goAssistant()}>Начать консультацию</Button></td></tr>}
      </tbody></table></div>
      <div className={s.pagination}><label>Отображать на странице<span className={s.selectWrap}><select aria-label="Отображать на странице" value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}><option>20</option><option>10</option><option>50</option></select><Icon name="paginationDown" /></span></label><nav aria-label="Страницы архива">{pages.map((p, i) => <span key={p}>{i > 0 && p - pages[i - 1] > 1 && <span className={s.ellipsis}>…</span>}<button className={page === p ? s.pageActive : ''} onClick={() => setPage(p)} aria-label={`Страница ${p}`} aria-current={page === p ? 'page' : undefined}>{p}</button></span>)}<button aria-label="Следующая страница" disabled={page === count} onClick={() => setPage(page + 1)}><Icon name="next" /></button></nav></div>
    </main></div>{demo && <Button className={s.demoBack} onClick={() => navigate('/demo')}>Все состояния</Button>}
  </div>;
}
