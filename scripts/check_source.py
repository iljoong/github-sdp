import json
import os
import re
import urllib.request

from common import require

run = os.environ["SOURCE_RUN_ID"]
require(re.fullmatch(r"[0-9]+", run), "Source run ID must be numeric")
repo = os.environ["GITHUB_REPOSITORY"]
request = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/actions/runs/{run}",
    headers={"Authorization": f'Bearer {os.environ["GH_TOKEN"]}',
             "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
with urllib.request.urlopen(request, timeout=30) as response:
    value = json.load(response)
require(value["repository"]["full_name"] == repo, "Wrong source repository")
require(value["path"] == ".github/workflows/release.yml" and value["event"] == "workflow_dispatch",
        "Source must be a release.yml dispatch")
require(value["head_branch"] == "main" and value["status"] == "completed", "Source must be completed main run")
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
    stream.write(f'sha={value["head_sha"]}\n')
