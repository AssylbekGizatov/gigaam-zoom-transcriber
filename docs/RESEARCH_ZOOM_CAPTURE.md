# Живой звук Zoom без записи встречи: техническое исследование

Проверено по официальным источникам **4 ноября 2026 года**. Это документ о выборе
транспорта аудио; он не заменяет отчёт о реальном испытании приложения.

## Вывод и выбранная граница

Уже воспроизводимый на компьютере звук Zoom можно передать локальной GigaAM через
Windows WASAPI. Нажатие Zoom Record, файл записи и платный RTMS для этого не нужны.
Звук существует кратковременно в ограниченной RAM-очереди. На диск можно сохранять
только текст и таймкоды. Это означает отсутствие дополнительного тарифа сервиса,
а не отсутствие расходов на уже существующий компьютер, электричество или интернет.

Но звук устройства вывода — **сумма голосов**, без отдельных Zoom ID. Нельзя обещать
достоверную связь каждого слова с реальным участником только на основании такого
звука. Наше приложение должно явно различать источник микрофона пользователя,
смешанный удалённый звук, подсказку имени из интерфейса и подтверждённый источник
Zoom. Неизвестное имя лучше ложного имени.

## Сравнение способов

| Способ | Без файлов записи / Zoom Record | Отдельные источники Zoom | Дополнительная тарификация | Решение |
|---|---|---|---|---|
| Windows process loopback | Да | Нет | Нет тарифа Zoom за этот Windows API | Предпочтительный локальный захват при наличии реализации |
| WASAPI выбранного устройства вывода | Да | Нет; также слышны другие программы на этом выходе | Нет | Практичный fallback |
| Chrome/Edge tab capture | Да | Нет; звук выбранной вкладки | Нет тарифа Zoom за браузерный захват | Альтернатива для Zoom Web App |
| RTMS | Да | Да | Зависит от Developer Pack/условий аккаунта | Не включать в режиме «без затрат» |
| Meeting SDK raw audio | Технически да, RAM callbacks | Да, с исключениями | Не RTMS metering, но действуют условия SDK | Новую AI-notetaker интеграцию не строить на этом без разъяснения Zoom |
| Импорт локальной записи | Нет | Зависит от записи | Сам импорт локальный | Не соответствует текущему запросу |

## Windows: что действительно доступно

Microsoft предоставляет **process loopback**: `ActivateAudioInterfaceAsync` с
`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` захватывает конкретный процесс и
его дочерние процессы, независимо от устройства вывода. Это позволяет выбирать
Zoom, исключая музыку и уведомления других приложений. Требуется Windows build
20348 или выше. Официальный пример пишет WAV, однако запись файла является частью
примера: обработчик приложения может передавать PCM напрямую в ASR.
[Microsoft sample](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/),
[API prerequisites](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ne-audioclientactivationparams-audioclient_activation_type).

Обычный **endpoint loopback** возвращает общий звук выбранных динамиков/наушников.
Это полезный совместимый вариант, но выбор устройства не равен выбору процесса.
[WASAPI loopback](https://learn.microsoft.com/en-gb/windows/win32/coreaudio/loopback-recording).
Библиотека [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) предоставляет
такой захват из Python, Windows wheels и MIT-лицензию. Она не обещает раздельные
аудиодорожки Zoom или process isolation.

Собственный голос обычно не возвращается Zoom в наушники: микрофон нужен отдельным
каналом. При работе через динамики локальный микрофон может снова услышать удалённых
участников; предпочтительны наушники. Приложение должно позволять отключить собственный
микрофон и явно показывать источники, потерю устройства и пропуски аудио.

## Браузер и добровольная демонстрация вкладки

Chrome `tabCapture` предоставляет MediaStream выбранной вкладки только после
действия пользователя. Для продолжительной обработки MV3 использует offscreen
document; начиная с Chrome 116 stream ID из service worker можно передать туда.
Полученный аудиопоток нужно также подключить к `AudioContext.destination`, иначе
пользователь перестанет слышать вкладку. AudioWorklet может передавать PCM локальному
серверу без `MediaRecorder` и без файлов видео/аудио.
[Chrome tabCapture](https://developer.chrome.com/docs/extensions/reference/api/tabCapture),
[официальные patterns](https://developer.chrome.com/docs/extensions/how-to/web-platform/screen-capture).

Вариант `getDisplayMedia()` показывает пользователю выбор экрана/окна/вкладки.
Доступность аудио зависит от выбранной поверхности и браузера. Само разрешение на
демонстрацию не создаёт раздельные голоса и не раскрывает Zoom ID. Чтение DOM Zoom
Web App — нестабильный адаптер интерфейса, а не поддерживаемый Zoom API источников.

## Почему не новый бот на Meeting SDK

В текущей официальной [главной странице Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/)
есть **Meeting SDK usage policy**, направляющая ботов и AI notetakers на RTMS.
То же предупреждение размещено в [Windows overview](https://developers.zoom.us/docs/meeting-sdk/windows/)
и [инструкции загрузки](https://developers.zoom.us/docs/meeting-sdk/windows/get-started/download/).
Поэтому техническое наличие raw API не означает поддерживаемость новой AI-notetaker
интеграции. Более старая [OBF FAQ](https://developers.zoom.us/docs/meeting-sdk/obf-faq/)
ещё описывает legacy note-taking apps. Это расхождение документации нужно признать,
а не интерпретировать как разрешение обходить актуальное ограничение.

Технически Windows SDK умеет получать raw PCM в RAM, не создавая MP4/M4A. Для
raw recording нужны recording privileges и уведомление Zoom; для raw streaming —
live streaming privileges и licensed host Pro/Business/Education/Enterprise.
SDK предоставляет `onOneWayAudioRawDataReceived` с ID источника, но этот callback
не поддерживает телефонных участников. [Raw data guide](https://developers.zoom.us/docs/meeting-sdk/windows/default-ui/advanced-features/raw-data/).

В текущей C++ reference методы называются `RequestRawLiveStreaming(url, name)` и
`StartRawLiveStreaming(url, name)`, завершение — `StopRawLiveStream()`. Некоторые
текстовые руководства используют старое имя `StartRawLiveStream`; компилировать
нужно по headers конкретного SDK. [Live stream controller reference](https://marketplacefront.zoom.us/sdk/meeting/windows/class_i_meeting_live_stream_controller.html).

Официальные [rate limits](https://developers.zoom.us/docs/api/rate-limits/) сообщают,
что Meeting SDK включён в Free/Pro/Business-планы. Это не доказательство бесплатности
RTMS и не исключение из usage policy. Для доступа к внешним встречам дополнительно
требуются review и авторизация ZAK/OBF; Local Test внутри developer account имеет
другую границу доступа. [Credentials / Local Test](https://developers.zoom.us/docs/meeting-sdk/get-credentials/).

Video SDK относится к собственным видеосессиям приложения и не является заменой
транспорта для обычной встречи Zoom Meetings. Сравнивать его бесплатные минуты с
RTMS или доступом Meeting SDK некорректно.

## Имена говорящих: что можно утверждать

1. **Прямой per-user audio callback/пакет** позволяет связывать фразу с ID источника.
   Общий микрофон всё равно представляет устройство/участника Zoom, а не каждого
   человека в комнате.
2. **Windows/браузерный mixed audio** такого ID не содержит. Диаризация может выдать
   кластеры «Говорящий 1/2», но не узнать реальные Zoom имена. Ручное присвоение имени
   кластеру — пользовательская разметка, а не идентификатор Zoom.
3. **Active speaker в UI** — временная подсказка. Задержка отображения, одновременная
   речь, скрытое окно, pin/spotlight и обновление Zoom могут сделать её неверной.
   Обычная большая карточка или имя в списке участников не доказывают говорящего.
   [Zoom layouts](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0063672),
   [spotlight](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0066300).
4. Официальный **Zoom Apps SDK** предоставляет `getMeetingParticipants`,
   `onParticipantChange`, `onActiveSpeakerChange` только host/co-host и без guest
   mode. Это метаданные активности, а не per-user audio. Такой sidecar также требует
   HTTPS; официальный sample не поддерживает localhost как адрес Zoom App.
   [SDK reference](https://appssdk.zoom.us/classes/ZoomSdk.ZoomSdk.html),
   [официальный sample](https://github.com/zoom/zoomapps-advancedsample-react).

## Реализованный осторожный адаптер Windows accessibility

`gigaam_zoom/zoom_accessibility.py` читает только видимые окна процесса `Zoom.exe`.
Он не нажимает кнопки, не меняет настройки Zoom и не записывает screenshots или UI
тексты в лог. Принимаются только явно размеченные строки вида `Active speaker: …`,
`… is speaking`, `Говорит: …`; обычные имена/participant list не используются.
Это контракт парсера, **не утверждение, что установленная версия Zoom предоставляет
именно такие строки**. До реальной проверки клиента адаптер считается экспериментальным.

Наблюдение содержит монотонное время, имя, evidence и локальный идентификатор
`local:zoom-uia:…`, который никогда не выдаётся за Zoom ID. Два разных элемента с
одинаковым именем дают ambiguous. Нет имени, неполный обход, исчезновение окна,
ошибка UIA, таймаут или старые данные дают unknown. Повторно вошедшего участника
нельзя автоматически объединять со старым по совпадению имени.

COM/UIA вынесены в отдельный уничтожаемый процесс, GUI получает только последний
снимок. Опрос не чаще 2 раз/с, максимум 12 окон/512 элементов/12 уровней и ограниченный
бюджет обхода. Снимки старше 2 секунд не используются. Такой подход учитывает
рекомендации Microsoft о [потоках UIA](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
и [ограничении обхода дерева](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-obtainingelements).

Не следует переносить имя, наблюдаемое сейчас, на ранее накопленный аудиобуфер.
Сопоставление выполняется по времени наблюдений; смешанные и неопределённые интервалы
остаются явно помеченными. Дополнительный локальный диаризатор можно исследовать
через [sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html),
но его точность и нагрузку нужно измерять отдельно от ASR.

## Проверка перед заявлением о готовности

Нужна живая встреча как минимум с двумя раздельными источниками речи: проверить
собственный микрофон и удалённый голос, смену говорящего, одинаковые имена,
переименование, выход/возврат, screen share, сворачивание окна, перекрывающуюся речь,
тишину и смену аудиоустройства. Нужно измерить ASR latency, CPU, RAM и потерянные
фреймы с реальным аудиотранспортом. Тест WAV, синтетические UI labels или зелёные
unit-тесты сами по себе этого не подтверждают.
