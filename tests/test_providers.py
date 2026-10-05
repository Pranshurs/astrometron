import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from astrometron.providers import Message, OpenAICompatProvider, ProviderError, Request
from astrometron.runner import run_eval
from astrometron.store import Store
from astrometron.taxonomy import ErrorKind
from conftest import make_config

BEHAVIOUR: dict[str, Any] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        BEHAVIOUR["last_auth"] = self.headers.get("Authorization")
        BEHAVIOUR["last_body"] = body
        mode = BEHAVIOUR.get("mode", "ok")
        if mode == "ok":
            payload = {
                "model": BEHAVIOUR.get("served", body["model"]),
                "choices": [{"message": {"content": "answer 1"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
            self._send(200, json.dumps(payload))
        elif mode == "tools":
            msgs = body["messages"]
            if msgs[-1]["content"].startswith("TOOL RESULT"):
                content = msgs[-1]["content"].split(": ", 1)[1]
                payload = {"choices": [{"message": {"content": content}}]}
            else:
                payload = {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": json.dumps({"key": "1"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            self._send(200, json.dumps(payload))
        elif mode == "garbage":
            self._send(200, "not json")
        elif mode == "filter":
            self._send(
                200,
                json.dumps(
                    {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}
                ),
            )
        else:
            code, text = mode
            self._send(code, text)

    def _send(self, code: int, text: str) -> None:
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


REQ = Request(messages=(Message("user", "hi"),), params={"temperature": 0}, seed=3)


def provider(url: str, **kw: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider({"base_url": url, "model": "local-model", **kw})


def test_success_and_usage(server: str) -> None:
    BEHAVIOUR.update(mode="ok")
    resp = provider(server).complete(REQ)
    assert resp.text == "answer 1" and resp.usage.input_tokens == 10
    assert BEHAVIOUR["last_body"]["seed"] == 3 and BEHAVIOUR["last_body"]["temperature"] == 0


@pytest.mark.parametrize(
    "mode,kind",
    [
        ((503, "overloaded"), ErrorKind.INFRA),
        ((429, "rate limited"), ErrorKind.INFRA),
        ((401, "bad key"), ErrorKind.INFRA),
        ((400, '{"error": "context_length_exceeded"}'), ErrorKind.MODEL),
        ("garbage", ErrorKind.INFRA),
        ("filter", ErrorKind.MODEL),
    ],
)
def test_error_mapping(server: str, mode: Any, kind: ErrorKind) -> None:
    BEHAVIOUR.update(mode=mode)
    with pytest.raises(ProviderError) as ei:
        provider(server).complete(REQ)
    assert ei.value.kind is kind


def test_connection_refused_is_infra() -> None:
    with pytest.raises(ProviderError) as ei:
        provider("http://127.0.0.1:9/v1", request_timeout_s=2).complete(REQ)
    assert ei.value.kind is ErrorKind.INFRA


def test_api_key_from_env_only(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    BEHAVIOUR.update(mode="ok")
    monkeypatch.setenv("TEST_KEY", "sk-FAKE-synthetic-test")
    provider(server, api_key_env="TEST_KEY").complete(REQ)
    assert BEHAVIOUR["last_auth"] == "Bearer sk-FAKE-synthetic-test"


def test_unpinned_by_default(server: str) -> None:
    assert not provider(server).identity().pinned
    assert not provider(server, pinned=True).identity().pinned  # no revision -> not pinned
    assert provider(server, pinned=True, revision="sha256:gguf").identity().pinned


def test_run_through_local_server(server: str, store: Store, data_file: Path) -> None:
    BEHAVIOUR.update(mode="ok", served="local-model")
    r = run_eval(
        make_config(
            data_file,
            provider={"kind": "openai-compat", "base_url": server, "model": "local-model"},
        ),
        store,
    )
    assert r["aggregate"]["counts"]["PASS"] == 1  # only c01 expects "answer 1"
    assert any("not pinned" in w for w in r["warnings"])
    assert "sk-" not in json.dumps(r)


def test_tool_loop_openai_tool_calls(server: str, store: Store, data_file: Path) -> None:
    BEHAVIOUR.update(mode="tools")
    r = run_eval(
        make_config(
            data_file,
            provider={"kind": "openai-compat", "base_url": server, "model": "m"},
            solver="tool_loop",
            tools=[{"name": "lookup", "version": "1", "function": "helpers_mod:lookup_tool"}],
        ),
        store,
    )
    case = next(c for c in r["cases"] if c["case_id"] == "c01")
    assert case["outcome"] == "PASS"
    assert [s["type"] for s in case["trajectory"]] == ["model_call", "tool_call", "model_call"]
