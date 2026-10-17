"""Inspect allowlisted claim metadata only; never persist/print the bearer JWT."""
import base64
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from common import require, save

KEYS = ("iss", "aud", "sub", "repository", "repository_id", "repository_owner",
        "repository_owner_id", "environment", "ref", "run_id", "workflow_ref",
        "job_workflow_ref", "job_workflow_sha")


def metadata(token):
    parts = token.split(".")
    require(len(parts) == 3, "Invalid OIDC token shape")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    # This is metadata inspection, not local signature verification. Entra will
    # perform actual issuer/signature/audience/subject validation at Azure login.
    return {key: payload[key] for key in KEYS if key in payload}


def main():
    endpoint = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    require(endpoint.startswith("https://"), "GitHub OIDC endpoint required")
    endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(
        {"audience": "api://AzureADTokenExchange"})
    request = urllib.request.Request(
        endpoint, headers={"Authorization": f'Bearer {os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}'})
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["value"]
    claims = metadata(token)
    require(claims["iss"] == "https://token.actions.githubusercontent.com", "Unexpected issuer")
    require(claims["aud"] == "api://AzureADTokenExchange", "Unexpected audience")
    require(claims["repository"] == os.environ["GITHUB_REPOSITORY"], "Unexpected repository")
    require(claims["ref"] == "refs/heads/main", "Inspect only protected main")
    environment = os.environ["INSPECT_ENVIRONMENT"]
    require(environment in {"build", "prod-kr", "prod-de", "prod-br",
                            "prod-rollback-kr", "prod-rollback-de", "prod-rollback-br"},
            "Unknown environment")
    claims["inspected_environment"] = environment
    save(Path("state") / f"claims-{environment}.json", claims)
    print(json.dumps(claims, indent=2), flush=True)


if __name__ == "__main__":
    main()
