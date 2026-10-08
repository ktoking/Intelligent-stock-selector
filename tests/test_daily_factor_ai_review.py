import json

import pytest

from research.daily_factor_lab.ai_review import (
    build_review_packet, main, request_ollama_review, validate_review, write_artifact,
)


AS_OF = "2026-09-23T00:00:00+00:00"


def event(**overrides):
    return {"title": "Company filing", "source_url": "https://www.sec.gov/example",
            "published_at": "2026-09-22T19:00:00-04:00", **overrides}


def packet():
    return build_review_packet({"signal_date": "2026-09-22"}, [event()], AS_OF)


def response(**overrides):
    return {"action": "reduce", "exposure_multiplier": 0.5,
            "evidence_ids": ["market_snapshot"], "rationale": "波动上升，建议降低目标敞口。",
            **overrides}


def test_future_evidence_and_bad_timestamps_are_explicitly_omitted():
    result = build_review_packet({}, [
        event(), event(published_at="2026-09-22T21:00:00-04:00"),
        event(published_at="2026-09-22T19:00:00"),
        event(available_at="2026-09-23T01:00:00+00:00"),
        event(source_url="file:///secret"), event(title=""),
    ], AS_OF)
    assert [item["id"] for item in result["evidence"]] == ["market_snapshot", "event_0001"]
    assert len(result["omitted_events"]) == 5
    assert "future evidence" in result["omitted_events"][0]["reason"]
    assert "timezone" in result["omitted_events"][1]["reason"]
    assert "future availability" in result["omitted_events"][2]["reason"]
    with pytest.raises(ValueError, match="timezone"):
        build_review_packet({}, [], "2026-09-23")


def test_packet_is_frozen_hashed_and_timezone_equivalent():
    snapshot, events = {"signal_date": "2026-09-22"}, [event()]
    first = build_review_packet(snapshot, events, AS_OF)
    equivalent = build_review_packet(snapshot, events, "2026-09-23T08:00:00+08:00")
    assert first == equivalent
    snapshot["signal_date"] = "2099-01-01"
    events[0]["title"] = "Changed later"
    assert first["evidence"][0]["data"]["signal_date"] == "2026-09-22"
    assert first["evidence"][1]["title"] == "Company filing"
    assert len(first["input_hashes"]["snapshot_sha256"]) == 64


@pytest.mark.parametrize("overrides", [
    {"action": "increase"}, {"action": []}, {"exposure_multiplier": 1.2},
    {"exposure_multiplier": True}, {"exposure_multiplier": float("nan")},
    {"exposure_multiplier": 0.8}, {"evidence_ids": []},
    {"evidence_ids": ["nonexistent"]}, {"evidence_ids": ["market_snapshot", "market_snapshot"]},
    {"action": "maintain", "exposure_multiplier": 0.5},
    {"action": "reduce", "exposure_multiplier": 1}, {"rationale": ""},
])
def test_invalid_llm_response_is_rejected(overrides):
    with pytest.raises(ValueError):
        validate_review(response(**overrides), packet())


def test_fixed_tiers_valid_citations_and_no_execution_authority():
    result = validate_review(response(evidence_ids=["event_0001"]), packet())
    assert result["exposure_multiplier"] == 0.5
    assert result["execution_authority"] == "none"
    assert result["mutates_frozen_backtest"] is False
    assert validate_review(response(action="pause_new", exposure_multiplier=1), packet())["action"] == "pause_new"
    assert validate_review(response(action="maintain", exposure_multiplier=1, evidence_ids=[]), packet())["action"] == "maintain"
    with pytest.raises(ValueError, match="exactly"):
        validate_review({**response(), "orders": [{"symbol": "US.AAPL"}]}, packet())
    changed = packet()
    changed["evidence"].append({"id": "invented"})
    with pytest.raises(ValueError, match="changed after hashing"):
        validate_review(response(evidence_ids=["invented"]), changed)


def test_immutable_file_and_default_cli_has_no_llm_call(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    events = tmp_path / "events.json"
    snapshot.write_text('{"signal_date": "2026-09-22"}')
    events.write_text("[]")
    output = tmp_path / "review.json"
    monkeypatch.setattr("research.daily_factor_lab.ai_review.request_ollama_review",
                        lambda *args, **kwargs: pytest.fail("Unexpected LLM call"))
    args = ["--snapshot", str(snapshot), "--events", str(events), "--asof", AS_OF,
            "--output", str(output)]
    main(args)
    artifact = json.loads(output.read_text())
    assert artifact["result"]["source"] == "deterministic_scaffold_no_llm"
    assert artifact["result"]["review"] is None
    assert artifact["result"]["status"] == "pending_review"
    assert len(artifact["input_file_hashes"]["snapshot_sha256"]) == 64
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        main(args + ["--call-llm"])
    with pytest.raises(FileExistsError):
        write_artifact(output, {"changed": True})
    assert output.read_bytes() == before


def test_ollama_response_has_auth_free_request_and_audit_hashes(monkeypatch):
    import requests

    raw = {"message": {"content": json.dumps(response(), ensure_ascii=False)}}

    class FakeResponse:
        status_code = 200
        content = json.dumps(raw).encode()

        def json(self):
            return raw

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert url == "http://127.0.0.1:11434/api/chat"
            assert self.trust_env is False
            assert kwargs["timeout"] <= 45
            assert kwargs["allow_redirects"] is False
            assert "headers" not in kwargs
            assert kwargs["json"]["stream"] is False
            assert kwargs["json"]["format"]["additionalProperties"] is False
            return FakeResponse()

    monkeypatch.setattr(requests, "Session", FakeSession)
    result = request_ollama_review(packet(), model="fake-test-model")
    assert result["source"] == "actual_ollama_response"
    assert result["status"] == "validated_llm_advice"
    for key in ("prompt_sha256", "request_sha256", "response_sha256", "response_content_sha256"):
        assert len(result[key]) == 64
    raw["message"]["content"] = json.dumps(response(action="increase"))
    FakeResponse.content = json.dumps(raw).encode()
    invalid = request_ollama_review(packet(), model="fake-test-model")
    assert invalid["status"] == "invalid_llm_advice"
    assert invalid["review"] is None
    assert "unsupported action" in invalid["validation_error"]
    assert len(invalid["response_sha256"]) == 64
    with pytest.raises(ValueError, match="at most 45"):
        request_ollama_review(packet(), timeout_seconds=46)
