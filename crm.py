#!/usr/bin/env python3
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    source_dir = root / "crm_app_source"
    parts = sorted(source_dir.glob("part_*"))
    if not parts:
        raise SystemExit("Missing crm_app_source parts. Re-download the full app folder from GitHub.")
    source = "".join(part.read_text(encoding="utf-8") for part in parts)
    globals_dict = {
        "__name__": "__main__",
        "__file__": str(source_dir / "combined_crm.py"),
    }
    exec(compile(source, globals_dict["__file__"], "exec"), globals_dict)


if __name__ == "__main__":
    main()
