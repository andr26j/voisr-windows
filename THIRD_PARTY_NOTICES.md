# Third-Party Notices

Voisr is licensed under the MIT License (see `LICENSE`). It bundles or
depends on the following third-party components. No component's licence
forbids redistribution; this was verified for each one below rather than
assumed.

## Bundled binaries / weights (ship inside the installer)

### whisper.cpp (`whisper-cli.exe`, `whisper-server.exe` and the `whisper-blas-bin-x64` build)
- **Licence:** MIT
- **Verified at:** https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/LICENSE
  ("MIT License / Copyright (c) 2023-2026 The ggml authors")
- **Use:** both programs come from the same v1.9.1 release zip and run as
  separate processes. `whisper-server.exe` (the kept-loaded model) is reached
  only over 127.0.0.1.

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

## Fonts and icons in the dashboard (ship inside the installer)

### Instrument Serif (`voisr/web/fonts/InstrumentSerif-Regular.ttf` and `InstrumentSerif-Italic.ttf`)
- **Licence:** SIL Open Font License 1.1
- **Copyright:** 2022 The Instrument Serif Project Authors
  (https://github.com/Instrument/instrument-serif)
- **Verified at:** the copies are unmodified, from https://github.com/google/fonts ,
  and the licence travels beside them as `voisr/web/fonts/OFL.txt`, as the
  OFL requires. The OFL permits bundling and redistribution with software,
  including commercial software; the fonts may not be sold on their own.
- **Network:** Voisr loads them only from those files, and never asks Google
  or anyone else for a font.

### Lucide icons (the small line icons drawn in the dashboard)
- **Licence:** ISC License, with some icons also under MIT (from Feather)
- **Copyright:** (c) 2026 Lucide Icons and Contributors; (c) 2013-present
  Cole Bemis (Feather)
- **Verified at:** https://github.com/lucide-icons/lucide (LICENSE). Most of
  the dashboard's small icons are copied or adapted from Lucide. The ISC text:

  > Permission to use, copy, modify, and/or distribute this software for any
  > purpose with or without fee is hereby granted, provided that the above
  > copyright notice and this permission notice appear in all copies.
  >
  > THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
  > WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
  > MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
  > ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
  > WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
  > ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
  > OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## Downloaded on request (not in the installer)

### ggml-small.en.bin, ggml-medium.en-q5_0.bin, ggml-large-v3-turbo-q5_0.bin
- **Licence:** MIT, as `ggml-base.en.bin` above: the same repository and the
  same OpenAI Whisper weights, converted (and for the last two, quantized to
  5 bits) by the whisper.cpp author.
- **Verified at:** https://huggingface.co/ggerganov/whisper.cpp (`license: mit`,
  checked 2026-11-03). Downloaded from there only when the user clicks
  Download in Settings → Models, into `%LOCALAPPDATA%\Voisr\models`.

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

### certifi (2026.1.4)
- **Licence:** MPL-2.0
- **Verified at:** https://github.com/certifi/python-certifi (LICENSE) and the
  package metadata of the pinned version.
- **Use:** its CA bundle (`cacert.pem`) is added to the Windows certificate
  store for model downloads. Shipped unmodified; MPL-2.0 is file-level
  copyleft and asks only that the source of the covered files stay available,
  which it is at the address above.

## Summary

| Component | Licence | Redistribution concern |
|---|---|---|
| whisper.cpp | MIT | None |
| ggml-base.en.bin | MIT | None |
| PortAudio | MIT | None |
| SDL2 | zlib | None |
| Python + stdlib | PSF-2.0 | None |
| Instrument Serif | SIL OFL 1.1 | None — licence ships beside the fonts |
| Lucide icons | ISC (some MIT) | None |
| pywebview | BSD 3-Clause | None |
| pythonnet | MIT | None |
| sounddevice | MIT | None |
| pystray | LGPLv3 | None — LGPL permits bundling; source link provided above |
| Pillow | MIT-CMU | None |
| certifi | MPL-2.0 | None — unmodified; source link provided above |
| small.en / medium.en-q5_0 / large-v3-turbo-q5_0 (downloaded) | MIT | None — not redistributed by Voisr |

No component blocks distribution to a company. All licences are permissive
(MIT/BSD/zlib/PSF/ISC, and the OFL for the font), and the two copyleft
components (pystray, LGPLv3; certifi, MPL-2.0) are used unmodified, in a way
their licences explicitly allow for proprietary applications.
