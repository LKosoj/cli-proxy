import time
from typing import Dict


class Metrics:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self.last_reset = time.time()
        self.last_output_chars = 0
        self.last_output_ts = None

    def inc(self, key: str, count: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + count

    def observe_output(self, chars: int) -> None:
        self.last_output_chars = chars
        self.last_output_ts = time.time()
        self.inc("outputs")

    def snapshot(self) -> str:
        uptime = int(time.time() - self.last_reset)
        parts = [
            f"Uptime: {uptime}s",
            f"Messages: {self.counters.get('messages', 0)}",
            f"Commands: {self.counters.get('commands', 0)}",
            f"Outputs: {self.counters.get('outputs', 0)}",
            f"Errors: {self.counters.get('errors', 0)}",
            f"Queueed: {self.counters.get('queued', 0)}",
        ]
        if self.last_output_ts:
            ago = int(time.time() - self.last_output_ts)
            parts.append(f"Last output: {ago}s ago ({self.last_output_chars} chars)")
        return "\n".join(parts)
