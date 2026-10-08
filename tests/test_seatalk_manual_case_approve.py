from dataclasses import replace
from types import SimpleNamespace

from scripts import seatalk_hermes_adapter as adapter


def _message(text: str) -> adapter.InboundMessage:
    return adapter.InboundMessage(
        event_id="event-1",
        event_type="message.group_chat",
        text=text,
        employee_code="580019",
        user_id="owner-user",
        group_id="group-1",
        thread_id="thread-1",
        message_id="message-1",
        quoted_message_id="",
    )


def test_parse_manual_case_approve_request_accepts_explicit_approval_commands():
    assert adapter._parse_manual_case_approve_request("审批case 100010010") == "100010010"
    assert adapter._parse_manual_case_approve_request("审批 caseId=100010010") == "100010010"
    assert adapter._parse_manual_case_approve_request("approve case 100010010") == "100010010"


def test_parse_manual_case_approve_request_ignores_non_approval_discussion():
    assert adapter._parse_manual_case_approve_request("为什么 case 100010010 审批失败") is None
    assert adapter._parse_manual_case_approve_request("查看 case 100010010") is None


def test_member_manual_case_approve_command_preloads_skill(monkeypatch):
    cfg = adapter.get_settings()
    captured = {}

    def fake_run(prompt, settings, *, owner_elevated=False, skills_override=""):
        captured.update(
            prompt=prompt,
            settings=settings,
            owner_elevated=owner_elevated,
            skills_override=skills_override,
        )
        return "审批成功"

    monkeypatch.setattr(adapter, "_run_hermes", fake_run)

    reply = adapter._handle_manual_case_approve_command(
        _message("审批case 100010010"),
        cfg,
    )

    assert reply == "审批成功"
    assert captured["owner_elevated"] is True
    assert captured["skills_override"] == "approve-manual-case"
    assert "100010010" in captured["prompt"]
    assert "SeaTalk sender employee code: 580019" in captured["prompt"]
    assert "owner 明确授权" not in captured["prompt"]


def test_owner_manual_case_approve_command_preloads_skill(monkeypatch):
    cfg = adapter.get_settings()
    captured = {}

    def fake_run(prompt, settings, *, owner_elevated=False, skills_override=""):
        captured.update(
            prompt=prompt,
            settings=settings,
            owner_elevated=owner_elevated,
            skills_override=skills_override,
        )
        return "审批成功"

    monkeypatch.setattr(adapter, "_run_hermes", fake_run)

    reply = adapter._handle_manual_case_approve_command(
        _message("审批case 100010010"),
        cfg,
    )

    assert reply == "审批成功"
    assert captured["owner_elevated"] is True
    assert captured["skills_override"] == "approve-manual-case"
    assert "100010010" in captured["prompt"]
    assert "ekyc-center-dev2-ext.biz.maribanksvc.com" in captured["prompt"]


def test_run_hermes_skill_override_replaces_global_preload(monkeypatch, tmp_path):
    cfg = replace(
        adapter.get_settings(),
        hermes_bin="hermes",
        hermes_project_dir=tmp_path,
        hermes_skills="unrelated-skill",
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result = adapter._run_hermes(
        "approve request",
        cfg,
        owner_elevated=True,
        skills_override="approve-manual-case",
    )

    assert result == "ok"
    skills_index = captured["cmd"].index("--skills")
    assert captured["cmd"][skills_index + 1] == "approve-manual-case"
    assert "unrelated-skill" not in captured["cmd"]
