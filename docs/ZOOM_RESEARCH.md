# Zoom: выбранный путь и проверенные ограничения

Проверено 16 октября 2026 года по официальным документам Zoom. Наличие кода
адаптера и прохождение имитационных тестов не подтверждают доступ к конкретному
Zoom-аккаунту, оплату RTMS или работу на реальной встрече.

## Решение

Выбран **RTMS для обычных Zoom Meetings, только аудио**, с локальным получателем
WebSocket и локальным GigaAM. Это инженерный выбор для приложения распознавания:
не нужен отдельный бот, клиент Meeting SDK и управление его локальной записью.
Zoom прямо рекомендует RTMS для постоянного доступа к медиа и автоматической
записи. [Сравнение вариантов Zoom](https://developers.zoom.us/blog/app-attribution-path/),
[текущие требования Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/obf-faq/).

RTMS не создаёт ещё одного участника или отдельную плитку. Приложение связано с
присутствующим пользователем. Zoom показывает приложение на его плитке и в
Active Apps Notifier; все видят уведомление о передаче содержания приложениям.
Хост может разрешить или отклонить доступ и остановить приложение. Нельзя
выдавать этот режим за незаметную запись.
[Официальный интерфейс RTMS](https://developers.zoom.us/docs/rtms/meetings/ux-overview/),
[уведомления хоста](https://developers.zoom.us/docs/rtms/meetings/ux-host/).

Сеть Zoom доставляет аудио на компьютер; распознавание и документы создаются
здесь. Сам Zoom и первоначальная загрузка модели используют сеть. Zoom cloud
transcription и внешние LLM не требуются. Следует запрашивать только audio scope,
а не transcript/video/chat.

## Почему не Meeting SDK и не Video SDK

| Вариант | Возможности и цена сложности |
| --- | --- |
| RTMS | WebSocket, идентификаторы источников, без отдельного участника; требует Developer Pack credits и разрешений хоста. |
| Windows Meeting SDK | Нативный SDK умеет `onOneWayAudioRawDataReceived` и смешанный звук. Нужны права local recording либо raw streaming, видимое присутствие клиента и уведомления. Per-user callback не поддерживает телефонных участников. |
| Video SDK | Собственные видеосессии; не может подключаться к обычным Zoom Meetings. |
| Захват системного звука | Смешанный звук не содержит достоверных ID источников, поэтому не удовлетворяет основной задаче атрибуции. |

[Windows raw data и требования](https://developers.zoom.us/docs/meeting-sdk/windows/default-ui/advanced-features/raw-data/),
[различие Video SDK и Meetings](https://zoom.us/pricing/developer).

С 2 марта 2026 года Meeting SDK для входа в чужой аккаунт требует атрибуции.
Для автоматизированного участника это JWT + OBF, полученный через OAuth
пользователя, который уже присутствует во встрече. Уход этого пользователя
завершает SDK-сессию. Это существенно усложняет надёжный локальный бот.
[Официальный FAQ OBF](https://developers.zoom.us/docs/meeting-sdk/obf-faq/).

## Оплата: отдельный блокирующий шаг

Для Meetings RTMS официально нужны активный **Zoom Developer Pack** и достаточные
кредиты. Слова «Developer-лицензия» не доказывают наличие такого пакета, остатка
или разрешения расходовать кредиты. Пакет может отличаться от Zoom Build
Platform/Video SDK credits.
[Предпосылки RTMS](https://developers.zoom.us/docs/rtms/meetings/getting-started/),
[диагностика планов и лимитов](https://developers.zoom.us/docs/rtms/troubleshooting/).

Публичная [страница Developer pricing](https://zoom.us/pricing/developer)
на дату проверки описывает Developer Pack, но в доступном HTML **не раскрывает
точную числовую ставку**. Поэтому этот проект не утверждает цену за минуту,
бесплатную квоту, цену кредита или нулевое списание. Для реального запуска нужно
получить отображаемое Zoom предложение/договор именно этого аккаунта:
ставка за audio RTMS, единица тарификации, включённые кредиты, округление,
автопополнение/overage и лимит расходов. Только после объяснения этих условий
и явного подтверждения владельца можно нажимать оплачиваемый Start. Покупки,
автопополнение, принятие соглашений и изменения безопасности не выполнялись.

## Однократная настройка владельцем аккаунта

1. Войти в [Zoom App Marketplace](https://marketplace.zoom.us/) официальным
   браузером. Не передавать пароль приложению или в GitHub.
2. Создать **General App, user-managed** для личного использования внутри своего
   аккаунта. RTMS scopes/events требуют именно user-managed приложения.
3. Добавить `meeting:read:meeting_audio`. Для ручного старта/стопа REST добавить
   `meeting:update:participant_rtms_app_status`. Дополнительные profile/meeting
   scopes нужны только соответствующим функциям интерфейса.
4. Настроить события `meeting.rtms_started`, `meeting.rtms_stopped`; для
   восстановления также `meeting.rtms_interrupted`, если оно доступно в UI.
   Документированный RTMS путь — публичный HTTPS webhook endpoint, его URL
   validation и подпись запросов. Указывайте endpoint этого компьютера через
   выбранный владельцем безопасный туннель. Туннель переносит webhook-метаданные;
   RTMS audio принимается отдельными исходящими WSS-соединениями.
5. Настроить точный OAuth redirect URI и разрешённые URL; подключить приложение
   через Local Test. Сохранить Client ID/Secret и Secret Token только локально.
6. По отдельному согласию владельца разрешить Zoom Apps → Share realtime meeting
   content with apps. Разрешение приложения и аккаунтные переключатели являются
   постоянными разрешениями; автоматизация не включает их без подтверждения.
7. Не включать auto-start до подтверждения тарификации: он способен запустить
   поток при входе во встречу. Для ежедневной работы предпочтителен ручной старт.

[Настройка scopes и событий](https://developers.zoom.us/docs/rtms/meetings/add-features/),
[пример настройки localhost](https://developers.zoom.us/docs/rtms/meetings/quickstart/),
[контроль хоста и администратора](https://developers.zoom.us/docs/rtms/meetings/ux-host-admin-tools-ctrls/).

REST Start использует `PATCH /v2/live_meetings/{meetingId}/rtms_app/status` с
`action: start` и `settings.client_id`; Stop — `action: stop`. Произвольная
ссылка на встречу не даёт прав доступа. Документация ограничивает старт для
участника тем, что он уже вошёл и является приглашённым участником, а не просто
получил общую ссылку. Права хоста и политики встречи также действуют.
[Официальный REST quickstart](https://developers.zoom.us/docs/rtms/meetings/quickstart-rest-api/).

## Исходящие события WebSocket: условный дополнительный путь

Общий Zoom Event WebSocket API поддерживает General apps и Server-to-Server
apps: `client_credentials` token, URL из Marketplace с `subscriptionId`, heartbeat
`{"module":"heartbeat"}` каждые 30 секунд. Это account-level канал только для
своего аккаунта. События за период разрыва не переотправляются.
[Официальная спецификация](https://developers.zoom.us/docs/api/websockets/).

Однако RTMS setup отдельно требует user-managed General App и документирует
webhook. В доступной документации не найдено однозначного обещания, что RTMS
lifecycle events можно выбрать в Event WebSocket именно такого приложения.
Используйте этот путь только если Marketplace реально предлагает RTMS events
для WebSocket-подписки и проверена доставка `meeting.rtms_started`. До этой
проверки нельзя обещать отсутствие HTTPS endpoint/туннеля. S2S приложение само
по себе не заменяет RTMS General App. Событийный WebSocket и два RTMS WebSocket —
разные протоколы с разной авторизацией и heartbeat.

## Аудио и атрибуция

Запрашивается отдельный PCM16 mono 16 kHz поток (`data_opt=2`), до трёх активных
источников за интервал 20 мс. Это не обещание произвольного числа одновременных
независимых микрофонов.
[Параметры медиа](https://developers.zoom.us/docs/rtms/media-parameter-definition/).

Каждый аудиопакет несёт Zoom `user_id`, `user_name` и timestamp сервера. Для
смешанного потока ID равен `0`; приложение должно показывать неизвестный
источник. Имя не является уникальным ID человека: одинаковые имена, общий
микрофон, переименования и новый ID после повторного входа требуют сохранения
истории источников. Нельзя объединять их по сходству голоса или имени.
[Доставка аудио](https://developers.zoom.us/docs/rtms/meetings/media/).

## Транспорт в этом проекте

Официальная SDK-обёртка RTMS на дату проверки публикует поддержку Python/Node
только для `darwin-arm64` и `linux-x64`. Для Windows реализован открытый
документированный WebSocket protocol, без неподдерживаемого бинарного wrapper.
[Официальный SDK repository](https://github.com/zoom/rtms).

Сначала открывается signaling WSS, затем audio WSS. Подпись — HMAC-SHA256 от
Client ID, meeting UUID и stream ID. На media handshake запрашивается audio-only
PCM. Типы сообщений и параметры заданы числами, проверяются статус и согласованный
формат. TLS и проверка hostname обязательны; повторно проверяются redirect URL.
Пакеты и очереди ограничены. Остановка запрашивает `STREAM_CLOSE_REQ` до закрытия
сокетов; при отсутствии подтверждения интерфейс обязан потребовать Stop в Zoom
или REST Stop, а не обещать прекращение списания.
[Universal events](https://developers.zoom.us/docs/rtms/event-reference/universal-events/).

Начало отсчёта берётся из первого signaling timestamp, сохраняется при
переподключении. Этот timestamp следует обозначать как начало **RTMS-сессии**:
при включении посреди встречи нельзя обещать, что он совпадает с фактическим
началом встречи без дополнительного `meeting.started`/проверенного start time.
Если он отсутствует, интерфейс должен явно обозначить отсчёт от первого
принятого аудио. Для join/leave подписывается signaling; ID stream, поколения
соединений и повторного входа сохраняются отдельно от реального `user_id`.
[Session events](https://developers.zoom.us/docs/rtms/event-reference/session-events/).

Для media-only разрыва восстанавливается audio socket, для потери signaling —
оба соединения. Повторный `rtms_started` активного stream ID может означать
failover с новыми URL. Число попыток ограничено; разрывы, повторы и отсутствие
пакетов показываются, а не скрываются вымышленным текстом.
[Failover and reconnection](https://developers.zoom.us/docs/rtms/meetings/failover-reconnection/).

## Публикация и реальные блокеры

Публичный GitHub-репозиторий исходников не равен публикации Zoom Marketplace
приложения. Для внешних пользователей Zoom требует App Review; private app
допустим для собственного аккаунта. Новые пользователи/права/публичная публикация
приложения требуют соответствующих действий владельца.
[App Review RTMS](https://developers.zoom.us/docs/rtms/meetings/submit-app-review/).

Реальная встреча остаётся непроверенной, пока не подтверждены тариф, доступ к
Marketplace, scopes, OAuth, доставка lifecycle events, разрешение хоста и
реальные аудиопакеты. Синтетический сервер проверяет протокол и сбои; он не
подменяет эту приёмку.

## Проверка транспорта в этой поставке

`tests/test_rtms.py`: **30 проверок прошли локально**, включая сигнатуру,
полный обмен сообщениями, идентичные имена разных ID, временные метки,
перекрытие/дубли пакетов, смешанный звук, повторный вход, media-only reconnect,
failover с новым URL, отмену без остановки удалённого потока, ограниченные
повторные попытки, фильтр endpoint/redirect и некорректные пакеты.

Отдельная проверка открывает настоящий TLS/TCP/WebSocket сервер на loopback
`127.0.0.1:443`, генерирует одноразовый сертификат во временном каталоге и
проверяет цепочку доверия и hostname. Через него проходят handshake,
keepalive, PCM и подтверждённый Stop. Это имитация протокола с реальными
локальными сокетами, **не реальная Zoom-встреча**. В CI эта проверка пропускается
только если недоступны openssl или loopback-порт443; остальные проверки от них
не зависят. Тестовый private key не хранится в репозитории.
