import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "2024-03-01"
AZ = "az.cmd" if os.name == "nt" else "az"
_next_login = time.monotonic() + 1800


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def az(*args):
    global _next_login
    if time.monotonic() >= _next_login:
        refresh_login()
        _next_login = time.monotonic() + 1800
    result = subprocess.run([AZ, *args, "--only-show-errors", "-o", "json"],
                            text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"az {' '.join(args[:4])} failed: {result.stderr[-2500:]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def refresh_login():
    """azure/login's original OIDC assertion cannot be reused for a 3h bake."""
    endpoint = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    require(endpoint.startswith("https://"), "Fresh GitHub OIDC endpoint unavailable")
    endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(
        {"audience": "api://AzureADTokenExchange"})
    request = urllib.request.Request(
        endpoint, headers={"Authorization": f'Bearer {os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}'})
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["value"]
    require(isinstance(token, str) and token.count(".") == 2, "Invalid GitHub OIDC assertion")
    # Never write the JWT to stdout, even as a workflow masking command.
    try:
        result = subprocess.run(
            [AZ, "login", "--service-principal", "--username", os.environ["AZURE_CLIENT_ID"],
             "--tenant", os.environ["AZURE_TENANT_ID"], "--federated-token", token,
             "--only-show-errors", "-o", "none"], text=True, capture_output=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        # TimeoutExpired normally embeds the full argv, including the assertion.
        raise RuntimeError("Fresh OIDC Azure login timed out or could not start") from None
    require(result.returncode == 0, f"Fresh OIDC Azure login failed (exit {result.returncode}); raw output suppressed")
    result = subprocess.run(
        [AZ, "account", "set", "--subscription", os.environ["AZURE_SUBSCRIPTION_ID"],
         "--only-show-errors"], text=True, capture_output=True, timeout=30)
    require(result.returncode == 0, "Cannot select subscription after fresh OIDC login")


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def image_valid(image):
    return bool(re.fullmatch(r"[a-z0-9]+\.azurecr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image))


def app_id():
    subscription = os.environ["AZURE_SUBSCRIPTION_ID"]
    group, name = os.environ["RESOURCE_GROUP"], os.environ["CONTAINER_APP"]
    require(re.fullmatch(r"[a-zA-Z0-9_.()-]+", group), "Invalid resource group")
    require(re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name), "Invalid app name")
    return f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.App/containerApps/{name}"


def app():
    return az("rest", "--method", "get", "--url", f"https://management.azure.com{app_id()}?api-version={API}")


def revision(name):
    require(re.fullmatch(r"[a-z0-9-]+", name), "Invalid revision name")
    return az("containerapp", "revision", "show", "-g", os.environ["RESOURCE_GROUP"],
              "-n", os.environ["CONTAINER_APP"], "--revision", name)


def props(value):
    return value["properties"]


def invariant(value):
    """Only traffic and revision template are expected to change in this sample."""
    config = json.loads(json.dumps(props(value)["configuration"]))
    config["ingress"].pop("traffic", None)
    return digest({"configuration": config, "identity": value.get("identity"),
                   "environmentId": props(value).get("environmentId"),
                   "managedEnvironmentId": props(value).get("managedEnvironmentId"),
                   "location": value.get("location")})


def traffic(value):
    result = {}
    for item in props(value)["configuration"]["ingress"].get("traffic", []):
        require(not item.get("latestRevision"), "latestRevision traffic forbidden, including zero weights")
        require(not item.get("label"), "Existing revision labels forbidden in this example")
        name, weight = item.get("revisionName"), item.get("weight", 0)
        require(name and name not in result and isinstance(weight, int) and 0 <= weight <= 100,
                "Malformed explicit traffic")
        result[name] = weight
    require(sum(result.values()) == 100, "Traffic must total 100")
    return result


def equal_traffic(actual, expected):
    # Azure can omit zero-weight entries.
    return {k: v for k, v in actual.items() if v} == {k: v for k, v in expected.items() if v}


def cli_app():
    return ["-g", os.environ["RESOURCE_GROUP"], "-n", os.environ["CONTAINER_APP"]]


def set_traffic(weights):
    require(sum(weights.values()) == 100 and "latest" not in weights, "Invalid traffic update")
    return az("containerapp", "ingress", "traffic", "set", *cli_app(),
              "--revision-weight", *[f"{name}={weight}" for name, weight in weights.items()])


def ready(value):
    p = props(value)
    return (p.get("active") is True and p.get("healthState") == "Healthy"
            and p.get("provisioningState") == "Provisioned"
            and p.get("runningState") in {"Running", "RunningAtMaxScale"})


def verify_context(snapshot):
    require(snapshot["schema"] == 1, "Unsupported snapshot")
    require(snapshot["app_id"].lower() == app_id().lower(), "Snapshot targets another app")
    require(snapshot["repository"] == os.environ["GITHUB_REPOSITORY"], "Snapshot from another repository")
    require(snapshot["region"] == os.environ["REGION"], "Snapshot from another region")
    require(re.fullmatch(r"[0-9]+", snapshot["run_id"]), "Invalid source run")
    require(snapshot["candidate"] == f'{os.environ["CONTAINER_APP"]}--r{snapshot["run_id"]}',
            "Candidate not owned by source run")
    require(image_valid(snapshot["image"]), "Invalid candidate digest")


def normalized_template(template):
    value = json.loads(json.dumps(template))
    scale = value.get("scale", {})
    for key in ("cooldownPeriod", "pollingInterval"):
        if key in scale and scale[key] is None:
            del scale[key]
    for container in value.get("containers", []):
        if "env" in container:
            container["env"] = sorted(container["env"], key=lambda v: v["name"])
        resources = container.get("resources", {})
        # Azure materializes 1Gi ephemeral storage for this example's Consumption size.
        if (resources.get("cpu") == 0.25 and resources.get("memory") == "0.5Gi"
                and resources.get("ephemeralStorage") == "1Gi"):
            del resources["ephemeralStorage"]
    return value


def expected_candidate(snapshot):
    template = json.loads(json.dumps(snapshot["stable_template"]))
    template["revisionSuffix"] = f'r{snapshot["run_id"]}'
    container = template["containers"][0]
    container["image"] = snapshot["image"]
    env = [v for v in container.get("env", []) if v["name"] != "RELEASE_ID"]
    env.append({"name": "RELEASE_ID", "value": snapshot["run_id"]})
    container["env"] = env
    # Older recovery artifacts predate the explicit candidate probe policy.
    if "candidate_probes" in snapshot:
        container["probes"] = snapshot["candidate_probes"]
    return normalized_template(template)


def guarded_state(snapshot, expected_weights=None):
    verify_context(snapshot)
    current = app()
    require(invariant(current) == snapshot["invariant"], "App-scoped drift; manual investigation required")
    stable = revision(snapshot["stable"])
    require(digest(props(stable)["template"]) == snapshot["stable_template_hash"], "Stable template changed")
    require(ready(stable), "Stable unavailable; cannot safely route to it")
    weights = traffic(current)
    require({name for name, weight in weights.items() if weight}.issubset({snapshot["stable"], snapshot["candidate"]}),
            "Unknown revision in traffic; refusing to overwrite another release")
    if expected_weights is not None:
        require(equal_traffic(weights, expected_weights), "Traffic drift; refusing blind overwrite")
    current_template = normalized_template(props(current)["template"])
    require(current_template in [normalized_template(snapshot["head_template"]), expected_candidate(snapshot)],
            "App template belongs to another release or was modified")
    return current, weights


def candidate_owned(snapshot):
    value = revision(snapshot["candidate"])
    template = normalized_template(props(value)["template"])
    if template.get("revisionSuffix") is None:
        require(value.get("name") == snapshot["candidate"], "Candidate revision name mismatch")
        template["revisionSuffix"] = f'r{snapshot["run_id"]}'
    require(template == expected_candidate(snapshot),
            "Candidate template drift; do not mutate")
    return value
