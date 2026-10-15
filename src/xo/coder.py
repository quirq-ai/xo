"""The xo-github template's inputs. No network or local Coder configuration lives here."""
from __future__ import annotations

import math
import re

from xo.client import XoError

SIZES = ("xo.mini", "xo.small", "xo.medium", "xo.large")


def github_parameters(url: str, *, name: str | None, ref: str, start_command: str,
                      capacity: str, workspace_size: str, home_disk_size: int,
                      wait_timeout: float) -> tuple[str, dict[str, str]]:
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?", url)
    if not match or any(p in (".", "..") for p in match.groups()):
        raise XoError("github needs an HTTPS GitHub repository URL, without credentials or a branch path; "
                      "use --ref for a branch or tag")
    repo = match[2].removesuffix(".git")
    if not repo:
        raise XoError("github needs a repository name")
    if name is None:
        name = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-")[:32].rstrip("-") or "github"
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,30}[a-zA-Z0-9])?", name):
        raise XoError("--name must be 1 to 32 letters, digits or hyphens, starting and ending with a letter or digit")
    if not 1 <= home_disk_size <= 100:
        raise XoError("--home-disk-size must be from 1 to 100 GB")
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise XoError("--wait-timeout must be a positive number of seconds")
    if not start_command.strip():
        raise XoError("--start-command must be auto, manual, or a shell command listening on port 3000")
    return name, {"repo_url": url.rstrip("/"), "repo_ref": ref, "start_command": start_command,
                  "capacity": capacity, "workspace_size": workspace_size,
                  "home_disk_size": str(home_disk_size)}
