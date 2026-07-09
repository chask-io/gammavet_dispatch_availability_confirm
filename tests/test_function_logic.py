import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.function_logic import (  # noqa: E402
    ACTOR_LAMBDA,
    TENANT_DRIVERS_LIST_PATH,
    TENANT_MARK_PROMPTED_PATH,
    FunctionBackend,
)


EVENT_ID = "11111111-2222-4333-8444-555555555555"


def _event(args=None, extra_params=None):
    params = {"tool_calls": [{"args": args or {}}]}
    if extra_params:
        params.update(extra_params)
    return SimpleNamespace(
        event_id=EVENT_ID,
        prompt="",
        extra_params=params,
        organization=SimpleNamespace(
            organization_id="99999999-aaaa-4bbb-8ccc-dddddddddddd"
        ),
        branch="test",
        access_token="access-token",
    )


def _driver(**overrides):
    data = {
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "full_name": "Test Driver",
        "phone": "+56950570324",
        "active": True,
        "paused": False,
        "archived_at": None,
        "available_confirmed_date": None,
        "availability_prompt_sent_date": None,
    }
    data.update(overrides)
    return data


def test_scheduler_selects_all_unprompted_drivers_marks_guard_and_returns_contacts(
    monkeypatch,
):
    backend = FunctionBackend(_event(args={"now_iso": "2026-07-09T11:30:00-04:00"}))
    calls = []

    def fake_get(path, params):
        calls.append({"method": "get", "path": path, "params": params})
        return [
            _driver(),
            _driver(
                id="bbbbbbbb-2222-4222-8222-222222222222",
                phone="+56911111111",
                full_name="Second Driver",
            ),
            _driver(
                id="cccccccc-3333-4333-8333-333333333333",
                phone="+56922222222",
                available_confirmed_date="2026-07-09",
            ),
        ]

    def fake_post(path, payload):
        calls.append({"method": "post", "path": path, "payload": payload})
        return {"driver": {"id": payload["driver_id"]}, "idempotent_noop": False}

    monkeypatch.setattr(backend, "_get_tenant_api", fake_get)
    monkeypatch.setattr(backend, "_post_tenant_api", fake_post)

    result = json.loads(backend.process_request())

    assert result["context"]["selected_count"] == 2
    assert result["clients_json"] == result["notification_clients"]
    assert result["clients_json"][0]["phone_number"] == "+56950570324"
    assert result["clients_json"][0]["template_parameters"] == ["Test"]
    assert result["clients_json"][1]["phone_number"] == "+56911111111"
    assert result["context"]["batch_time"] == "11:30"
    assert result["context"]["notification_window"] == "11:30-18:00"
    assert calls[0]["path"] == TENANT_DRIVERS_LIST_PATH
    assert calls[1] == {
        "method": "post",
        "path": TENANT_MARK_PROMPTED_PATH,
        "payload": {
            "driver_id": "aaaaaaaa-1111-4111-8111-111111111111",
            "orchestration_event_uuid": EVENT_ID,
            "actor_lambda": ACTOR_LAMBDA,
        },
    }
    assert calls[2]["path"] == TENANT_MARK_PROMPTED_PATH
    assert calls[2]["payload"]["driver_id"] == "bbbbbbbb-2222-4222-8222-222222222222"


def test_scheduler_skips_contact_when_mark_endpoint_reports_idempotent_noop(monkeypatch):
    backend = FunctionBackend(_event(args={"now_iso": "2026-07-09T11:30:00-04:00"}))
    monkeypatch.setattr(backend, "_get_tenant_api", lambda path, params: [_driver()])
    monkeypatch.setattr(
        backend,
        "_post_tenant_api",
        lambda path, payload: {"driver": {"id": payload["driver_id"]}, "idempotent_noop": True},
    )

    result = json.loads(backend.process_request())

    assert result["clients_json"] == []
    assert result["context"]["candidate_count"] == 1


def test_scheduler_can_use_test_drivers_json_without_get(monkeypatch):
    backend = FunctionBackend(
        _event(
            args={
                "now_iso": "2026-07-09T11:30:00-04:00",
                "test_drivers_json": json.dumps([_driver()]),
            }
        )
    )

    def fail_get(*args, **kwargs):
        raise AssertionError("test_drivers_json should avoid Tenant API list")

    monkeypatch.setattr(backend, "_get_tenant_api", fail_get)
    monkeypatch.setattr(
        backend,
        "_post_tenant_api",
        lambda path, payload: {"driver": {"id": payload["driver_id"]}, "idempotent_noop": False},
    )

    result = json.loads(backend.process_request())

    assert len(result["clients_json"]) == 1


def test_operator_params_suite_path_does_not_read_tenant_api(monkeypatch):
    backend = FunctionBackend(_event(extra_params={"is_operator_params_test": True}))

    def fail_get(*args, **kwargs):
        raise AssertionError("operator params suite must not read Tenant API")

    monkeypatch.setattr(backend, "_get_tenant_api", fail_get)

    result = json.loads(backend.process_request())

    assert result["clients_json"] == []
