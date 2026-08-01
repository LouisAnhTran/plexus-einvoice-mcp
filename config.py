from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Env-driven config so this server deploys independently.

    Env var names are declared explicitly rather than derived from a prefix,
    because EINVOICE_API_KEY has to match the value einvoice-api is configured
    with — the two services share one secret under one name, so a compose file
    or Kubernetes Secret can feed both without drift.
    """

    api_base_url: str = Field(
        default="http://localhost:8100",
        validation_alias="EINVOICE_API_BASE_URL",
        description="Base URL of the e-invoice Access Point API.",
    )
    api_key: str = Field(
        default="dev-smp-key",
        validation_alias="EINVOICE_API_KEY",
        description="Sent as X-Api-Key. Must equal the API's EINVOICE_API_KEY.",
    )
    api_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="EINVOICE_API_TIMEOUT_SECONDS",
        description=(
            "Per-request timeout when calling the API. Bounded so a hung "
            "upstream surfaces as a tool error instead of stalling the agent."
        ),
    )

    host: str = Field(default="127.0.0.1", validation_alias="MCP_HOST")
    port: int = Field(default=9000, validation_alias="MCP_PORT")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
