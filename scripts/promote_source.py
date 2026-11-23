"""Verify a source release run and republish its release record for a resume run.

Resume mode of release.yml reuses the source run's immutable image instead of
rebuilding; the record is rebound to this run so region.yml, snapshot.py and
rollout.py (including their attempt-1 rerun guards) run unchanged.
"""
import json
import os
import re
import sys
import urllib.request

from common import image_valid, load, require, save

REGIONS = (("kr", "korea"), ("sea", "southeastasia"), ("usw", "westus"))


def github_reader(token):
    def read(path):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    return read


def verify_source(read, repository, source_run_id, start_region, current_run_id):
    require(re.fullmatch(r"[0-9]+", source_run_id or ""), "Source run ID must be numeric")
    require(source_run_id != current_run_id, "Source must be another run")
    earlier = REGIONS[:start_index(start_region)]
    run = read(f"/repos/{repository}/actions/runs/{source_run_id}")
    require(run["repository"]["full_name"] == repository, "Wrong source repository")
    require(run["path"] == ".github/workflows/release.yml", "Source must be a release.yml run")
    require(run["event"] == "workflow_dispatch", "Source must be a workflow_dispatch run")
    require(run["head_branch"] == "main", "Source must be a main run")
    require(run["status"] == "completed", "Source must be completed")
    listing = read(f"/repos/{repository}/actions/runs/{source_run_id}/jobs?filter=latest&per_page=100")
    jobs = listing["jobs"]
    require(listing["total_count"] == len(jobs), "Incomplete source jobs listing")
    for region, caller in earlier:
        matches = [job for job in jobs if job["name"] == f"{caller} / rollout"]
        require(len(matches) == 1 and matches[0]["status"] == "completed"
                and matches[0]["conclusion"] == "success",
                f"Source region {region} did not succeed in its latest attempt")
    return run["head_sha"]


def start_index(start_region):
    names = [region for region, _ in REGIONS]
    require(start_region in names, "Invalid start region")
    return names.index(start_region)


def rebind(release, *, repository, source_run_id, source_sha, run_id, commit, registry):
    require(release.get("repository") == repository, "Source release record from another repository")
    require(release.get("run_id") == source_run_id, "Source release record has wrong origin run")
    require(release.get("commit") == source_sha, "Source release record commit differs from source run")
    image = release.get("image")
    require(isinstance(image, str) and image_valid(image), "Source image must be an immutable digest")
    require(re.fullmatch(r"[a-z0-9]{5,50}", registry or "")
            and image.startswith(f"{registry}.azurecr.io/safe-demo@sha256:"),
            "Source image is not from the configured registry")
    require(re.fullmatch(r"[0-9]+", run_id) and run_id != source_run_id, "Invalid resume run")
    return {"image": image, "run_id": run_id, "repository": repository,
            "commit": commit, "source_run_id": source_run_id}


def main(argv, env=os.environ, read=None):
    command = argv[0] if argv else ""
    repository, source = env["GITHUB_REPOSITORY"], env["SOURCE_RUN_ID"]
    if command == "verify":
        read = read or github_reader(env["GH_TOKEN"])
        sha = verify_source(read, repository, source, env["START_REGION"], env["GITHUB_RUN_ID"])
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"sha={sha}\n")
    elif command == "rebind":
        save("release.json", rebind(load(argv[1]), repository=repository, source_run_id=source,
                                    source_sha=env["SOURCE_SHA"], run_id=env["GITHUB_RUN_ID"],
                                    commit=env["GITHUB_SHA"], registry=env["ACR_NAME"]))
    else:
        raise RuntimeError("Usage: promote_source.py verify | rebind <source release.json>")


if __name__ == "__main__":
    main(sys.argv[1:])
