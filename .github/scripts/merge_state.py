"""Merge this run's local state.json with the just-fetched origin/main version.

Used by the "Persist updated state" workflow step when a concurrent run has
already pushed a state.json update — takes the union of both runs' seen_ids
so neither run's newly-processed posts get lost (which would otherwise risk
a duplicate repost on a later run).
"""

import json
import subprocess

with open("state.json") as f:
    local = json.load(f)

remote = json.loads(subprocess.check_output(["git", "show", "origin/main:state.json"]))

local_ids = local.get("seen_ids", [])
remote_ids = remote.get("seen_ids", [])
seen = set(local_ids)
merged_ids = local_ids + [i for i in remote_ids if i not in seen]

merged = {
    "seen_ids": merged_ids,
    "telegraph_access_token": local.get("telegraph_access_token") or remote.get("telegraph_access_token"),
}

with open("state.json", "w") as f:
    json.dump(merged, f, indent=2, ensure_ascii=False)
    f.write("\n")
