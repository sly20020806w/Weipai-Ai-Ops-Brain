"""Read committed Git objects for an authenticated connector upload."""

import base64
import json
import subprocess
import sys


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


if sys.argv[1] == "manifest":
    head = git("rev-parse", "HEAD").decode().strip()
    parent = git("rev-parse", "HEAD^").decode().strip()
    entries = {}
    for line in git("ls-tree", "-r", "-z", head).split(b"\0"):
        if not line:
            continue
        meta, name = line.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        entries[name.decode("utf-8")] = {"mode": mode, "type": kind, "sha": sha}
    changes = git("diff-tree", "--no-commit-id", "--name-status", "--no-renames", "-r", "-z", parent, head).split(b"\0")
    files = []
    for index in range(0, len(changes) - 1, 2):
        status, name = changes[index].decode(), changes[index + 1].decode("utf-8")
        if status == "D":
            files.append({"path": name, "mode": "100644", "type": "blob", "sha": None})
        else:
            entry = dict(entries[name], path=name)
            entry["size"] = int(git("cat-file", "-s", entry["sha"]))
            files.append(entry)
    print(json.dumps({"head": head, "parent": parent, "tree": git("rev-parse", "HEAD^{tree}").decode().strip(), "base_tree": git("rev-parse", parent + "^{tree}").decode().strip(), "message": git("log", "-1", "--format=%B").decode("utf-8"), "files": files}, ensure_ascii=True))
elif sys.argv[1] == "chunk":
    sha = sys.argv[2]
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError("Invalid blob SHA")
    data = git("cat-file", "blob", sha)
    start = int(sys.argv[3])
    print(base64.b64encode(data[start:start + 12000]).decode("ascii"))
else:
    raise ValueError("Unsupported mode")
