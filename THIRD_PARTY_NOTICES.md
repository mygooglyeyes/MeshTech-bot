# Third-Party Notices

MeshTech-Bot builds on open-source software. We thank the authors and
keep their license notices here, as their licenses require and good
practice encourages. All of the components below are permissive-licensed
(MIT, BSD, PSF, or SIL OFL) and are compatible with this project's MIT
license.

## Python libraries (runtime)

| Library | Version | License | Copyright / Notes |
|---|---|---|---|
| meshcore | 2.3.9.1 | MIT | © Florent de Lamotte |
| PyYAML | 6.0.3 | MIT | © Kirill Simonov and contributors |
| fastapi | 0.141.1 | MIT | © FastAPI authors |
| uvicorn | 0.52.4 | BSD-3-Clause | © uvicorn contributors |
| websockets | 17.1 | BSD-3-Clause | © Aymeric Augustin and contributors |
| starlette | 1.6.0 | BSD-3-Clause | © Starlette contributors |
| pydantic | 2.13.5 | MIT | © Samuel Colvin and contributors |
| pydantic-core | 2.13.5 | MIT | © Pydantic contributors |
| anyio | 4.15.0 | MIT | © Alex Grönholm |
| click | 8.5.0 | BSD-3-Clause | © Armin Ronacher and contributors |
| h11 | 0.16.0 | MIT | © Nathaniel J. Smith |
| typing-extensions | 4.16.0 | PSF-2.0 | Python Software Foundation |

These are installed by `pip install -r requirements.txt` from PyPI; each
package ships its own full license text in its distribution.

### Development only

| Library | Version | License | Notes |
|---|---|---|---|
| pytest | 9.1.1 | MIT | Test framework; `requirements-dev.txt` only, not part of the app |

## JavaScript

| Library | Version | License | Copyright / Notes |
|---|---|---|---|
| marked | 15.0.12 | MIT | © Christopher Jeffrey. Used only by the internal docs-preview tooling (`.freebuff/`); not shipped with the app. |

## Fonts

| Font | License | Notes |
|---|---|---|
| IBM Plex Mono | SIL Open Font License 1.1 | Loaded from Google Fonts via `<link>` in the dashboard page; not bundled. If you ever self-host the font files, keep the OFL notice alongside them. |

## Container base image

| Image | License | Notes |
|---|---|---|
| python:3.11-slim | Standard distro licenses (e.g. bash GPL-3, glibc LGPL-2.1) | Operating-system layer of the Docker container; imposes no obligations on this project's code. |

## Related projects

- **MeshCore** — the mesh protocol and Python library this bot talks to.
  This project is independent of, and not affiliated with, the MeshCore
  or openHop projects.

## Full license texts

Each package above distributes its complete license text. The MIT and
BSD texts for this project are available in the `LICENSE` file; the
remaining notices ship inside the installed packages themselves.