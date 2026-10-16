# Происхождение и лицензии

Новый код приложения — MIT; исходное MIT-уведомление сохранено в LICENSE.
`engine.py`, алгоритм overlap в `pipeline.py`, `scripts/acquire_model.py` и
DejaVuSans адаптированы/скопированы из
[AssylbekGizatov/gigaam-local-meeting-transcriber](https://github.com/AssylbekGizatov/gigaam-local-meeting-transcriber/tree/6f43fdd292f54234e6abbd51b0f97ea0aba93bea),
MIT, Copyright (c) 2026 Teams ASR Platform contributors. Upstream читался только
read-only. Его история, версии и настройки не менялись.

| Компонент | Лицензия / источник |
| --- | --- |
| GigaAM native code | MIT, [salute-developers/GigaAM](https://github.com/salute-developers/GigaAM/tree/7447938d791c4f3e643386ee22c33777004293a5) |
| GigaAM Multilingual 220M CTC weights | MIT по [model card](https://huggingface.co/ai-sage/GigaAM-Multilingual/tree/2f8a57144e6ec3adfd32fe0484d9ea9913305bc8); веса отдельно |
| Python / Tcl/Tk | PSF / Tcl-Tk, поставляются с вашим Python |
| PyTorch / torchaudio CPU | BSD-style, upstream licenses в установленных distributions |
| NumPy | BSD-3-Clause; собственные bundled BLAS notices |
| SentencePiece | Apache-2.0 |
| Hydra / OmegaConf | MIT / BSD-3-Clause |
| websockets | BSD-3-Clause |
| psutil | BSD-3-Clause |
| python-docx / lxml | MIT / BSD-3-Clause (libxml2/libxslt notices) |
| ReportLab / Pillow | BSD-style / HPND |
| DejaVu Sans | Bitstream Vera/DejaVu, полный текст `fonts/LICENSE.txt` |
| pytest / pypdf | MIT / BSD-3-Clause, только проверка |
| Zoom API / RTMS | Отдельные условия Zoom Developer Platform, не MIT приложения |

Полный перечень фактически установленных версий — constraints.txt; лицензии
зависимостей доступны в их Python dist-info. ONNX/ONNXRuntime/soundfile
устанавливаются как зависимости GigaAM, native CTC путь не использует облачное
STT. Бинарный EXE и модель в этот репозиторий не поставляются; при их дальнейшем
распространении нужно включить полные лицензии всего бинарного состава.

Веса: revision `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8`, config SHA256
`c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653`, weights SHA256
`e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728`.
Код GigaAM: commit `7447938d791c4f3e643386ee22c33777004293a5`.

Публичные тесты: FLEURS (Google, Conneau et al., 2022), CC BY 4.0; LibriSpeech
(Vassil Panayotov et al., OpenSLR12), CC BY 4.0; русский официальный example GigaAM
в MIT repository. Источники, преобразования, SHA256 и ограничения перечислены в
docs/evidence и TESTING.md. Аудио остаётся локально и не публикуется.
