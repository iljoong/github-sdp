import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

REGIONS = ("kr", "sea", "usw")
ENVIRONMENTS = frozenset(f"prod-{kind}{r}" for r in REGIONS for kind in ("", "rollback-"))
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
RESOURCE = re.compile(r"/subscriptions/[A-Fa-f0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft\.App/containerApps/[a-z0-9-]+", re.I)


def positive(value):
    if not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError("Invalid identifier")
    return int(value)


def repository(value):
    if not REPO.fullmatch(value) or value.split("/")[1] in (".", ".."):
        raise ValueError("Invalid repository")
    return value.lower()


@dataclass(frozen=True)
class Target:
    resource_id: str
    app_name: str


@dataclass(frozen=True)
class SharedSettings:
    repo: str
    repo_id: int
    installation_id: int
    table_endpoint: str

    @classmethod
    def from_env(cls):
        endpoint = os.environ["DASHBOARD_TABLE_ENDPOINT"]
        if not re.fullmatch(r"https://[a-z0-9]{3,24}\.table\.core\.windows\.net", endpoint):
            raise ValueError("Invalid Azure Table endpoint")
        return cls(repository(os.environ["DASHBOARD_REPO"]), positive(os.environ["DASHBOARD_REPO_ID"]),
                   positive(os.environ["DASHBOARD_INSTALLATION_ID"]), endpoint)


@dataclass(frozen=True)
class Settings:
    shared: SharedSettings
    tenant_id: str
    entra_client_id: str
    origin: str
    app_client_id: str
    signing_key: str
    targets: dict[str, Target]

    @classmethod
    def from_env(cls):
        origin = os.environ["DASHBOARD_ORIGIN"]
        url = urlsplit(origin)
        if url.scheme != "https" or not url.hostname or origin != f"https://{url.hostname}":
            raise ValueError("Expected an HTTPS origin without a path")
        targets = json.loads(os.environ["DASHBOARD_TARGETS"])
        if set(targets) != set(REGIONS) or any(not RESOURCE.fullmatch(value) for value in targets.values()):
            raise ValueError("Expected three registered Azure Container Apps")
        key = os.environ["DASHBOARD_SIGNING_KEY"]
        if not re.fullmatch(r"https://[a-z0-9-]+\.vault\.azure\.net/keys/[A-Za-z0-9-]+/[A-Fa-f0-9]{32}", key):
            raise ValueError("Expected a versioned Azure Key Vault key")
        client = os.environ["DASHBOARD_APP_CLIENT_ID"]
        if not re.fullmatch(r"[A-Za-z0-9.]+", client):
            raise ValueError("Invalid GitHub App client ID")
        return cls(SharedSettings.from_env(), str(UUID(os.environ["DASHBOARD_TENANT_ID"])),
                   str(UUID(os.environ["DASHBOARD_ENTRA_CLIENT_ID"])), origin, client, key,
                   {r: Target(value, value.rsplit("/", 1)[1]) for r, value in targets.items()})
