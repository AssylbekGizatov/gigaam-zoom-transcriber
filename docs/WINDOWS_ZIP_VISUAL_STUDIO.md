# Запуск из ZIP на корпоративном Windows 11

Нужен установленный Python 3.13 **x64** с Tcl/Tk и рабочий Zoom Workplace.
Никакой IDE, Git, virtual environment или запуск с правами администратора
приложению не требуется.

1. На GitHub нажмите Code → Download ZIP. Распакуйте в локальную папку,
   например `C:\Users\User\Documents\GigaAM`. Не запускайте изнутри ZIP.
2. Откройте эту папку в Проводнике, в адресной строке напишите `cmd` и Enter.
3. Проверьте интерпретатор:

```bat
py -3.13 -c "import sys, struct; print(sys.version); print(struct.calcsize('P') * 8)"
```

Должны выводиться версия 3.13 и `64`. Обычная сборка CPython нужна, не
free-threaded Python. Окно Tcl/Tk можно проверить `py -3.13 -m tkinter`.

4. Установите все библиотеки одной командой:

```bat
py -3.13 -m pip install --user -r requirements-desktop.txt -c constraints.txt
```

Она устанавливает CPU PyTorch, локальный код GigaAM, WASAPI, Windows OCR
и WinRT-модули Windows Graphics Capture. После обновления исходников повторите
эту же команду для новых зависимостей; веса GigaAM повторно скачивать не нужно.
При этом нужны доступы к PyPI / files.pythonhosted.org и download.pytorch.org.
Если организация использует свой каталог пакетов, согласуйте установку с IT;
не изменяйте Group Policy и не отключайте проверку TLS.

5. Для уже имеющейся модели выполните проверку:

```bat
py -3.13 scripts\acquire_model.py --check
```

При отсутствии модели скачайте её:

```bat
py -3.13 scripts\acquire_model.py
```

Источник — [GitHub Release models-v1](https://github.com/AssylbekGizatov/gigaam-zoom-transcriber/releases/tag/models-v1).
Нужны github.com и release-assets.githubusercontent.com. Hugging Face не
используется. Веса проверяются по SHA256, повреждённые файлы не подменяются.
Для ручного переноса поместите разрешённый `pytorch_model.bin` из этого релиза
в отдельную папку и выполните:

```bat
py -3.13 scripts\acquire_model.py --source-directory "C:\ApprovedModels"
```

6. Запустите:

```bat
py -3.13 app.py
```

После установки можно дважды нажимать `Start_GigaAM.cmd`. Эта обёртка запускает
указанный системный Python и показывает сообщение при ошибке. PowerShell-
скрипты не нужны, ExecutionPolicy не меняется.

## Частые ошибки

| Ошибка | Проверка |
|---|---|
| WinError 1260 при `.venv\Scripts\python.exe` | Используйте команды `py -3.13` выше; `.venv` не нужен. |
| `py` не найден или Python 3.13 отсутствует | Нужна разрешённая установка Python 3.13 x64 и Python Launcher. |
| `No module named ...` | Повторите единую pip-команду через тот же `py -3.13`, которым запускаете приложение. |
| Нет подходящего колеса пакета | Проверьте 64-bit CPython 3.13, версии из requirements и доступ к индексам. |
| pip заблокирован / каталог read-only | Нужен согласованный с IT способ установки; обход политики не предусмотрен. |
| OCR язык недоступен | Выберите установленный язык либо согласуйте компонент Windows OCR с IT. |
| Имя не появляется | Разверните Zoom, проверьте Speaker View, калибровку и `capture_method`/`capture_attempts`. При отказе WGC/PrintWindow уберите перекрытие для screen DC. |
| Модель не найдена | Проверьте папку в настройках и выполните `acquire_model.py --check`. |

В Visual Studio, VS Code или PyCharm выберите **системный Python 3.13 x64**,
файл `app.py`, рабочую папку проекта. Не позволяйте IDE автоматически выбирать
старый `.venv`. Остальные настройки описаны в [руководстве](DESKTOP_GUIDE.md).

## Корпоративный proxy и перенос модели

Загрузчик использует стандартные настройки HTTPS proxy Python. Если IT выдал
адрес proxy без секретов и корпоративный PEM-сертификат, их можно задать только
для текущего CMD, например:

```bat
set HTTPS_PROXY=http://proxy.company.example:8080
set SSL_CERT_FILE=C:\ApprovedCertificates\company-ca.pem
py -3.13 scripts\acquire_model.py
```

Это примеры: используйте реальные согласованные значения, не отключайте TLS
и не помещайте пароли в команду или репозиторий. Для pip собственный PEM
задаётся `PIP_CERT`. Если proxy не разрешает GitHub/CDN, перенесите вес из
`models-v1` разрешённым способом и используйте `--source-directory`, как выше.
Скачивать с Hugging Face или менять сетевую политику не требуется.

## Возврат к предыдущей копии

Перед обновлением распакуйте новую версию в отдельную папку и сохраните старую
папку приложения. Не удаляйте каталог результатов и `.sqlite` сеансы. Настройки
приложения хранятся отдельно от исходников; перед сменой версии сохраните их
локальную копию, особенно если меняется формат калибровки.

Для возврата остановите приложение и запустите `py -3.13 app.py` из предыдущей
папки. Если менялись версии библиотек, установите зависимости этой конкретной
версии через тот же системный Python согласно её документации. Не заменяйте
и не удаляйте веса и результаты ради отката. Старая версия может не понимать
новые настройки — восстановите их сохранённую копию; документы SQLite храните.

Для отката именно этого обновления сохраните настройки **до первого запуска**.
В CMD, если файл уже существует:

```bat
if exist "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.json" copy /Y "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.json" "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.before-ocr.json"
```

Закройте приложение, распакуйте [стартовую версию ветки](https://github.com/AssylbekGizatov/gigaam-zoom-transcriber/archive/5e7a4baed99385c077fa976cd25d220aa15c7020.zip)
в отдельную папку. Из этой папки:

```bat
if exist "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.before-ocr.json" copy /Y "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.before-ocr.json" "%LOCALAPPDATA%\GigaAMZoom\desktop-settings.json"
py -3.13 -m pip install --user -r requirements-desktop.txt -r requirements-zoom-ocr.txt -c constraints.txt
py -3.13 app.py
```

При необходимости снова выберите прежние папки модели и текста.
