"""MCP server exposing e-invoice registration as agent tools.

Transport is Streamable HTTP: one endpoint (POST /mcp) carrying JSON-RPC 2.0.
Clients call `tools/list` once to discover the tools below, then `tools/call`
to invoke them.

Tool docstrings are not documentation for humans — they are shipped to the
model verbatim as the tool description, and are the main thing determining
whether it picks the right tool with the right arguments.
"""

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from client import AccessPointError, EInvoiceClient, build_participant_id
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("einvoice-mcp")

# One client for the process, not one per tool call — building an
# httpx.AsyncClient per call throws away connection pooling and adds a TCP +
# TLS handshake to every tool the agent runs.
_client: EInvoiceClient | None = None


def get_client() -> EInvoiceClient:
    if _client is None:
        raise ToolError("The e-invoice client is not initialised.")
    return _client


@asynccontextmanager
async def lifespan(_server: FastMCP):
    global _client
    _client = EInvoiceClient(
        base_url=settings.api_base_url,
        api_key=settings.api_key,
        timeout=settings.api_timeout_seconds,
    )
    logger.info("e-invoice MCP server ready — upstream %s", settings.api_base_url)
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


mcp = FastMCP(
    name="einvoice",
    version="0.1.0",
    instructions=(
        "Tools for registering companies on the InvoiceNow (Peppol) e-invoicing "
        "network and managing their tax-submission status. Companies are "
        "identified by their UEN (Singapore business registration number, e.g. "
        "202400100A). Registration and activation are asynchronous: statuses "
        "start PENDING and settle on their own after a short delay, so check "
        "status again rather than re-submitting."
    ),
    lifespan=lifespan,
)


def _summarise(company: dict) -> dict:
    """Trim the API payload to what the model needs.

    Dropping accessPointConfigurations and the raw timestamps keeps tool
    results small — every field returned here is spent as context tokens on
    every subsequent turn of the conversation.
    """
    return {
        "uen": company["uen"],
        "companyName": company["name"],
        "participantId": company["participantId"],
        "countryCode": company["countryCode"],
        "kycStatus": company["kycStatus"],
        "kycSecondsUntilNextChange": company["kycSecondsUntilNextChange"],
        "taxStatus": company["taxStatus"],
        "taxSecondsUntilNextChange": company["taxSecondsUntilNextChange"],
    }


@mcp.tool(
    annotations=ToolAnnotations(title="Register company for e-invoicing"),
)
async def register_company_for_einvoice(
    uen: str,
    company_name: str,
    country_code: str = "SG",
    enable_tax_submission: bool = False,
) -> dict:
    """Register a company on the InvoiceNow e-invoicing network.

    KYC registration always starts, beginning at kycStatus PENDING and becoming
    REGISTERED automatically after a short delay.

    Set enable_tax_submission to true only if the customer has asked to submit
    tax documents; it starts taxStatus at PENDING_ACTIVATION, which becomes
    ACTIVATED automatically. If left false, taxStatus is null and tax
    submission can still be activated later with activate_tax_submission.

    Args:
        uen: The company's Unique Entity Number, e.g. "202400100A".
        company_name: Registered legal name of the company.
        country_code: Two-letter ISO country code. Defaults to SG.
        enable_tax_submission: Whether to also start tax-submission activation.
    """
    try:
        company = await get_client().register(
            participant_id=build_participant_id(uen),
            name=company_name,
            country_code=country_code,
            tax_submission_enabled=enable_tax_submission,
        )
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return _summarise(company)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get e-invoice registration", readOnlyHint=True
    ),
)
async def get_einvoice_registration(uen: str) -> dict:
    """Look up one company's e-invoice registration and onboarding status.

    Returns kycStatus (PENDING or REGISTERED) and taxStatus (null if tax
    submission was never enabled, otherwise PENDING_ACTIVATION, ACTIVATED,
    PENDING_DEACTIVATION or DEACTIVATED).

    When a status is still pending, the matching secondsUntilNextChange field
    says roughly how long until it settles — use it to tell the customer when
    to check back instead of guessing.

    Args:
        uen: The company's Unique Entity Number, e.g. "202400100A".
    """
    try:
        company = await get_client().get(uen.strip())
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return _summarise(company)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List e-invoice registrations", readOnlyHint=True
    ),
)
async def list_einvoice_registrations() -> dict:
    """List every company registered through this Access Point.

    Use when the customer has not given a specific UEN, or to find a company by
    name. Prefer get_einvoice_registration when the UEN is already known.
    """
    try:
        companies = await get_client().list_all()
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return {
        "count": len(companies),
        "companies": [_summarise(c) for c in companies],
    }


@mcp.tool(
    annotations=ToolAnnotations(title="Activate tax submission"),
)
async def activate_tax_submission(uen: str) -> dict:
    """Start tax-submission activation for an already-registered company.

    Allowed whatever the KYC status is — a company does not need to be
    REGISTERED first. taxStatus becomes PENDING_ACTIVATION and then ACTIVATED
    on its own after a short delay.

    Fails if tax submission is already ACTIVATED or PENDING_ACTIVATION; check
    with get_einvoice_registration first if unsure.

    Args:
        uen: The company's Unique Entity Number, e.g. "202400100A".
    """
    try:
        company = await get_client().activate_tax(uen.strip())
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return _summarise(company)


@mcp.tool(
    annotations=ToolAnnotations(title="Deactivate tax submission"),
)
async def deactivate_tax_submission(uen: str) -> dict:
    """Stop tax submission for a company.

    taxStatus becomes PENDING_DEACTIVATION and then DEACTIVATED on its own
    after a short delay. The company stays registered on the network — this
    only turns off tax submission.

    Fails if tax submission was never enabled, or is already deactivating.

    Args:
        uen: The company's Unique Entity Number, e.g. "202400100A".
    """
    try:
        company = await get_client().deactivate_tax(uen.strip())
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return _summarise(company)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Deregister company", destructiveHint=True, idempotentHint=False
    ),
)
async def deregister_company(uen: str) -> dict:
    """Permanently remove a company from the e-invoicing network.

    This deletes the registration and its tax-submission status entirely and
    cannot be undone. Only call this when the customer has explicitly asked to
    deregister or cancel their registration. To stop tax submission while
    keeping the company registered, use deactivate_tax_submission instead.

    Args:
        uen: The company's Unique Entity Number, e.g. "202400100A".
    """
    try:
        await get_client().delete(uen.strip())
    except AccessPointError as exc:
        raise ToolError(str(exc))
    return {"uen": uen.strip(), "deregistered": True}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """Liveness/readiness probe for Kubernetes.

    Plain HTTP rather than MCP — kubelet cannot speak JSON-RPC. Reports whether
    the upstream API is reachable so a probe failure points at the real cause.
    """
    upstream_ok = await _client.ping() if _client else False
    return JSONResponse(
        {
            "status": "ok" if upstream_ok else "degraded",
            "upstream": settings.api_base_url,
            "upstreamReachable": upstream_ok,
        },
        status_code=200 if upstream_ok else 503,
    )


# ASGI app for uvicorn / gunicorn / a container image.
#
# stateless_http=True matters for Kubernetes: by default the Streamable HTTP
# transport keeps per-client session state keyed by an Mcp-Session-Id header,
# so with more than one replica behind a Service, requests round-robin to a pod
# that has never seen the session. Stateless mode lets any replica serve any
# request.
app = mcp.http_app(path="/mcp", stateless_http=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
