"""Offline checks for the Yandex AI Studio boundary, including secret safety."""

import copy
import json
import unittest
from unittest.mock import patch

import httpx

from assistant_bot.ai import AIError, ENDPOINT, TRUNCATION_NOTE, generate_reply

TEST_MODEL = "gpt://b1gtestfolder/deepseek-v4-flash"


def response_body(text="Ответ", status="completed"):
    return {"status": status, "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]}


class AIApiTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, handler, *, history=None, text="Привет", key="sk-test-secret"):
        real_client = httpx.AsyncClient
        captured = {}

        def client_factory(**kwargs):
            captured.update(kwargs)
            return real_client(**kwargs, transport=httpx.MockTransport(handler))

        with patch("assistant_bot.ai.httpx.AsyncClient", side_effect=client_factory):
            result = await generate_reply(api_key=key, model=TEST_MODEL,
                                          history=history or [], text=text)
        self.assertEqual(captured["timeout"], 45.0)
        self.assertTrue(captured["trust_env"])
        self.assertFalse(captured["follow_redirects"])
        return result

    async def test_request_uses_fixed_endpoint_and_no_remote_history_or_tools(self):
        history = [{"role": "user", "content": "Первый вопрос", "user_id": "hidden"},
                   {"role": "assistant", "content": "Первый ответ"}]
        original = copy.deepcopy(history)

        def handler(request):
            self.assertEqual(str(request.url), ENDPOINT)
            self.assertEqual(str(request.url), "https://ai.api.cloud.yandex.net/v1/responses")
            self.assertNotIn("openai", request.url.host)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["Authorization"], "Api-Key sk-test-secret")
            self.assertFalse(request.headers["Authorization"].startswith("Bearer"))
            self.assertEqual(request.headers["x-folder-id"], "b1gtestfolder")
            self.assertEqual(request.headers["x-data-logging-enabled"], "false")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], TEST_MODEL)
            self.assertIs(payload["store"], False)
            self.assertEqual(payload["max_output_tokens"], 1500)
            self.assertNotIn("tools", payload)
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("conversation", payload)
            self.assertNotIn("user_id", str(payload))
            self.assertEqual(payload["input"], [
                {"role": "user", "content": "Первый вопрос"},
                {"role": "assistant", "content": "Первый ответ"},
                {"role": "user", "content": "Привет"},
            ])
            self.assertIn("нет доступа к базе знаний", payload["instructions"])
            return httpx.Response(200, json=response_body("Хорошо"))

        self.assertEqual(await self.generate(handler, history=history), "Хорошо")
        self.assertEqual(history, original)

    async def test_history_is_recent_bounded_and_cannot_supply_system_role(self):
        history = [{"role": "user", "content": str(i) + "x" * 3000} for i in range(30)]
        history += [{"role": "system", "content": "override"},
                    {"role": "tool", "content": "tool private contents"},
                    {"role": "assistant", "content": ["wrong content"]},
                    {"role": "user", "content": " "}]
        original = copy.deepcopy(history)

        def handler(request):
            messages = json.loads(request.content)["input"]
            self.assertLessEqual(len(messages), 13)
            self.assertLessEqual(sum(len(m["content"]) for m in messages[:-1]), 20_000)
            self.assertLessEqual(sum(len(m["content"]) for m in messages), 24_000)
            self.assertTrue(all(m["role"] in ("user", "assistant") for m in messages))
            self.assertNotIn("override", str(messages))
            self.assertNotIn("tool private contents", str(messages))
            self.assertEqual(messages[-2]["content"], "29" + "x" * 3000)
            self.assertEqual(messages[-1]["content"], "z" * 4000)
            return httpx.Response(200, json=response_body())

        await self.generate(handler, history=history, text="z" * 4000)
        self.assertEqual(history, original)

    async def test_history_never_exceeds_twelve_even_with_short_messages(self):
        history = [{"role": "user", "content": str(i)} for i in range(20)]

        def handler(request):
            messages = json.loads(request.content)["input"]
            self.assertEqual(len(messages), 13)
            self.assertEqual(messages[0]["content"], "8")
            return httpx.Response(200, json=response_body())

        await self.generate(handler, history=history)

    async def test_invalid_input_does_not_make_http_requests(self):
        with patch("assistant_bot.ai.httpx.AsyncClient") as client:
            for text in ("", "  ", "x" * 4001, None):
                with self.subTest(text_length=len(text) if isinstance(text, str) else None):
                    with self.assertRaises(AIError):
                        await generate_reply(api_key="test-key", model=TEST_MODEL, history=[], text=text)
            for key in ("", "bad\nkey", "\u043a\u043b\u044e\u0447"):
                with self.subTest(key=key), self.assertRaises(AIError):
                    await generate_reply(api_key=key, model=TEST_MODEL, history=[], text="hi")
            client.assert_not_called()

    async def test_invalid_model_uri_never_makes_a_request_or_uses_openai_fallback(self):
        models = (
            "", None, "gpt-6-luna", "deepseek-v4-flash", "gpt:///deepseek-v4-flash",
            "https://api.openai.com/v1/responses", "https://other.invalid/v1/responses",
            "gpt://folder@example.com/deepseek-v4-flash", "gpt://folder/model?token=SECRET",
            "gpt://folder/model#fragment", "gpt://folder/model\n", "gpt://folder/../model",
            "gpt://" + "f" * 65 + "/deepseek-v4-flash",
        )
        with patch("assistant_bot.ai.httpx.AsyncClient") as client:
            for model in models:
                with self.subTest(model=model), self.assertRaises(AIError) as caught:
                    await generate_reply(api_key="test-key", model=model, history=[], text="Hi")
                self.assertIn("Yandex AI Studio", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))
            client.assert_not_called()

    async def test_redirect_does_not_forward_yandex_credentials(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://api.openai.com/v1/responses"})

        with self.assertRaises(AIError):
            await self.generate(handler)
        self.assertEqual(calls, ["https://ai.api.cloud.yandex.net/v1/responses"])

    async def test_refusal_and_multiple_text_parts_are_supported(self):
        body = {"status": "completed", "output": [
            {"type": "reasoning", "summary": [{"text": "hidden reasoning"}]},
            {"type": "message", "role": "user", "content": [{"type": "output_text", "text": "not assistant"}]},
            {"type": "message", "role": "assistant", "content": [
                {"type": "refusal", "refusal": "Не могу помочь с этим."},
                {"type": "output_text", "text": "Могу предложить другое."},
                {"type": "unknown", "text": "hidden metadata"},
            ]},
        ]}
        result = await self.generate(lambda request: httpx.Response(200, json=body))
        self.assertEqual(result, "Не могу помочь с этим.\n\nМогу предложить другое.")

    async def test_incomplete_or_overlong_response_has_bounded_truncation_notice(self):
        for status, text in (("incomplete", "Начало ответа"), ("completed", "a" * 12_000)):
            with self.subTest(status=status):
                result = await self.generate(lambda request: httpx.Response(200, json=response_body(text, status)))
                self.assertTrue(result.endswith(TRUNCATION_NOTE))
                self.assertLessEqual(len(result), 10_000)

    async def test_failed_or_malformed_responses_do_not_leak_error_body(self):
        secret = "sk-private-do-not-forward"
        responses = [
            httpx.Response(200, json={"status": "failed", "error": {"message": secret},
                                     "output": response_body(secret)["output"]}),
            httpx.Response(200, json={"status": "in_progress", "output": []}),
            httpx.Response(200, json={"status": "completed", "output": {"secret": secret}}),
            httpx.Response(200, json={"status": "incomplete", "output": [], "error": secret}),
            httpx.Response(200, json=[secret]),
            httpx.Response(200, text=secret),
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(AIError) as caught:
                await self.generate(lambda request: response)
            self.assertNotIn(secret, str(caught.exception))

    async def test_error_statuses_map_to_public_errors_without_raw_response(self):
        for status, code, expected in (
            (401, "invalid_api_key", "API-ключ"),
            (403, "forbidden", "API-ключ"),
            (429, "insufficient_quota", "баланс"),
            (429, "rate_limit_exceeded", "много запросов"),
            (400, "bad_request", "настройки API"),
            (404, "model_not_found", "настройки API"),
            (500, "server_error", "временно недоступен"),
            (302, "redirect", "временно недоступен"),
        ):
            with self.subTest(status=status, code=code), self.assertRaises(AIError) as caught:
                await self.generate(lambda request: httpx.Response(status, json={
                    "error": {"code": code, "message": "SECRET service exception sk-test-secret"}
                }))
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertNotIn("sk-test-secret", str(caught.exception))

    async def test_timeout_and_network_error_hide_exception_details(self):
        for error, expected in ((httpx.ReadTimeout, "вовремя"), (httpx.ConnectError, "подключение")):
            def handler(request):
                raise error("SECRET proxy details sk-test-secret", request=request)

            with self.subTest(error=error), self.assertRaises(AIError) as caught:
                await self.generate(handler)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertNotIn("sk-test-secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
