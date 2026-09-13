"""Terminal rendering and cancellable, non-echoing input."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from typing import Any, TextIO

from app.runtime.safety import Sanitizer


class InteractionRequired(Exception):
    reported = False


class SafeFormatter(logging.Formatter):
    def __init__(self, sanitizer: Sanitizer) -> None:
        super().__init__("%(levelname)s: %(message)s")
        self.sanitizer = sanitizer

    def format(self, record: logging.LogRecord) -> str:
        return self.sanitizer.text(super().format(record))


class Console:
    def __init__(
        self,
        sanitizer: Sanitizer,
        *,
        json_output: bool = False,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.sanitizer, self.json_output = sanitizer, json_output
        self.stdin, self.stdout, self.stderr = (
            stdin or sys.stdin,
            stdout or sys.stdout,
            stderr or sys.stderr,
        )

    @property
    def interactive(self) -> bool:
        return self.stdin.isatty()

    def say(self, message: str) -> None:
        print(
            self.sanitizer.text(message),
            file=self.stderr if self.json_output else self.stdout,
            flush=True,
        )

    def result(self, value: Any) -> None:
        print(
            json.dumps(self.sanitizer.clean(value), ensure_ascii=False),
            file=self.stdout,
            flush=True,
        )

    def event(self, event: dict[str, Any]) -> None:
        if self.json_output:
            self.result(event)
            return
        kind, data = event["type"], event["data"]
        if kind == "run_started":
            self.say(f"Run ID: {data['run_id']}")
        elif kind in {"planning", "replanning"}:
            self.say("Planning…" if kind == "planning" else "Replanning…")
        elif kind == "step_result":
            self.say(f"{data.get('status', '')}: {data.get('result_summary', '')}")
            result = data.get("result") or {}
            if result.get("data"):
                self.result(result["data"])
        elif kind in {"error", "interaction_required"}:
            self.say(f"Error: {data.get('message', 'Run failed')}")
        elif kind == "done":
            self.say(f"Run finished: {data.get('status', 'unknown')}")

    def action(self, data: dict[str, Any]) -> None:
        step = data.get("step") or {}
        self.say(
            f"\nProposed action: {step.get('action_type', 'unknown')} / {step.get('target_system', '')}"
        )
        self.say(f"Target: {step.get('target', '')}")
        self.say(f"Risk: {data.get('risk_level', 'unknown')}")
        self.say(
            json.dumps(
                self.sanitizer.clean(step.get("payload") or {}), indent=2, ensure_ascii=False
            )
        )
        for reason in data.get("reasons") or []:
            self.say(f"Reason: {reason}")

    async def read(self, label: str, *, hidden: bool = False, default: str | None = None) -> str:
        if not self.interactive:
            raise InteractionRequired("A terminal is required for input")
        prompt = label + (f" [{default}]" if default is not None else "") + ": "
        print(self.sanitizer.text(prompt), end="", file=self.stderr, flush=True)
        if sys.platform == "win32":
            value = await self._read_windows(hidden)
        else:
            value = await self._read_posix(hidden)
        if value == "" and default is not None:
            return default
        return value

    async def _read_posix(self, hidden: bool) -> str:
        import termios

        descriptor = self.stdin.fileno()
        original = termios.tcgetattr(descriptor)
        if hidden:
            attributes = termios.tcgetattr(descriptor)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def readable() -> None:
            if not ready.done():
                ready.set_result(None)

        try:
            loop.add_reader(descriptor, readable)
            await ready
            line = self.stdin.readline()
            if not line:
                raise InteractionRequired("Input ended before a response was received")
            return line.rstrip("\r\n")
        finally:
            loop.remove_reader(descriptor)
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
            if hidden:
                print(file=self.stderr, flush=True)

    async def _read_windows(self, hidden: bool) -> str:
        msvcrt = importlib.import_module("msvcrt")

        characters: list[str] = []
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.03)
                continue
            character = msvcrt.getwch()
            if character == "\x03":
                raise KeyboardInterrupt
            if character == "\x04":
                raise InteractionRequired("Input ended before a response was received")
            if character in ("\r", "\n"):
                print(file=self.stderr, flush=True)
                return "".join(characters)
            if character == "\b":
                if characters:
                    characters.pop()
                    if not hidden:
                        print("\b \b", end="", file=self.stderr, flush=True)
            elif character in ("\x00", "\xe0"):
                msvcrt.getwch()
            else:
                characters.append(character)
                if not hidden:
                    print(character, end="", file=self.stderr, flush=True)
