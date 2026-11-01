"""Android bridge for the NOCTURNE Flask application.

The server binds only to 127.0.0.1. The database is stored in the app's private
files directory, not next to the read-only Python source bundled in the APK.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.request import urlopen

_HOST = "127.0.0.1"
_PORT = 8765
_server = None
_thread = None
_lock = threading.Lock()


def start_server(data_dir: str) -> str:
    """Start Flask once and wait until the local home route responds."""
    global _server, _thread

    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    os.environ["NOCTURNE_DB"] = str(data_path / "nocturne.db")

    with _lock:
        if _server is None:
            app_module = importlib.import_module("otakudesu_gothic")
            from flask import make_response
            from werkzeug.serving import make_server

            # Content-Disposition lets Android DownloadManager save backups. The
            # standard NOCTURNE export API remains JSON for normal browser clients.
            if "android_export_download" not in app_module.app.view_functions:
                @app_module.app.get("/api/export/download")
                def android_export_download():
                    response = make_response(app_module.api_export())
                    response.headers["Content-Disposition"] = 'attachment; filename="nocturne-backup.json"'
                    response.headers["Content-Type"] = "application/json; charset=utf-8"
                    return response

            _server = make_server(_HOST, _PORT, app_module.app, threaded=True)
            _thread = threading.Thread(
                target=_server.serve_forever,
                name="nocturne-flask",
                daemon=True,
            )
            _thread.start()

    last_error = "server belum merespons"
    for _ in range(60):
        try:
            with urlopen(f"http://{_HOST}:{_PORT}/", timeout=1.5) as response:
                if 200 <= response.status < 500:
                    return json.dumps({
                        "ok": True,
                        "url": f"http://{_HOST}:{_PORT}",
                        "database": str(data_path / "nocturne.db"),
                    })
        except Exception as exc:  # Android startup can take a few seconds.
            last_error = str(exc)
        time.sleep(0.25)

    return json.dumps({"ok": False, "error": last_error})


def stop_server() -> None:
    """Stop the local server when explicitly requested."""
    global _server, _thread
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
            _thread = None
