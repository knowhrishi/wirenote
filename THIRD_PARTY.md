# Third-party components

Wirenote is licensed under the AGPL-3.0-or-later. It uses the following components, which keep their own licenses. Versions are those in `uv.lock` at release time.

## Python packages (direct dependencies)

| Package | License |
| --- | --- |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | Apache-2.0 |
| [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast) | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [icalendar](https://github.com/collective/icalendar) | BSD-2-Clause |
| [recurring-ical-events](https://github.com/niccokunzmann/python-recurring-ical-events) | LGPL-3.0-or-later |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 |

## Notable indirect dependencies

| Package | License |
| --- | --- |
| [x-wr-timezone](https://github.com/niccokunzmann/x-wr-timezone) | LGPL-3.0-or-later |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause; its wheels include FFmpeg libraries under the LGPL-2.1-or-later |
| [huggingface_hub](https://github.com/huggingface/huggingface_hub), [tokenizers](https://github.com/huggingface/tokenizers) | Apache-2.0 |
| [NumPy](https://numpy.org) | BSD-3-Clause and others |

`uv tree` lists the full set; each installed package's license is in its metadata.

## Models

Downloaded from Hugging Face on first use, at fixed revisions; not included in the package.

| Model | Used for | License |
| --- | --- | --- |
| [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) by OpenAI, in the [CTranslate2 conversion](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) | transcription | MIT |
| [pyannote segmentation 3.0](https://huggingface.co/pyannote/segmentation-3.0), in the [sherpa-onnx conversion](https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0) | finding where speakers change | MIT |
| [NVIDIA NeMo TitaNet-S](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/nemo/models/titanet_small), in the [sherpa-onnx conversion](https://huggingface.co/csukuangfj/speaker-embedding-models) | telling speakers apart | covered by the [NeMo Toolkit license](https://github.com/NVIDIA/NeMo/blob/main/LICENSE) (Apache-2.0), per its model card |

The local summary model (default `qwen3:4b`) is installed by you through Ollama and is not part of Wirenote.

## Fonts

| Font | License |
| --- | --- |
| [Inter](https://rsms.me/inter/) by Rasmus Andersson | SIL Open Font License 1.1 (`src/wirenote/ui/static/fonts/OFL.txt`) |

## External programs

Wirenote runs these if they are installed; they are not distributed with it: PipeWire tools (`pw-dump`, `pw-record`), FFmpeg, `xdg-open`, and for the clipboard `wl-copy`, `xclip` or `xsel`.
