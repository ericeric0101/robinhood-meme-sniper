from __future__ import annotations

from pathlib import Path
import json
from typing import Any


class JsonStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, payload: Any) -> Path:
        path = self.base_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return path
