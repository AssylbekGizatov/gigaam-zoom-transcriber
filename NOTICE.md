# Происхождение и лицензии

Код приложения — MIT; исходное уведомление сохранено в LICENSE.
`engine.py`, алгоритм overlap в `pipeline.py`, загрузчик GigaAM и DejaVuSans
адаптированы/скопированы из
[AssylbekGizatov/gigaam-local-meeting-transcriber](https://github.com/AssylbekGizatov/gigaam-local-meeting-transcriber/tree/6f43fdd292f54234e6abbd51b0f97ea0aba93bea),
MIT, Copyright (c) 2026 Teams ASR Platform contributors.

| Компонент | Лицензия / источник |
|---|---|
| Локальный CPU CTC код GigaAM (`vendor/gigaam`) | MIT, [salute-developers/GigaAM](https://github.com/salute-developers/GigaAM/tree/7447938d791c4f3e643386ee22c33777004293a5). Полный MIT и список изменений включены. |
| GigaAM Multilingual 220M CTC weights | MIT согласно исходной model card в `assets/gigaam`. Веса без изменений в GitHub Release `models-v1`. |
| Python / Tcl/Tk | PSF / Tcl-Tk, поставляются с установленным Python. |
| PyTorch / torchaudio CPU | BSD-style; лицензии установленных distributions. |
| NumPy | BSD-3-Clause и bundled BLAS notices. |
| PyAudioWPatch / PortAudio | MIT / PortAudio MIT-style, [исходник](https://github.com/s0d3s/PyAudioWPatch). |
| PyWinRT | MIT, [исходник](https://github.com/pywinrt/pywinrt). |
| Windows.Media.Ocr | Системный компонент Windows, условия Windows. |
| Hydra / OmegaConf | MIT / BSD-3-Clause. |
| psutil | BSD-3-Clause. |
| python-docx / lxml | MIT / BSD-3-Clause; libxml2/libxslt notices. |
| ReportLab / Pillow | BSD-style / HPND. |
| DejaVu Sans | Bitstream Vera/DejaVu, полный текст в `fonts/LICENSE.txt`. |
| pytest / pypdf | MIT / BSD-3-Clause, только проверки. |

Закреплённые версии — `requirements-desktop.txt` и `constraints.txt`.
Полные лицензии зависимостей доступны в их dist-info. Приложение не поставляет
бинарный EXE и не использует внешнюю модель определения личности по голосу.

Веса GigaAM: revision `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8`,
config SHA256 `c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653`,
weights SHA256 `e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728`.
Код GigaAM: commit `7447938d791c4f3e643386ee22c33777004293a5`.
Источники и размеры закреплены в `assets/model-manifest.json`.

Существующий исторический GitHub Release может содержать дополнительные
артефакты прежних версий. Текущая установка загружает только GigaAM и не
изменяет опубликованные assets или их исходные уведомления о лицензиях.

Исторические публичные тестовые материалы: FLEURS (Google, Conneau et al.,
2022), CC BY 4.0; LibriSpeech (Vassil Panayotov et al., OpenSLR12), CC BY 4.0;
русский официальный пример GigaAM из MIT-репозитория. Происхождение, хеши и
ограничения сохранены в `docs/evidence`. Аудио в репозиторий не включено.
