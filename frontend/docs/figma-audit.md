# Сверка с Figma — 13 ноября 2026

Повторно прочитаны design context и изображения всех 22 состояний страницы «Консультант v2». Структура макетов сохранилась. Снимки приложения проверены на desktop 1440×960 и mobile 390×844.

Базовые параметры: Inter 400/500/600, шапка 64 px, история 360/72 px, отладка 360 px, отступы 24 px, фон #E4E1F5 и исходные локальные SVG с неизменёнными корневыми размерами. Чат, форма, карточки, история, сброс и архив сохранены. Уточнены внутренние интервалы карточек продуктов (24/16/32 px) и success-цвет #137A49. Индикатор задержки: до 2 секунд зелёный, 2–5 фиолетовый, более 5 красный; порог высокой релевантности источника — 80%.

| Экран / состояние | Figma node |
|---|---|
| [Вопрос клиента, бот печатает](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=74-389) | `74:389` |
| [Новый диалог](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-364) | `75:364` |
| [Уточняющий вопрос и быстрые ответы](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-531) | `75:531` |
| [Подбор продуктов](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-719) | `75:719` |
| [Предложение передать менеджеру](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-963) | `75:963` |
| [Форма заявки: пустая](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-1139) | `75:1139` |
| [Форма заявки: ошибка телефона](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-1358) | `75:1358` |
| [Заявка отправлена и резюме](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-1618) | `75:1618` |
| [Меню свёрнуто: подбор продуктов](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=75-1879) | `75:1879` |
| [Отладка: уточняющий вопрос](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=77-1038) | `77:1038` |
| [Отладка: подбор продуктов](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=77-1300) | `77:1300` |
| [Отладка: нет в базе](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=77-1598) | `77:1598` |
| [Нет в базе: предложение специалиста](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=78-1352) | `78:1352` |
| [Вне компетенции](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=78-1517) | `78:1517` |
| [Смена темы: солнечные станции поверх счёта](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=78-1753) | `78:1753` |
| [Возврат к первой теме: счёт](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=78-1996) | `78:1996` |
| [Ошибка сети: повторить](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=78-2175) | `78:2175` |
| [Подтверждение сброса](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=79-1758) | `79:1758` |
| [Чистый диалог после сброса](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=79-1994) | `79:1994` |
| [Прошлая сессия: только чтение](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=79-2177) | `79:2177` |
| [Пустая история](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=79-2369) | `79:2369` |
| [Архив результатов](https://www.figma.com/design/NsFKHEsWB7tR7pY4pzThhY/?node-id=91-2214) | `91:2214` |

Дополнительно проверены варианты Chat input (8:62): активная отправка красная, иконки filled/sending экспортированы отдельно; SVG не перекрашиваются CSS-фильтрами.

## Изменения только для API-режима

- Добавлен обязательный серверный предпросмотр заявки и отдельное подтверждение. Использованы существующие стили карточек и кнопок.
- Подтверждение сообщает о сохранении в прототипе, без обещания передачи в CRM. Содержимое резюме приходит с сервера.
- История API не восстанавливается из localStorage; закрытые серверные обращения и технические источники доступны через защищённый review.
- API не подставляет моковые продукты, быстрые ответы или уверенность: соответствующих публичных полей в OpenAPI нет. Все исходные визуальные состояния остаются доступны в /demo.
- Мобильная компоновка адаптирована из desktop-макетов: история и отладка открываются отдельными панелями, продукты располагаются вертикально.

## Проверка

Моковый Playwright-набор повторно проверяет 22 состояния, загрузку и исходные размеры каждого видимого SVG, отсутствие горизонтального переполнения и создаёт 44 снимка. API-набор отдельно проверяет инициализацию, 202/polling, повтор 503 без дублей, изоляцию секретов, частичную заявку и PREVIEW_STALE. Скриншоты: test-results/screens и test-results-api/screens.
