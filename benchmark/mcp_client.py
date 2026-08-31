# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Bounded stdio client shared by integration tests and the MCP benchmark."""

import json
import subprocess
from collections import deque
from queue import Empty, Queue
from threading import Thread


class MCPClient:
    def __init__(self, command, cwd, *, env=None, timeout=15):
        self.timeout = timeout
        self.version = None
        self._id = 0
        self.stderr = deque(maxlen=200)
        self.responses = Queue()
        self._proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._readers = [
            Thread(target=self._read_stdout, daemon=True),
            Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError as exc:
                    self.responses.put(exc)
        finally:
            self.responses.put(EOFError("MCP stdout closed"))

    def _read_stderr(self):
        self.stderr.extend(self._proc.stderr)

    def write_raw(self, line):
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

    def receive(self, timeout=None):
        try:
            response = self.responses.get(
                timeout=self.timeout if timeout is None else timeout
            )
        except Empty as exc:
            raise TimeoutError(
                "MCP response timed out: " + "".join(self.stderr)
            ) from exc
        if isinstance(response, Exception):
            raise AssertionError(  # noqa: TRY004 — integration assertion, not a caller type error
                f"Invalid MCP stream: {response}; {''.join(self.stderr)}"
            )
        return response

    def request(self, method, params=None, *, request_id=None):
        self._id += 1
        request_id = self._id if request_id is None else request_id
        params = dict(params or {})
        if self.version:
            params.setdefault(
                "_meta",
                {
                    "io.modelcontextprotocol/protocolVersion": self.version,
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "blastradius-tests",
                        "version": "1",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            )
        self.write_raw(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                ensure_ascii=False,
            )
        )
        return request_id

    def send(self, method, params=None):
        request_id = self.request(method, params)
        response = self.receive()
        assert response.get("id") == request_id, response
        return response

    def notify(self, method, params=None):
        self.write_raw(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        )

    def initialize(self, version="2024-11-05"):
        response = self.send(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "blastradius-tests", "version": "1"},
            },
        )
        assert response["result"]["protocolVersion"] == version
        self.notify("notifications/initialized")
        return response["result"]

    def call_tool(self, name, arguments):
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if not self._proc.stdin.closed:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        for reader in self._readers:
            reader.join(timeout=1)
        self._proc.stdout.close()
        self._proc.stderr.close()
        return self._proc.returncode

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
