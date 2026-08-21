#!/usr/bin/env python3
"""Behavior tests for bounded external review recovery and diagnostics."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).with_name("review_batch.py")
SPEC = importlib.util.spec_from_file_location("review_batch", MODULE_PATH)
assert SPEC and SPEC.loader
BATCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BATCH
SPEC.loader.exec_module(BATCH)


VALID_REPORT = json.dumps(
    {
        "verdict": "needs_changes",
        "findings": [
            {
                "id": "R1",
                "severity": "P1",
                "claim": "A required boundary is missing.",
                "evidence": ["packet section 2"],
                "impact": "Acceptance can fail.",
                "minimal_change": "Add the boundary and its test.",
            }
        ],
        "missing_evidence": [],
        "confidence": "high",
    }
)


def response(content: Any, *, finish_reason: str = "stop", reasoning: str = "", parsed: Any = None) -> dict[str, Any]:
    message = {
        "role": "assistant",
        "content": content,
        "reasoning_content": reasoning,
    }
    if parsed is not None:
        message["parsed"] = parsed
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    }


class FakeModule:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.payloads: list[dict[str, Any]] = []

    def request_json(self, _base, _key, _timeout, _method, _path, payload):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def run(
    module: FakeModule,
    *,
    fallback: str | None = "kimi-k3",
    retry_limit: int = 1,
    model: str = "kimi-k3-256k",
    role: str = "coverage",
):
    return BATCH.run_one(
        module,
        ("https://example.invalid/v1", "secret", 5.0),
        "plan",
        role,
        model,
        fallback,
        "frozen packet",
        3500,
        6000,
        retry_limit,
    )


class ReviewBatchTests(unittest.TestCase):
    def test_thinking_primary_call_uses_json_mode_low_effort_and_token_floor(self):
        module = FakeModule([response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        payload = module.payloads[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertGreaterEqual(payload["max_tokens"], BATCH.THINKING_OUTPUT_FLOOR)
        self.assertTrue(result["attempt_log"][0]["json_mode"])
        self.assertEqual(result["attempt_log"][0]["reasoning_effort"], "low")

    def test_non_thinking_model_gets_json_mode_without_reasoning_effort(self):
        module = FakeModule([response(VALID_REPORT)])

        result = run(module, model="glm-5.2", role="implementation", fallback="deepseek-v4-flash")

        self.assertTrue(result["ok"])
        payload = module.payloads[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_tokens"], 3500)

    def test_grok_uses_json_mode_without_reasoning_effort(self):
        module = FakeModule([response(VALID_REPORT)])

        result = run(module, model="grok-4.6", role="adversarial", fallback="glm-5.2")

        self.assertTrue(result["ok"])
        payload = module.payloads[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", payload)
        self.assertGreaterEqual(payload["max_tokens"], BATCH.THINKING_OUTPUT_FLOOR)
        self.assertIsNone(result["attempt_log"][0]["reasoning_effort"])

    def test_deepseek_reasoning_field_counts_as_reasoning_output(self):
        payload = response("")
        payload["choices"][0]["message"].pop("reasoning_content", None)
        payload["choices"][0]["message"]["reasoning"] = "private reasoning"
        module = FakeModule([payload, response(VALID_REPORT)])

        result = run(module, model="deepseek-v4-flash", role="implementation", fallback="glm-5.2")

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "empty_final_content")
        self.assertTrue(result["attempt_log"][0]["reasoning_present"])
        self.assertEqual(result["attempt_log"][0]["retry_action"], "same_model_output_recovery")
        self.assertEqual(result["models_attempted"], ["deepseek-v4-flash", "deepseek-v4-flash"])

    def test_reasoning_only_output_stays_on_model_with_more_tokens(self):
        module = FakeModule([response("", reasoning="private reasoning"), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3-256k"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "empty_final_content")
        self.assertEqual(result["attempt_log"][0]["reasoning_content_length"], len("private reasoning"))
        self.assertNotIn("reasoning_content", result["attempt_log"][0])
        self.assertGreaterEqual(module.payloads[0]["max_tokens"], BATCH.THINKING_OUTPUT_FLOOR)
        self.assertGreater(module.payloads[1]["max_tokens"], module.payloads[0]["max_tokens"])
        self.assertNotIn("private reasoning", json.dumps(result))

    def test_normal_stop_empty_final_uses_one_bounded_fallback(self):
        module = FakeModule([response(""), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["attempt_log"][0]["retry_action"], "fallback_after_empty_final")

    def test_truncation_is_classified_and_recovers_without_a_third_call(self):
        module = FakeModule([response('{"verdict":', finish_reason="length"), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "output_truncated")
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3-256k"])
        self.assertEqual(len(module.payloads), 2)

    def test_complete_json_is_accepted_even_if_finish_reason_is_length(self):
        module = FakeModule([response(VALID_REPORT, finish_reason="length")])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["recovered"])

    def test_transport_retry_stays_on_primary_model(self):
        module = FakeModule([RuntimeError("connection failed: reset"), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3-256k"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "transport_error")

    def test_authentication_failure_does_not_retry(self):
        module = FakeModule([RuntimeError("HTTP 401: unauthorized"), response(VALID_REPORT)])

        result = run(module)

        self.assertFalse(result["ok"])
        self.assertFalse(result["recovered"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["failure_kind"], "authentication_error")
        self.assertEqual(len(module.payloads), 1)

    def test_invalid_json_uses_fallback_when_available(self):
        module = FakeModule([response("not json"), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["attempt_log"][0]["retry_action"], "fallback_after_format_failure")
        self.assertNotIn("raw_response", result)

    def test_invalid_json_repairs_on_same_model_without_fallback(self):
        module = FakeModule([response("not json"), response("still not json")])

        result = run(module, fallback=None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["failure_kind"], "invalid_json")
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3-256k"])
        self.assertEqual(result["attempt_log"][0]["retry_action"], "same_model_format_repair")

    def test_upstream_error_uses_fallback_without_persisting_error_body(self):
        secret_body = "HTTP 503: upstream body that must not be persisted"
        module = FakeModule([RuntimeError(secret_body), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3"])
        self.assertEqual(result["attempt_log"][0]["http_status"], 503)
        self.assertNotIn(secret_body, json.dumps(result))

    def test_http_404_is_model_unavailable_and_falls_back(self):
        module = FakeModule([RuntimeError("HTTP 404: model not served"), response(VALID_REPORT)])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "model_unavailable")
        self.assertEqual(result["attempt_log"][0]["retry_action"], "fallback_after_upstream_failure")
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3"])

    def test_unsupported_request_strips_extras_on_same_model(self):
        module = FakeModule(
            [
                RuntimeError("HTTP 400: unknown parameter reasoning_effort"),
                response(VALID_REPORT),
            ]
        )

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["models_attempted"], ["kimi-k3-256k", "kimi-k3-256k"])
        self.assertEqual(result["attempt_log"][0]["failure_kind"], "unsupported_request")
        self.assertEqual(result["attempt_log"][0]["retry_action"], "same_model_strip_request_extras")
        self.assertIn("response_format", module.payloads[0])
        self.assertNotIn("response_format", module.payloads[1])
        self.assertNotIn("reasoning_effort", module.payloads[1])

    def test_more_than_five_findings_are_truncated(self):
        report = json.loads(VALID_REPORT)
        report["findings"] = [{**report["findings"][0], "id": f"R{index}"} for index in range(6)]

        parsed = BATCH.parse_report(json.dumps(report))

        self.assertEqual([item["id"] for item in parsed["findings"]], ["R0", "R1", "R2", "R3", "R4"])

    def test_verdict_aliases_and_numeric_confidence_are_accepted(self):
        grok_like = {
            "verdict": "request_changes",
            "findings": json.loads(VALID_REPORT)["findings"],
            "missing_evidence": ["need the live trace"],
            "confidence": 0.84,
        }
        glm_like = {
            "verdict": "conditional_pass_with_blocking_issues",
            "findings": json.loads(VALID_REPORT)["findings"],
            "confidence": "HIGH",
        }
        kimi_like = {
            "verdict": "insufficient_evidence",
            "findings": json.loads(VALID_REPORT)["findings"],
            "missing_evidence": "no hash",
            "confidence": "medium",
        }

        grok = BATCH.parse_report(json.dumps(grok_like))
        glm = BATCH.parse_report(json.dumps(glm_like))
        kimi = BATCH.parse_report(json.dumps(kimi_like))

        self.assertEqual(grok["verdict"], "needs_changes")
        self.assertEqual(grok["confidence"], "high")
        self.assertEqual(glm["verdict"], "needs_changes")
        self.assertEqual(kimi["verdict"], "blocked")
        self.assertEqual(kimi["missing_evidence"], ["no hash"])

    def test_json_embedded_in_prose_is_extracted(self):
        wrapped = "Analysis follows.\n" + VALID_REPORT + "\nThanks."

        parsed = BATCH.parse_report(wrapped)

        self.assertEqual(parsed["verdict"], "needs_changes")
        self.assertEqual(parsed["findings"][0]["id"], "R1")

    def test_structured_reasoning_block_is_not_treated_as_final_content(self):
        content = [
            {"type": "reasoning", "text": "private chain"},
            {"type": "output_text", "text": VALID_REPORT},
        ]

        final = BATCH.extract_text_content(content)

        self.assertEqual(final, VALID_REPORT)
        self.assertNotIn("private chain", final)

    def test_parsed_json_object_field_is_used_as_the_report(self):
        module = FakeModule([response("", parsed=json.loads(VALID_REPORT))])

        result = run(module)

        self.assertTrue(result["ok"])
        self.assertEqual(result["report"]["verdict"], "needs_changes")
        self.assertTrue(result["attempt_log"][0]["parsed_present"])

    def test_unknown_report_fields_are_removed_before_persistence(self):
        report = json.loads(VALID_REPORT)
        report["reasoning"] = "PRIVATE_CHAIN_SHOULD_NOT_PERSIST"
        report["upstream_body"] = "RAW_PROVIDER_BODY_SHOULD_NOT_PERSIST"
        report["findings"][0]["analysis"] = "PRIVATE_FINDING_CHAIN"

        parsed = BATCH.parse_report(json.dumps(report))

        serialized = json.dumps(parsed)
        self.assertEqual(set(parsed), {"verdict", "findings", "missing_evidence", "confidence"})
        self.assertNotIn("PRIVATE_CHAIN_SHOULD_NOT_PERSIST", serialized)
        self.assertNotIn("RAW_PROVIDER_BODY_SHOULD_NOT_PERSIST", serialized)
        self.assertNotIn("PRIVATE_FINDING_CHAIN", serialized)

    def test_http_content_filter_is_non_retryable(self):
        secret_body = "HTTP 400: content_filter provider detail"
        module = FakeModule([RuntimeError(secret_body), response(VALID_REPORT)])

        result = run(module)

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["failure_kind"], "content_filtered")
        self.assertEqual(result["attempt_log"][0]["retry_action"], "stop_non_retryable")
        self.assertNotIn(secret_body, json.dumps(result))
        self.assertEqual(len(module.payloads), 1)

    def test_fallback_does_not_duplicate_another_primary_reviewer(self):
        selected = {"coverage": "kimi-k3-256k", "implementation": "glm-5.2"}
        fallbacks = BATCH.resolve_fallback_models(
            {"kimi-k3-256k", "kimi-k3", "kimi-k2.7-code", "glm-5.2"},
            ["coverage", "implementation"],
            selected,
            True,
        )

        self.assertEqual(fallbacks["coverage"], "kimi-k3")
        self.assertEqual(fallbacks["implementation"], "kimi-k2.7-code")

    def test_grok_upstream_fallback_prefers_glm_over_dead_deepseek_family_twin(self):
        selected = {"coverage": "kimi-k3-256k", "adversarial": "grok-4.6"}
        fallbacks = BATCH.resolve_fallback_models(
            {
                "kimi-k3-256k",
                "kimi-k3",
                "glm-5.2",
                "grok-4.6",
                "grok-4.5",
                "deepseek-v4-flash",
            },
            ["coverage", "adversarial"],
            selected,
            True,
        )

        self.assertEqual(fallbacks["coverage"], "glm-5.2")
        self.assertEqual(fallbacks["adversarial"], "glm-5.2")
        self.assertNotEqual(fallbacks["adversarial"], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
