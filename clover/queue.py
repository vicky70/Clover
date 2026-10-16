"""Offline queue for verify results. Sent in order once the server is reachable.

A queued run only counts in-window if the server *receives* it before close + grace.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


class Queue:
    def __init__(self, path: Path):
        self.path = Path(path)

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
        tmp.replace(self.path)

    def push(self, item: dict) -> None:
        self._write(self.items() + [item])

    def flush(self, send: Callable[[dict], object], offline: type[Exception]) -> tuple[list, int]:
        """Send queued items in order; stop at the first `offline` error.

        Returns (responses for sent items, number still queued). Items the server
        rejects with a non-offline error are dropped and returned as the exception.
        """
        items = self.items()
        results = []
        while items:
            try:
                results.append(send(items[0]))
            except offline:
                break
            except Exception as e:  # server rejected it (e.g. 409 window closed): don't retry forever
                results.append(e)
            items.pop(0)
            self._write(items)
        return results, len(items)
