# -*- coding: utf-8 -*-
"""
test_agent_loop.py
==================
Mocked smoke tests for sap_migration_agent.py -- no network, no API key.

Covers the REPL mechanics that are easy to break silently:
  - submit_user_turn() restoring message-history shape on every recoverable
    API error (unanswered user turn, or a turn that died mid-tool-loop leaving
    a trailing tool_result) -- roles must stay strictly alternating
  - the tool loop itself: a tool_use response is executed and its result fed
    back, then a text response ends the turn
  - save_mapping_decision writing a valid CSV (header on first write only)
  - execute_tool converting exceptions into an error JSON payload instead of
    crashing the loop

Run: python -m unittest discover -s tests   (from etl/)
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import anthropic

ETL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ETL_DIR, "agent")
for p in (ETL_DIR, AGENT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

agent = importlib.import_module("sap_migration_agent")


class FakeContent(list):
    """Mimics the SDK's content block: a list that also has .type/.text/.id/.name/.input."""

    def __init__(self, items, **attrs):
        super().__init__(items)
        for k, v in attrs.items():
            setattr(self, k, v)


def text_response(text):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[FakeContent([text], type="text", text=text)],
    )


def tool_response(name, tool_input, tool_use_id="tu_1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[FakeContent([name], type="tool_use", id=tool_use_id,
                             name=name, input=tool_input)],
    )


class FakeMessages:
    """Returns the queued responses in order, one per API call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("no queued fake responses left")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def fake_http_response(status_code, headers=None):
    import httpx
    return httpx.Response(status_code, headers=headers or {},
                          request=httpx.Request("POST", "https://api.anthropic.com"))


def connection_error():
    # anthropic.APIConnectionError needs a request kwarg
    import httpx
    return anthropic.APIConnectionError(
        message="Connection error.", request=httpx.Request("POST", "https://api.anthropic.com"))


class TestSubmitUserTurn(unittest.TestCase):
    def test_success_appends_user_and_assistant(self):
        client = FakeClient([text_response("hello back")])
        messages = []
        reply = agent.submit_user_turn(client, messages, "hello")
        self.assertEqual(reply, "hello back")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])

    def test_unanswered_user_turn_rolled_back(self):
        client = FakeClient([connection_error(), text_response("ok")])
        messages = []
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(agent.submit_user_turn(client, messages, "q1"))
            self.assertEqual(agent.submit_user_turn(client, messages, "q1 retry"), "ok")
        # The failed turn left nothing behind: exactly one clean user/assistant pair
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "q1 retry")

    def test_mid_tool_loop_failure_rolled_back(self):
        # First call: model asks for a tool; second call (after the tool result
        # is fed back) blows up -- the whole turn must disappear, including the
        # trailing tool_result message.
        client = FakeClient([tool_response("get_source_sample", {"sheet": "Employees"}),
                             connection_error(),
                             text_response("ok")])
        messages = []
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(agent.submit_user_turn(client, messages, "q1"))
            self.assertEqual(agent.submit_user_turn(client, messages, "q2"), "ok")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])

    def test_rate_limit_rolled_back(self):
        e = anthropic.RateLimitError(
            message="rate limited",
            response=fake_http_response(429, {"retry-after": "3"}),
            body=None,
        )
        client = FakeClient([e])
        messages = []
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(agent.submit_user_turn(client, messages, "q"))
        self.assertEqual(messages, [])

    def test_auth_error_propagates(self):
        e = anthropic.AuthenticationError(
            message="bad key", response=fake_http_response(401), body=None)
        client = FakeClient([e])
        with self.assertRaises(anthropic.AuthenticationError):
            with redirect_stdout(io.StringIO()):
                agent.submit_user_turn(client, [], "q")


class TestToolLoop(unittest.TestCase):
    def test_tool_use_executed_and_result_fed_back(self):
        client = FakeClient([
            tool_response("search_sap_tables", {"query": "vendor"}),
            text_response("Found it"),
        ])
        messages = []
        with redirect_stdout(io.StringIO()):
            reply = agent.submit_user_turn(client, messages, "find vendor tables")
        self.assertEqual(reply, "Found it")
        # user, assistant(tool_use), user(tool_result), assistant(text)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        tool_result = messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        payload = json.loads(tool_result["content"])
        self.assertIn("matches", payload)


class TestSaveMappingDecision(unittest.TestCase):
    def test_writes_header_once_and_row(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "decisions.csv")
            orig = agent.DECISIONS_CSV
            agent.DECISIONS_CSV = csv_path
            try:
                r1 = json.loads(agent.tool_save_mapping_decision(
                    source_sheet="Customers", source_column="Country",
                    sap_target_table="KNA1", sap_field="LAND1",
                    transform="lookup_passthrough", confidence="APPROX",
                    rationale="Needs target-system customizing"))
                r2 = json.loads(agent.tool_save_mapping_decision(
                    source_sheet="Customers", source_column="City",
                    sap_target_table="ADRC", sap_field="CITY1",
                    transform="direct", confidence="HIGH",
                    rationale="1:1"))
                self.assertEqual(r1["status"], "saved")
                self.assertEqual(r2["status"], "saved")
                with open(csv_path, encoding="utf-8-sig") as f:
                    lines = [l for l in f.read().splitlines() if l]
            finally:
                agent.DECISIONS_CSV = orig
        self.assertEqual(len(lines), 3)  # header + 2 rows, header only once
        self.assertIn("source_column", lines[0])
        self.assertIn("Customers", lines[1])


class TestExecuteTool(unittest.TestCase):
    def test_exception_becomes_error_json_not_crash(self):
        # Sheet that doesn't exist -> tool returns an error payload; and a
        # genuinely broken call still can't crash the loop.
        out = json.loads(agent.execute_tool("get_source_sample", {"sheet": "No Such Sheet"}))
        self.assertIn("error", out)
        out = json.loads(agent.execute_tool("preview_transform", {"sheet": "No Such Sheet",
                                                                  "target_table": "X", "fields": []}))
        self.assertIn("error", out)

    def test_unknown_tool(self):
        out = json.loads(agent.execute_tool("does_not_exist", {}))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
