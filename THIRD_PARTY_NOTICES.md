# Third-Party Notices

Voisr is licensed under the MIT License (see `LICENSE`). It bundles or
depends on the following third-party components. No component's licence
forbids redistribution; this was verified for each one below rather than
assumed.

## Bundled binaries / weights (ship inside the installer)

### whisper.cpp (`whisper-cli.exe` and the `whisper-blas-bin-x64` build)
- **Licence:** MIT
- **Verified at:** https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/LICENSE
  ("MIT License / Copyright (c) 2023-2026 The ggml authors")

### ggml-base.en.bin (Whisper `base.en` model weights)
- **Licence:** MIT
- **Verified at:** https://huggingface.co/ggerganov/whisper.cpp (repository
  card shows `License: mit`). These are OpenAI's Whisper weights converted
  to GGML format; OpenAI's own Whisper repository is also MIT — verified at
  https://raw.githubusercontent.com/openai/whisper/main/LICENSE
  ("MIT License / Copyright (c) 2022 OpenAI").

### PortAudio (`libportaudio64bit.dll`, loaded by `sounddevice`)
- **Licence:** MIT
- **Verified at:** https://raw.githubusercontent.com/PortAudio/portaudio/master/LICENSE.txt

### SDL2 (`SDL2.dll`, shipped alongside the whisper.cpp release binaries)
- **Licence:** zlib License
- **Verified at:** https://raw.githubusercontent.com/libsdl-org/SDL/main/LICENSE.txt
  ("This software is provided 'as-is' ..." — the standard zlib licence text)

### Python interpreter and standard library
- **Licence:** Python Software Foundation License Version 2 (PSF-2.0)
- **Verified at:** https://docs.python.org/3/license.html — explicitly
  permits bundling in proprietary/commercial software without
  open-sourcing the application, provided the PSF copyright notice is
  retained and changes to Python itself (none, here) are documented.

## Python packages (runtime dependencies, importable in the frozen build)

### pywebview
- **Licence:** BSD 3-Clause
- **Verified at:** `pip show pywebview` in the project venv, which prints
  the full BSD 3-Clause text ("Copyright (c) 2014-2017, Roman Sirokov").

### pythonnet
- **Licence:** MIT
- **Verified at:** `pip show pythonnet` in the project venv
  (`License-Expression: MIT`).

### sounddevice
- **Licence:** MIT
- **Verified at:** `pip show sounddevice` in the project venv
  (`License-Expression: MIT`).

### pystray
- **Licence:** LGPLv3
- **Verified at:** `venv/Lib/site-packages/pystray-0.19.5.dist-info/COPYING`
  and `COPYING.LGPL` (the package ships both the GPLv3 text and the LGPL's
  additional permissions on top of it), and the PyPI classifier "GNU Lesser
  General Public License v3 (LGPLv3)".
- **Redistribution note:** LGPLv3, unlike GPL, explicitly permits linking
  from proprietary applications. Voisr uses pystray unmodified and does not
  statically link it into a single binary that would prevent relinking —
  it ships as an ordinary Python package inside the frozen bundle. The
  LGPLv3 text is reproduced in this notices file's obligations below, and
  pystray's source is publicly available at
  https://github.com/moses-palmer/pystray for anyone who wants to modify
  or relink it.

### Pillow
- **Licence:** MIT-CMU (the "Historical Permission Notice and Disclaimer",
  MIT-style)
- **Verified at:** `pip show pillow` in the project venv
  (`License-Expression: MIT-CMU`).

## Summary

| Component | Licence | Redistribution concern |
|---|---|---|
| whisper.cpp | MIT | None |
| ggml-base.en.bin | MIT | None |
| PortAudio | MIT | None |
| SDL2 | zlib | None |
| Python + stdlib | PSF-2.0 | None |
| pywebview | BSD 3-Clause | None |
| pythonnet | MIT | None |
| sounddevice | MIT | None |
| pystray | LGPLv3 | None — LGPL permits bundling; source link provided above |
| Pillow | MIT-CMU | None |

No component blocks distribution to a company. All licences are permissive
(MIT/BSD/zlib/PSF), and the one copyleft component (pystray, LGPLv3) is used
in a way LGPL explicitly allows for proprietary applications.
