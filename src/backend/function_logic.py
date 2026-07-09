"""Build the fixed daily availability prompt batch for Gammavet."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin
from uuid import UUID
from zoneinfo import ZoneInfo

import requests
from chask_foundation.backend.models import OrchestrationEvent

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_DRIVERS_LIST_PATH = "gammavet/drivers/list"
TENANT_MARK_PROMPTED_PATH = "gammavet/drivers/mark-availability-prompted"
ACTOR_LAMBDA = "gammavet_dispatch_availability_confirm"
DEFAULT_TENANT_SLUG = "chask"
PROD_GAMMAVET_ORG_UUID = "7a95d94b-6f55-4eb4-b971-47f77cb29e46"
PROD_GAMMAVET_TENANT_SLUG = "gammavet"
DEFAULT_TENANT_BRANCH = "test"
DEFAULT_TIMEOUT = 30
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
CHILE_TZ = ZoneInfo("America/Santiago")


class FunctionBackend:
    """Select all unprompted active drivers and return contacts for EnviarLoteWhatsappFn."""

    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        logger.info(
            "Initialized GammavetDispatchAvailabilityConfirmFn for org=%s",
            orchestration_event.organization.organization_id,
        )

    def process_request(self) -> str:
        args = self._extract_tool_args()
        now = self._now(args)
        today = now.date()
        drivers = self._drivers_for_selection(args)
        due_drivers = [driver for driver in drivers if self._driver_is_due(driver, today=today)]

        contacts: list[dict[str, Any]] = []
        for driver in due_drivers:
            mark_result = self._mark_prompted(driver)
            if mark_result.get("idempotent_noop"):
                logger.info(
                    "Skipping driver already marked prompted today: id=%s phone=%s",
                    driver.get("id"),
                    driver.get("phone"),
                )
                continue
            contact = self._contact_for_driver(driver)
            if contact is not None:
                contacts.append(contact)

        campaign_name = f"confirmacion_disponibilidad_{today.isoformat()}_{now:%H%M}"
        return json.dumps(
            {
                "clients_json": contacts,
                "notification_clients": contacts,
                "campaign_name": campaign_name,
                "context": {
                    "selected_count": len(contacts),
                    "candidate_count": len(due_drivers),
                    "now_chile": now.isoformat(),
                    "batch_time": "11:30",
                    "notification_window": "11:30-18:00",
                },
            },
            ensure_ascii=False,
        )

    def _drivers_for_selection(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        test_drivers = args.get("test_drivers_json")
        if test_drivers:
            if isinstance(test_drivers, str):
                parsed = json.loads(test_drivers)
            else:
                parsed = test_drivers
            if not isinstance(parsed, list):
                raise ValueError("test_drivers_json must be a list")
            return [driver for driver in parsed if isinstance(driver, dict)]
        extra_params = self.orchestration_event.extra_params or {}
        if extra_params.get("is_operator_params_test"):
            return []
        return self._get_all_tenant_api(
            TENANT_DRIVERS_LIST_PATH,
            {
                "active": "true",
                "paused": "false",
                "include_archived": "false",
            },
        )

    def _driver_is_due(self, driver: dict[str, Any], *, today: date) -> bool:
        if not driver.get("active", True):
            return False
        if driver.get("paused"):
            return False
        if driver.get("archived_at"):
            return False
        if self._parse_date(driver.get("availability_prompt_sent_date")) == today:
            return False
        if self._parse_date(driver.get("available_confirmed_date")) == today:
            return False
        return True

    def _mark_prompted(self, driver: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "orchestration_event_uuid": str(self._event_uuid()),
            "actor_lambda": ACTOR_LAMBDA,
        }
        if driver.get("id"):
            payload["driver_id"] = str(driver["id"])
        elif driver.get("phone"):
            payload["driver_phone"] = str(driver["phone"])
        else:
            raise ValueError(f"Driver missing id and phone: {driver!r}")
        return self._post_tenant_api(TENANT_MARK_PROMPTED_PATH, payload)

    def _contact_for_driver(self, driver: dict[str, Any]) -> dict[str, Any] | None:
        phone = str(driver.get("phone") or "").replace(" ", "")
        if not phone:
            logger.warning("Skipping due driver without phone: id=%s", driver.get("id"))
            return None

        full_name = str(driver.get("full_name") or driver.get("name") or "Conductor").strip()
        first_name = full_name.split()[0] if full_name else "Conductor"
        last_name = " ".join(full_name.split()[1:]) if len(full_name.split()) > 1 else ""
        return {
            "name": first_name,
            "last_name": last_name,
            "phone_number": phone,
            "template_parameters": [first_name],
            "context": {
                "driver_id": str(driver.get("id") or ""),
                "driver_phone": phone,
                "nombre": full_name,
                "availability_confirm_pipeline": True,
                "batch_time": "11:30",
            },
        }

    def _now(self, args: dict[str, Any]) -> datetime:
        raw_now = args.get("now_iso")
        if raw_now:
            parsed = datetime.fromisoformat(str(raw_now).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CHILE_TZ)
            return parsed.astimezone(CHILE_TZ)
        return datetime.now(CHILE_TZ)

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value)[:10])

    def _event_uuid(self) -> UUID:
        raw_event_id = getattr(self.orchestration_event, "event_id", None)
        try:
            return UUID(str(raw_event_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid orchestration_event.event_id: expected UUID, "
                f"got {raw_event_id!r}"
            ) from exc

    def _extract_tool_args(self) -> dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            return {}
        raw_args = tool_calls[0].get("args", {}) or {}
        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            return parsed_args if isinstance(parsed_args, dict) else {}
        return raw_args if isinstance(raw_args, dict) else {}

    def _get_all_tenant_api(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        limit = 500
        offset = 0
        rows: list[dict[str, Any]] = []
        while True:
            page = self._get_tenant_api(path, {**params, "limit": limit, "offset": offset})
            if not isinstance(page, list):
                raise RuntimeError(f"Tenant API {path} returned a non-list response")
            rows.extend(page)
            if len(page) < limit:
                return rows
            offset += limit

    def _get_tenant_api(self, path: str, params: dict[str, Any]) -> Any:
        session = requests.Session()
        token = self._exchange_tenant_token(session)
        url = urljoin(self._tenant_base_url(), path.lstrip("/"))
        response = self._request_with_retries(
            session,
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params,
        )
        return self._response_json_or_raise(response)

    def _post_tenant_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = requests.Session()
        token = self._exchange_tenant_token(session)
        url = urljoin(self._tenant_base_url(), path.lstrip("/"))
        response = self._request_with_retries(
            session,
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        data = self._response_json_or_raise(response)
        if not isinstance(data, dict):
            raise ValueError(f"Tenant API {path} returned a non-object response")
        return data

    def _exchange_tenant_token(self, session: requests.Session) -> str:
        env_jwt = os.environ.get("TENANT_JWT")
        if env_jwt:
            return env_jwt
        response = self._request_with_retries(
            session,
            "POST",
            f"{self._control_plane_base_url()}/auth/exchange-tenant-token",
            headers=self._control_plane_headers(),
            json={
                "org_uuid": self.orchestration_event.organization.organization_id,
                "branch": self._tenant_branch(),
            },
        )
        data = self._response_json_or_raise(response)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ValueError("Tenant token response missing access_token")
        return str(token)

    def _tenant_base_url(self) -> str:
        override_url = os.getenv("CHASK_TENANT_API_BASE_URL")
        if override_url:
            return override_url.rstrip("/") + "/"
        if self._tenant_branch() == "prod":
            return f"https://{self._tenant_slug()}.chask.co/api/"
        return f"https://{self._tenant_slug()}.chask.co/api/test/"

    def _control_plane_base_url(self) -> str:
        explicit_base_url = os.getenv("CHASK_API_BASE_URL")
        if explicit_base_url:
            return explicit_base_url.rstrip("/")
        base_domain = os.getenv("BASE_DOMAIN")
        if base_domain:
            return f"https://{base_domain}/api/v2"
        mode = os.getenv("MODE", os.getenv("GLOBAL_SERVER", "DEVELOPMENT")).upper()
        if mode == "PRODUCTION":
            return "https://app.chask.io/api/v2"
        return "https://app.chask.it/api/v2"

    def _control_plane_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Organization-ID": self.orchestration_event.organization.organization_id,
        }
        access_token = getattr(self.orchestration_event, "access_token", None)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _tenant_branch(self) -> str:
        branch = os.environ.get("TENANT_BRANCH") or os.environ.get("CHASK_TENANT_BRANCH")
        if not branch:
            base_domain = os.getenv("BASE_DOMAIN", "")
            if base_domain == "app.chask.io":
                branch = "prod"
            else:
                branch = getattr(self.orchestration_event, "branch", None) or DEFAULT_TENANT_BRANCH
        if branch not in ("prod", "test"):
            raise ValueError(f"Invalid branch: {branch}")
        return branch

    def _tenant_slug(self) -> str:
        tenant_slug = os.environ.get("TENANT_SLUG") or os.environ.get("CHASK_TENANT_SLUG")
        if tenant_slug:
            return tenant_slug
        org_uuid = self.orchestration_event.organization.organization_id
        if self._is_prod_control_plane() and org_uuid == PROD_GAMMAVET_ORG_UUID:
            return PROD_GAMMAVET_TENANT_SLUG
        return DEFAULT_TENANT_SLUG

    def _is_prod_control_plane(self) -> bool:
        if os.getenv("BASE_DOMAIN") == "app.chask.io":
            return True
        return os.getenv("MODE", os.getenv("GLOBAL_SERVER", "")).upper() == "PRODUCTION"

    def _request_with_retries(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **request_kwargs: Any,
    ) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                response = session.request(method, url, timeout=DEFAULT_TIMEOUT, **request_kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < len(RETRY_BACKOFF_SECONDS):
                    time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                    continue
                raise
            if response.status_code >= 500 and attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("Tenant API request exhausted retries without a response")

    def _response_json_or_raise(self, response: requests.Response) -> dict[str, Any] | list[Any]:
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text}
        if 200 <= response.status_code < 300:
            return data
        detail = data.get("detail") if isinstance(data, dict) else data
        raise requests.HTTPError(
            f"HTTP {response.status_code} from {response.url}: {detail}",
            response=response,
        )
