from time import perf_counter


class LatencyTracker:
    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self._values: dict[str, int] = {}
        self._total_start = perf_counter()

    def start(self, name: str) -> None:
        self._starts[name] = perf_counter()

    def stop(self, name: str) -> int:
        elapsed = int((perf_counter() - self._starts.pop(name, perf_counter())) * 1000)
        self._values[f"{name}_ms"] = elapsed
        return elapsed

    def values(self) -> dict[str, int]:
        return {**self._values, "total_ms": int((perf_counter() - self._total_start) * 1000)}
