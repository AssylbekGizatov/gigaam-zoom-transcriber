# Проверки проекта

## Автоматические тесты

Из корня проекта в обычном CMD:

```bat
py -3.13 -m pip install --user -r requirements-dev.txt -c constraints.txt
py -3.13 -m pytest -q
py -3.13 scripts/check_publication.py
```

Unit tests используют синтетические слова, наблюдения имён и подмены аудиоустройств.
Они проверяют таймкоды, смены/пропуски имён, ограниченные очереди, lifecycle,
сохранение и экспорт. Эти проверки не доказывают точность Windows OCR, GigaAM
или переключения Speaker View настоящего Zoom.

CI запускает тесты на Windows/Linux с Python 3.12/3.13. Отдельный job устанавливает
единый desktop-runtime с Python 3.13, скачивает проверенную GigaAM из GitHub и
запускает её на синтетическом PCM при блокировке Python network calls. Он не
входит в Zoom и не снимает реальные окна встречи.

## Проверки на машине пользователя

```bat
py -3.13 scripts/acquire_model.py --check
py -3.13 scripts/probe_zoom_names.py --list-languages
py -3.13 scripts/probe_zoom_names.py --help
py -3.13 scripts/probe_desktop_audio.py --help
py -3.13 scripts/evaluate_desktop.py --help
```

`evaluate_desktop.py` воспроизводит разрешённые публичные WAV с проверенными
хешами через реальный выход Windows, получает WASAPI-звук, распознаёт и
проверяет экспорты. Это проверка звука и ASR; наблюдатель имён отключён.
`scripts/soak.py` проверяет pipeline/SQLite с fake ASR ускоренным потоком;
он не равен многочасовой настоящей Zoom-встрече.

Исторический [аппаратный отчёт](evidence/desktop-loopback.json) относится к
старому прогону на другой Windows-сборке. [Проверка offline GigaAM](evidence/no-huggingface-runtime.json)
также не подтверждает нынешний OCR. Не переносите прежние числа тестов и
задержки на новую сборку без повторения.

Для настоящей встречи используйте [сценарий приёмки](DESKTOP_TESTING.md).
Не сохраняйте в публичном репозитории частные имена, реплики, кадры или аудио.


## Разрешение зависимостей CPython 3.13 Windows x64

[Отчёт проверки](evidence/windows313-dependencies.json) фиксирует успешный
`pip --dry-run` для `win_amd64`, CPython 3.13 (`cp313`). Проверены все desktop-
зависимости, включая CPU Torch/Torchaudio, PyAudioWPatch и WinRT OCR/WGC. Для шести
новых WGC-модулей проверены и сами wheels с нативными `.pyd` для `cp313-win_amd64`. На Linux
Windows-маркеры верхнего уровня были явно включены для этой проверки;
чисто Python wheels vendored GigaAM и ANTLR были предварительно собраны локально.
Это проверяет совместимость метаданных и наличие подходящих wheels, но не
исполнение Windows DLL, качество OCR или аппаратный звук. Обычная пользовательская
pip-команда сама собирает эти два чисто Python пакета без C/C++ компилятора.
