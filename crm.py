#!/usr/bin/env python3
import base64
import gzip
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    source_dir = root / "crm_app_source"
    payload = source_dir / "app_source.py.gz.b64"
    if not payload.exists():
        raise SystemExit("Missing crm_app_source/app_source.py.gz.b64. Re-download the full app folder from GitHub.")
    source = gzip.decompress(base64.b64decode(payload.read_text(encoding="utf-8"))).decode("utf-8")
    globals_dict = {
        "__name__": "__main__",
        "__file__": str(source_dir / "combined_crm.py"),
    }
    exec(compile(source, globals_dict["__file__"], "exec"), globals_dict)


if __name__ == "__main__":
    main()
