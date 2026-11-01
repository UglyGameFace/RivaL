"""Standard-library-only Python entry point for the private RivaL LLM service.

Discloud detects Python via main.py plus requirements.txt. This launcher
restores the binary executable permission if ZIP extraction stripped it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from service import launch_server


def main() -> None:
    binary = Path(os.getenv("RIVAL_LLAMA_SERVER_BIN", "runtime/bin/llama-server"))
    if binary.is_file() and not os.access(binary, os.X_OK):
        binary.chmod(binary.stat().st_mode | 0o100)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    launch_server()


if __name__ == "__main__":
    main()
