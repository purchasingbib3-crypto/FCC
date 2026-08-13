from __future__ import annotations

import importlib
import sys
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    backend_dir = Path(__file__).resolve().parent
    bundle_root = backend_dir.parent
    package_name = backend_dir.name
    if str(bundle_root) not in sys.path:
        sys.path.insert(0, str(bundle_root))

    config = importlib.import_module(f"{package_name}.config")
    settings = config.get_settings()
    uvicorn.run(
        f"{package_name}.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
