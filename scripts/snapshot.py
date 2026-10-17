"""Read-only; its output MUST be uploaded successfully before rollout.py runs."""
import os
from pathlib import Path

from common import (app, app_id, az, cli_app, digest, image_valid, invariant, iso,
                    load, props, ready, require, revision, save, traffic, utcnow)


def main():
    require(os.environ["GITHUB_RUN_ATTEMPT"] == "1",
            "Deploy reruns are forbidden. Restore the original run or dispatch a NEW release.")
    path = Path("state") / f'baseline-{os.environ["REGION"]}.json'
    require(not path.exists(), "Never overwrite an existing baseline")
    release = load("release.json")
    require(release["repository"] == os.environ["GITHUB_REPOSITORY"], "Wrong release repository")
    require(release["run_id"] == os.environ["GITHUB_RUN_ID"], "Wrong release run")
    require(release["commit"] == os.environ["GITHUB_SHA"], "Wrong release commit")
    require(image_valid(release["image"]), "Digest required")
    value = app()
    require(props(value)["configuration"]["activeRevisionsMode"] == "Multiple", "Multiple mode required")
    ingress = props(value)["configuration"]["ingress"]
    require(ingress.get("external") and ingress.get("transport") in {"auto", "http", "http2"},
            "Example expects public HTTP ingress")
    weights = traffic(value)
    stable = [name for name, weight in weights.items() if weight == 100]
    require(len(stable) == 1, "Require exactly one explicit stable revision at 100%")
    stable = stable[0]
    baseline = revision(stable)
    require(ready(baseline), "Stable must be active, provisioned, running and Healthy")
    containers = props(baseline)["template"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "web", "Only single web container supported")
    require(image_valid(containers[0]["image"]), "Bootstrap stable must also use an immutable ACR digest")
    require(props(baseline)["template"]["scale"]["minReplicas"] >= 1, "Stable must not scale to zero")
    candidate = f'{os.environ["CONTAINER_APP"]}--r{os.environ["GITHUB_RUN_ID"]}'
    revisions = az("containerapp", "revision", "list", *cli_app())
    require(not any(r["name"] == candidate for r in revisions), "Candidate already exists; fail closed")
    save(path, {
        "schema": 1, "run_id": os.environ["GITHUB_RUN_ID"],
        "repository": os.environ["GITHUB_REPOSITORY"], "commit": os.environ["GITHUB_SHA"],
        "region": os.environ["REGION"], "app_id": app_id(), "captured_at": iso(utcnow()),
        "image": release["image"], "stable": stable, "candidate": candidate,
        "traffic": weights, "invariant": invariant(value),
        "head_template": props(value)["template"],
        "stable_template_hash": digest(props(baseline)["template"]),
        "stable_template": props(baseline)["template"],
    })
    print(f"Captured read-only baseline {path}; upload it BEFORE any write.")


if __name__ == "__main__":
    main()
