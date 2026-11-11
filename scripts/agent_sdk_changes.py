"""Everything that changed between the installed claude-agent-sdk and a newer one.

Prints, for an upgrade review: the installed and target SDK versions with the
CLI each bundles, the SDK's release notes in between, the SDK commits and the
files they touch (release notes are sometimes empty), and the full Claude Code
CLI changelog between the two bundled CLI versions. Nothing is filtered: read
all of it and sort each item into "fixes something we hit", "new capability we
could use", "behaviour change to verify" or "not our setup" — see
scripts/upgrade_agent_sdk.md.

    python scripts/agent_sdk_changes.py              # installed → latest on PyPI
    python scripts/agent_sdk_changes.py --to 0.2.180 --out review.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

SDK_REPO = "anthropics/claude-agent-sdk-python"
CLI_CHANGELOG = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "legal-helper-upgrade-review"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _bundled_cli(sdk_version: str) -> str:
    text = _get(f"https://raw.githubusercontent.com/{SDK_REPO}/v{sdk_version}/src/claude_agent_sdk/_cli_version.py")
    m = re.search(r'"([\d.]+)"', text)
    return m.group(1) if m else "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--to", help="target SDK version (default: latest on PyPI)")
    ap.add_argument("--out", help="also write the review to this file")
    args = ap.parse_args()

    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    cur, cur_cli = claude_agent_sdk.__version__, __cli_version__
    target = args.to or json.loads(_get("https://pypi.org/pypi/claude-agent-sdk/json"))["info"]["version"]
    out: list[str] = [f"# claude-agent-sdk {cur} → {target}", ""]
    if _vkey(target) <= _vkey(cur):
        print(f"installed {cur} is current (target {target})")
        return 0
    target_cli = _bundled_cli(target)
    out += [f"- installed: SDK {cur}, bundled Claude Code {cur_cli}",
            f"- target: SDK {target}, bundled Claude Code {target_cli}", ""]

    releases = json.loads(_get(f"https://api.github.com/repos/{SDK_REPO}/releases?per_page=50"))
    out += ["## SDK release notes", ""]
    for r in sorted(releases, key=lambda r: _vkey(r["tag_name"])):
        v = r["tag_name"].lstrip("v")
        if _vkey(cur) < _vkey(v) <= _vkey(target):
            body = (r.get("body") or "").split("\n---")[0].strip() or "(no notes)"
            out += [f"### {v} ({r['published_at'][:10]})", body, ""]

    cmp = json.loads(_get(f"https://api.github.com/repos/{SDK_REPO}/compare/v{cur}...v{target}"))
    out += ["## SDK commits", ""] + [f"- {c['commit']['message'].splitlines()[0]}" for c in cmp.get("commits", [])]
    out += ["", "## SDK files changed (src/ is what we run)", ""]
    out += [f"- {f['status']} +{f['additions']} -{f['deletions']} {f['filename']}" for f in cmp.get("files", [])]

    log = _get(CLI_CHANGELOG)
    sections = re.split(r"(?m)^## ", log)
    out += ["", f"## Claude Code CLI changelog after {cur_cli} up to {target_cli}", ""]
    for sec in sections[1:]:
        head = sec.splitlines()[0].strip()
        if re.fullmatch(r"[\d.]+", head) and _vkey(cur_cli) < _vkey(head) <= _vkey(target_cli):
            out += ["### " + sec.strip(), ""]
    text = "\n".join(out)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
