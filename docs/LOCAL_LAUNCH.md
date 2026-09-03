# Local launch

This project serves user-owned extracted files from a local Python HTTP server.
The browser never receives a filesystem path; paths are used only by the
server process.

Create or refresh the ignored config from the repository root:

~~~powershell
py -3 tools\setup_viewer.py --stadium1-rom "C:\path\to\pokemon-stadium-1.z64" --stadium2-rom "C:\path\to\pokemon-stadium-2.z64"
~~~

The helper writes viewer.local.json, references the Stadium 1 ROM directly, and stores the Stadium 2 extraction
cache outside the repository. Use --cache-dir "C:\path\outside\this\repo" to
choose another external cache location. Use --force to refresh an existing
cache.

Start both providers:

~~~powershell
py -3 tools\stadium1_viewer.py --config viewer.local.json --dual --port 8767 --open
~~~

Or double-click start_viewer.bat. Stop the server with Ctrl+C; the
repository's stop_viewer.bat is a convenience helper for Windows.
