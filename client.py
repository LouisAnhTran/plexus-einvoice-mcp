"""HTTP client for the e-invoice Access Point API.

This module is the only thing in the server that knows the API exists. It holds
the API key and speaks REST; it has no database credentials and no direct
storage access — the same boundary a real MCP server sits behind.

Errors are translated into AccessPointError with messages written to be read by
a language model. A bare "HTTP 401" or a stack trace gives the model nothing to
act on, so it invents an explanation for the user; a sentence naming the actual
problem lets it report the truth.
"""

import httpx

# Peppol issuing-agency scheme for Singapore UEN. Other jurisdictions use
# different codes; this dummy only models SG.
SG_UEN_SCHEME = "0195"
PARTICIPANT_PREFIX = "iso6523-actorid-upis"


class AccessPointError(Exception):
    """A call to the Access Point API failed, with a model-readable reason."""


def build_participant_id(uen: str, scheme: str = SG_UEN_SCHEME) -> str:
    """Turn a bare UEN into a Peppol participant ID.

    Done here rather than asking the model for the full string — expecting an
    LLM to assemble `iso6523-actorid-upis::0195:202400100A` byte-perfectly is a
    reliable source of malformed identifiers.
    """
    uen = uen.strip()
    if uen.startswith(f"{PARTICIPANT_PREFIX}::"):
        return uen  # already fully qualified
    return f"{PARTICIPANT_PREFIX}::{scheme}:{uen}"


class EInvoiceClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise AccessPointError(
                f"The Access Point API at {self._base_url} did not respond in time."
            )
        except httpx.RequestError as exc:
            raise AccessPointError(
                f"Could not reach the Access Point API at {self._base_url} "
                f"({exc.__class__.__name__}). Check the service is running and "
                f"EINVOICE_API_BASE_URL is correct."
            )

        if response.is_success:
            return None if response.status_code == 204 else response.json()

        detail = _extract_detail(response)

        if response.status_code == 401:
            raise AccessPointError(
                "The Access Point rejected the API key (401). The MCP server's "
                "EINVOICE_API_KEY does not match the one the API expects."
            )
        if response.status_code == 404:
            raise AccessPointError(f"Not found: {detail}")
        if response.status_code == 409:
            raise AccessPointError(detail)
        if response.status_code == 422:
            raise AccessPointError(f"The Access Point rejected the request: {detail}")
        if response.status_code >= 500:
            raise AccessPointError(
                f"The Access Point API returned a server error "
                f"({response.status_code}). This is a problem with the API, not "
                f"the request."
            )
        raise AccessPointError(f"Request failed ({response.status_code}): {detail}")

    # --- operations ---

    async def register(
        self,
        participant_id: str,
        name: str,
        country_code: str,
        tax_submission_enabled: bool,
    ) -> dict:
        return await self._request(
            "POST",
            "/ap/v1/participant",
            json={
                "participantId": participant_id,
                "name": name,
                "countryCode": country_code.upper(),
                "taxSubmissionEnabled": tax_submission_enabled,
                "accessPointConfigurations": [],
            },
        )

    async def get(self, identifier: str) -> dict:
        return await self._request("GET", f"/ap/v1/participant/{identifier}")

    async def list_all(self) -> list[dict]:
        return await self._request("GET", "/ap/v1/participant")

    async def activate_tax(self, identifier: str) -> dict:
        return await self._request(
            "POST", f"/ap/v1/participants/{identifier}/tax/activate"
        )

    async def deactivate_tax(self, identifier: str) -> dict:
        return await self._request(
            "POST", f"/ap/v1/participants/{identifier}/tax/deactivate"
        )

    async def delete(self, identifier: str) -> None:
        await self._request("DELETE", f"/ap/v1/participant/{identifier}")

    async def ping(self) -> bool:
        """Unauthenticated health check, used by the /healthz route."""
        try:
            response = await self._client.get("/health", timeout=5.0)
            return response.is_success
        except httpx.RequestError:
            return False


def _extract_detail(response: httpx.Response) -> str:
    """Pull FastAPI's {"detail": ...} out, falling back to raw text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    return str(detail) if detail else f"HTTP {response.status_code}"
