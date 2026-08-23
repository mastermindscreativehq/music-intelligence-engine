"""Phase 5 tests: source comparison, verification workflow, optional LLM.

Every test runs OFFLINE. The LLM layer is exercised through injectable
transports — no Ollama server is ever required.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from enrichment.compare import (
    CONFLICTING as COMPARE_CONFLICTING,
    CORROBORATED,
    SINGLE_SOURCE,
    SOURCE_STRENGTH,
    UNOBSERVED,
    SourceComparator,
    compare_record,
    normalize_claim_value,
)
from enrichment.llm import (
    ENV_HOST,
    ENV_MODEL,
    OllamaClient,
    OllamaConfig,
    TemplateError,
    load_template,
    suggest_contact_role,
    validate_against_schema,
)
from enrichment.verify import (
    CONFLICTING,
    STALE,
    UNSUPPORTED,
    UNVERIFIED,
    VERIFIED,
    apply_verification,
    is_stale,
    verify_record,
    verify_records,
)


def _now() -> datetime:
    return datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class FakeTransport:
    """Injectable transport returning queued responses in order."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path))
        if method == "GET":        # /api/tags probe never consumes queue
            return {}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


_OFFLINE_CONFIG = OllamaConfig(host="http://127.0.0.1:9",
                               model="test-model")


# ---------------------------------------------------------------------------
# Source comparison
# ---------------------------------------------------------------------------

class TestCompare(unittest.TestCase):
    def test_same_value_two_domains_corroborates(self):
        comparator = SourceComparator()
        comparator.observe("emails[music@x.example]", "email",
                           "music@x.example", "https://a.example/contact",
                           "official_website_page", "2026-01-01")
        comparator.observe("emails[music@x.example]", "email",
                           "MUSIC@x.example", "https://b.example/page",
                           "directory_source", "2026-02-01")
        entries = comparator.evaluate()
        self.assertEqual(entries[0].outcome, CORROBORATED)
        self.assertEqual(entries[0].values[0]["independent_domains"], 2)

    def test_conflicting_values_both_preserved_no_winner(self):
        comparator = SourceComparator()
        comparator.observe_fact({"value": "old@x.example",
                                 "source_url": "https://dir.example/x",
                                 "source_type": "directory_source"}, "e",
                                "email")
        comparator.observe("e", "email", "new@x.example",
                           "https://x.example/contact",
                           "official_website_page")
        entry = comparator.evaluate()[0]
        self.assertEqual(entry.outcome, COMPARE_CONFLICTING)
        values = {v["value"] for v in entry.values}
        self.assertEqual(values, {"old@x.example", "new@x.example"})
        self.assertIn("no automatic winner chosen",
                      " ".join(entry.reasons))
        strongest = max(v["strongest_evidence"]["strength"]
                        for v in entry.values)
        self.assertEqual(strongest,
                         SOURCE_STRENGTH["official_website_page"])

    def test_single_source_and_unobserved_summary(self):
        report = compare_record({
            "emails": [{"value": "one@x.example",
                        "source_url": "https://a.example/c",
                        "discovered_at": "2026-01-01"}],
        })
        self.assertEqual(
            report["summary"],
            {CORROBORATED: 0, CONFLICTING: 0, SINGLE_SOURCE: 1,
             UNOBSERVED: 0})

    def test_empty_record_yields_zero_claims(self):
        report = compare_record({})
        self.assertEqual(report["claims"], [])
        self.assertEqual(sum(report["summary"].values()), 0)

    def test_compare_does_not_mutate_input(self):
        record = {
            "station_id": "s1",
            "emails": [{"value": "a@x.example", "source_url": "https://a.e/",
                        "also_seen_at": ["https://b.e/"]}],
            "contacts": [{"email": "a@x.example"}],
        }
        snapshot = json.dumps(record, sort_keys=True)
        compare_record(record)
        verify_record(record)
        self.assertEqual(json.dumps(record, sort_keys=True), snapshot)

    def test_normalize_claim_value_email_casefold(self):
        self.assertEqual(normalize_claim_value("email", " A@X.com "),
                         "a@x.com")


# ---------------------------------------------------------------------------
# Verification workflow
# ---------------------------------------------------------------------------

def _corroborated_record(last_verified_at=None):
    return {
        "station_id": "s1",
        "last_verified_at": last_verified_at,
        "emails": [
            {"value": "md@x.example", "source_url": "https://x.example/md",
             "source_type": "official_website_page",
             "discovered_at": "2026-07-01"},
            {"value": "md@x.example", "source_url": "https://y.example/dir",
             "source_type": "directory_source",
             "discovered_at": "2026-07-02"},
        ],
    }


class TestVerify(unittest.TestCase):
    def test_corroborated_claim_becomes_verified(self):
        results = verify_record(_corroborated_record(), now=_now())
        statuses = {r["status"] for r in results}
        self.assertIn(VERIFIED, statuses)
        verified = results[0]
        self.assertEqual(verified["verifier"], "code")
        self.assertEqual(verified["method"], "source_comparison")
        self.assertTrue(verified["checked_at"])

    def test_stale_when_previous_verification_too_old(self):
        old = _now() - timedelta(days=120)
        results = verify_record(_corroborated_record(
            last_verified_at=_iso(old)), now=_now(), max_age_days=90)
        self.assertTrue(all(r["status"] == STALE for r in results
                            if r["method"] == "source_comparison"))

    def test_fresh_previous_verification_stays_verified(self):
        fresh = _now() - timedelta(days=10)
        results = verify_record(_corroborated_record(
            last_verified_at=_iso(fresh)), now=_now())
        self.assertIn(VERIFIED, {r["status"] for r in results})

    def test_is_stale_handles_unparsable_and_missing(self):
        self.assertFalse(is_stale(None, _now(), 90))
        self.assertFalse(is_stale("not-a-date", _now(), 90))

    def test_unsupported_contact_without_provenance(self):
        record = _corroborated_record()
        record["contacts"] = [{"email": "ghost@x.example"}]
        results = verify_record(record, now=_now())
        unsupported = [r for r in results if r["status"] == UNSUPPORTED]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["claim"], "contacts[0].email")

    def test_backed_contact_not_flagged_unsupported(self):
        record = _corroborated_record()
        record["contacts"] = [{"email": "md@x.example"}]
        results = verify_record(record, now=_now())
        self.assertNotIn(
            UNSUPPORTED, [r["status"] for r in results])

    def test_apply_verification_touches_lifecycle_only(self):
        record = _corroborated_record()
        before_values = json.dumps(record["emails"], sort_keys=True)
        results = verify_record(record, now=_now())
        apply_verification(record, results)
        self.assertEqual(json.dumps(record["emails"], sort_keys=True),
                         before_values)          # facts untouched
        self.assertTrue(record.get("last_verified_at"))
        meta = record["raw_metadata"]["verification"]["last_result"]
        self.assertGreaterEqual(meta["verified"], 1)

    def test_verify_records_summary_counts(self):
        records = [_corroborated_record(), {}]
        report = verify_records(records, now=_now())
        self.assertEqual(report["summary"][VERIFIED], 1)
        self.assertEqual(sum(report["summary"].values()), 1)
        self.assertEqual(len(report["records"]), 2)


# ---------------------------------------------------------------------------
# Versioned prompt templates + offline LLM layer
# ---------------------------------------------------------------------------

class TestTemplates(unittest.TestCase):
    def test_contact_role_template_contract(self):
        template = load_template("enrichment", "contact_role")
        self.assertEqual(template.version, 1)
        self.assertTrue(template.purpose)
        self.assertIn("context_text", template.variables)
        enum = template.output_schema["properties"]["role"]["enum"]
        self.assertIn("unknown", enum)
        self.assertIsInstance(template.example, dict)
        rendered = template.render(context_text="jane@x.example contact")
        self.assertNotIn("{{context_text}}", rendered)

    def test_station_genre_template_contract(self):
        template = load_template("enrichment", "station_genre")
        self.assertEqual(template.variables, ("station_name", "page_text"))
        self.assertIsInstance(template.example, dict)

    def test_missing_variable_raises_template_error(self):
        template = load_template("enrichment", "contact_role")
        with self.assertRaises(TemplateError):
            template.render(wrong="x")

    def test_schema_validator_rejects_bad_enum(self):
        schema = {"type": "object", "required": ["role"],
                  "properties": {"role": {"type": "string",
                                          "enum": ["dj", "host"]}}}
        with self.assertRaises(ValueError):
            validate_against_schema({"role": "president"}, schema)
        validate_against_schema({"role": "dj"}, schema)


class TestOllamaOffline(unittest.TestCase):
    CONFIG = _OFFLINE_CONFIG

    def test_generate_ok_carries_model_and_prompt_version(self):
        transport = FakeTransport({"response":
                                   '```json\n{"role": "dj"}\n```'})
        client = OllamaClient(self.CONFIG, transport=transport)
        template = load_template("enrichment", "contact_role")
        result = client.generate(template, context_text="spin records")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"role": "dj"})
        self.assertEqual(result.model, "test-model")
        self.assertEqual(result.prompt_version, "v1")

    def test_unparsable_output_rejected_without_retry(self):
        transport = FakeTransport({"response": "I think it's a DJ!"})
        client = OllamaClient(self.CONFIG, transport=transport)
        result = client.generate(load_template("enrichment",
                                               "contact_role"),
                                 context_text="spin records")
        self.assertFalse(result.ok)
        self.assertTrue(result.error_kind.startswith("UnparsableOutput"))
        self.assertEqual(result.attempts, 1)

    def test_schema_violation_rejected_without_retry(self):
        transport = FakeTransport({"response": '{"role": "president"}'})
        client = OllamaClient(self.CONFIG, transport=transport)
        result = client.generate(load_template("enrichment",
                                               "contact_role"),
                                 context_text="spin records")
        self.assertFalse(result.ok)
        self.assertTrue(result.error_kind.startswith("InvalidOutput"))

    def test_transport_errors_retry_then_fail_cleanly(self):
        transport = FakeTransport(ConnectionError("down"),
                                  ConnectionError("down"))
        client = OllamaClient(self.CONFIG, transport=transport)
        result = client.generate(load_template("enrichment",
                                               "contact_role"),
                                 context_text="spin records")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "ConnectionError")
        self.assertEqual(result.attempts, self.CONFIG.max_attempts)

    def test_config_reads_env_var_names_only(self):
        os.environ[ENV_HOST] = "http://localhost:59999"
        os.environ[ENV_MODEL] = "unit-test-model"
        try:
            config = OllamaConfig.from_env()
            self.assertEqual(config.host, "http://localhost:59999")
            self.assertEqual(config.model, "unit-test-model")
        finally:
            del os.environ[ENV_HOST]
            del os.environ[ENV_MODEL]


class TestSuggestContactRole(unittest.TestCase):
    CONFIG = _OFFLINE_CONFIG

    def test_rules_win_without_consulting_llm(self):
        role, meta = suggest_contact_role("Music Director: jane@x.example")
        self.assertEqual(role, "music_director")
        self.assertIsNone(meta)

    def test_no_client_falls_back_to_unknown(self):
        role, meta = suggest_contact_role("jane@x.example station contact")
        self.assertEqual((role, meta), ("unknown", None))

    def test_unreachable_server_falls_back_cleanly(self):
        client = OllamaClient(OllamaConfig(host="http://127.0.0.1:9"))
        role, meta = suggest_contact_role("jane@x.example station contact",
                                          client=client)
        self.assertEqual((role, meta), ("unknown", None))

    def test_invalid_output_falls_back_to_unknown(self):
        transport = FakeTransport({"response": '{"role": "wizard"}'})
        client = OllamaClient(self.CONFIG, transport=transport)
        role, meta = suggest_contact_role("jane@x.example station contact",
                                          client=client)
        self.assertEqual((role, meta), ("unknown", None))

    def test_valid_hint_returns_inference_metadata(self):
        transport = FakeTransport(
            {"response": '{"role": "dj", "reason": "spins nights"}'})
        client = OllamaClient(self.CONFIG, transport=transport)
        # Context must defeat every deterministic rule so the LLM is hit:
        role, meta = suggest_contact_role("jane handles the night slot "
                                          "jane@x.example", client=client)
        self.assertEqual(role, "dj")
        self.assertEqual(meta["kind"], "inference")
        self.assertEqual(meta["model"], "test-model")
        self.assertEqual(meta["prompt_version"], "v1")


# ---------------------------------------------------------------------------
# Opt-in pipeline hook (default engine stays deterministic/offline)
# ---------------------------------------------------------------------------

STATION = {
    "id": "st-1",
    "name": "Radio X",
    "website": "https://x.example/",
    "contacts": [{"id": "c1", "email": "jane@x.example",
                  "role": "unknown"}],
}


class TestPipelineRoleAdvisor(unittest.TestCase):
    def test_default_engine_leaves_roles_untouched(self):
        from discovery.radio.enrich_pipeline import EnrichmentEngine
        engine = EnrichmentEngine()
        self.assertIsNone(engine._role_advisor)
        result = engine.enrich_records([dict(STATION)])
        contacts = result.records[0]["contacts"]
        self.assertEqual(contacts[0]["role"], "unknown")

    def test_advisor_flips_unknown_role_with_inference_provenance(self):
        from discovery.radio.enrich_pipeline import EnrichmentEngine

        def advisor(context_text):
            return "music_director", {"kind": "inference",
                                      "method": "llm",
                                      "model": "m", "prompt_version": "v1"}

        engine = EnrichmentEngine(role_advisor=advisor)
        result = engine.enrich_records([json.loads(json.dumps(STATION))])
        contact = result.records[0]["contacts"][0]
        self.assertEqual(contact["role"], "music_director")
        inference = [p for p in contact["provenance"]
                     if p.get("kind") == "inference"]
        self.assertEqual(len(inference), 1)
        self.assertEqual(inference[0]["prompt_version"], "v1")
        self.assertTrue(any("local model" in reason for reason in
                            contact["confidence_reasons"]))

    def test_advisor_unknown_answer_keeps_honest_default(self):
        from discovery.radio.enrich_pipeline import EnrichmentEngine
        engine = EnrichmentEngine(
            role_advisor=lambda text: ("unknown", None))
        result = engine.enrich_records([json.loads(json.dumps(STATION))])
        contact = result.records[0]["contacts"][0]
        self.assertEqual(contact["role"], "unknown")
        self.assertFalse(any(p.get("kind") == "inference"
                             for p in contact["provenance"]))

    def test_raising_advisor_never_kills_enrichment(self):
        from discovery.radio.enrich_pipeline import EnrichmentEngine

        def boom(text):
            raise RuntimeError("advisor down")

        engine = EnrichmentEngine(role_advisor=boom)
        result = engine.enrich_records([json.loads(json.dumps(STATION))])
        self.assertEqual(result.failure_count, 0)
        self.assertEqual(result.records[0]["contacts"][0]["role"],
                         "unknown")

    def test_cli_without_flag_never_builds_a_client(self):
        import enrichment.llm as llm_module
        from discovery.radio.enrich_pipeline import main as enrich_main
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "records.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([STATION], handle)
            out = os.path.join(tmp, "out.json")
            with mock.patch.object(llm_module, "OllamaClient") as mock_client:
                rc = enrich_main(["--input", path, "--output", out])
            self.assertEqual(rc, 0)
            mock_client.assert_not_called()

    def test_cli_with_ai_roles_flag_wires_opt_in_client(self):
        import discovery.radio.enrich_pipeline as pipeline
        import enrichment.llm as llm_module
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "records.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([STATION], handle)
            out = os.path.join(tmp, "out.json")
            fake_client = llm_module.OllamaClient(
                OllamaConfig(host="http://127.0.0.1:9"))
            with mock.patch.object(llm_module, "OllamaClient",
                                   return_value=fake_client) as mock_client:
                rc = pipeline.main(["--input", path, "--output", out,
                                    "--ai-roles"])
            self.assertEqual(rc, 0)
            mock_client.assert_called_once()


if __name__ == "__main__":
    unittest.main()
