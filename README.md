# Pokémon Stadium Model Viewer

A small, dependency-free local viewer for extracted Pokémon Stadium model and
animation resources. It provides a Python HTTP server and a vanilla WebGL
frontend for inspecting Stadium 1 and Stadium 2 models in a browser.

The repository contains viewer code and synthetic test fixtures only. ROMs,
extracted game data, local paths, screenshots, and caches stay outside Git.

## Requirements

- Python 3.9 or newer
- A modern browser with WebGL support
- Node.js 18 or newer for the JavaScript checks only
- A legally obtained ROM or an existing extraction
- Stadium 1 can read a user-owned .z64/.n64/.v64/.rom image directly, or an extracted pokemon_models directory
- Stadium 2 can be extracted directly from a user-owned ROM

No Python packages or frontend build step are required.

## Quick start

Run these commands from the repository root.

### 1. Configure external assets

For both providers:

~~~powershell
py -3 tools\setup_viewer.py --stadium1-rom "C:\path\to\pokemon-stadium-1.z64" --stadium2-rom "C:\path\to\pokemon-stadium-2.z64"
~~~

This does the following:

- Reads the source-defined Stadium 1 model archive directly from the ROM without copying it.
- Reads the Stadium 2 ROM and writes decoded model/pose data to the operating
  system's application cache.
- Creates the ignored viewer.local.json.
- Never copies a ROM or extracted cache into this repository.

To use an existing Stadium 2 cache:

~~~powershell
py -3 tools\setup_viewer.py --stadium1-rom "C:\path\to\pokemon-stadium-1.z64" --stadium2-cache "C:\path\outside\this\repo\stadium2"
~~~

To choose a different extraction location, add --cache-dir to the ROM
command. The cache directory must be outside this Git repository.

The example file [viewer.local.example.json](viewer.local.example.json) shows
the supported config shape. It contains placeholders only. Do not replace it
with personal paths and commit it; use the ignored viewer.local.json.

### 2. Start the viewer

~~~powershell
py -3 tools\stadium1_viewer.py --config viewer.local.json --dual --port 8767 --open
~~~

On Windows, you can double-click [start_viewer.bat](start_viewer.bat) after
creating the config. Open http://127.0.0.1:8767/ manually if the browser
does not open.

For one provider:

~~~powershell
py -3 tools\stadium1_viewer.py --config viewer.local.json --provider stadium1 --open
py -3 tools\stadium1_viewer.py --config viewer.local.json --provider stadium2 --open
~~~

Explicit --assets paths are also supported and override the config for a
single-provider launch.

For a one-off dual launch without writing a config file, pass both ROMs or
external asset roots directly:

~~~powershell
py -3 tools\stadium1_viewer.py --dual --stadium1-rom "C:\path\to\pokemon-stadium-1.z64" --stadium2-rom "C:\path\to\pokemon-stadium-2.z64" --open
~~~

Stop the server with Ctrl+C. On Windows, stop_viewer.bat is a convenience
helper for the default port.

## How files are referenced

The browser never reads a ROM or opens a filesystem path. The Python server
reads the configured external root and exposes only normalized model JSON and
the bundled static frontend.

~~~text
user-owned ROM
      |
      v
tools/setup_viewer.py
      |
      +--> ignored viewer.local.json ---> local viewer server ---> browser
      |
      +--> external Stadium 2 cache
~~~

viewer.local.json stores local provider paths and is ignored by Git. Relative
paths are resolved from the config file's directory; environment variables and
user-home expansion are supported.

The default bind address is 127.0.0.1. The /api/health response reports
provider/model status but intentionally omits absolute asset paths. Keep the
server on loopback unless LAN access is deliberate.

## Privacy and publishing checklist

Before committing or publishing, review:

~~~powershell
git status --short
git diff --check
git diff --cached --stat
git ls-files
~~~

Never add:

- ROMs or disc images
- extracted model, pose, texture, or archive files
- viewer.local.json
- screenshots, captures, logs, caches, or build output
- credentials, tokens, private checkout paths, or machine-specific notes

The .gitignore covers common ROM and extraction extensions, but ignored files
can still be force-added. Always inspect the staged file list.

The included setup helper refuses to place a new Stadium 2 cache inside this
repository. It does not upload files or modify a remote repository.

## What is supported

The viewer follows this extracted-resource path:

~~~text
BinArchive -> PERS-SZP/Yay0 -> FRAGMENT -> model root -> GeoLayout -> F3DEX2
~~~

It supports the common Stadium 1 model structures needed by the extracted
Pokémon model set, including geometry, textures, graph hierarchy, transform
curves, and expression tracks. Stadium 2 uses a separate indexed model/pose
provider and its extracted manifest.

Unknown or unsupported structures are reported as resource diagnostics where
possible. This is an inspection tool, not a full emulator, ROM loader, or
complete replacement for the game's N64 renderer. Direct Stadium 1 ROM access
currently targets the source-defined US model archive; use an extracted
pokemon_models directory when working with a different build or region.

## Controls

- Search models by file, Pokédex ID, name, or provider alias (s1/s2).
- Select animations, play/pause, loop, step frames, scrub, and change speed.
- Drag to orbit, Shift-drag or right-drag to pan, and use the wheel to zoom.
- Toggle textures, lighting, wireframe, axes, bounds, skeleton, and bone names.
- In dual mode, each provider has its own tab and selection state.

## Project layout

~~~text
tools/stadium1_viewer.py              server and resource providers
tools/stadium1_viewer/                browser client and capture helpers
tools/stadium2_extract.py             external Stadium 2 cache extractor
tools/setup_viewer.py                 local config and extraction workflow
tools/test_stadium1_viewer.py         parser and provider tests
tools/test_viewer_config.py           config/privacy tests
docs/                                  launch, format, and validation notes
~~~

## Validation

~~~powershell
node tools\test_stadium1_viewer_bone_math.js
py -3 -m unittest discover -s tools -p "test_*.py"
node --check tools\stadium1_viewer\viewer.js
node --check tools\stadium1_viewer\bone_math.js
~~~

Live rendering validation requires external assets. Keep generated captures
outside the repository.

## Legal note

This project does not distribute ROMs or extracted game assets. You are
responsible for obtaining and using source files lawfully and for complying
with the applicable laws and licenses in your region.
