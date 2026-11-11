# Upgrading claude-agent-sdk (the `claude_code` engine)

The SDK bundles the Claude Code CLI, and nearly every release is a new CLI
(one every two or three days). The CLI decides how our turns behave: the loop,
WebFetch, permissions, resume, compaction, the event stream we translate, the
plan-usage answer we read through a private control request. So the version is
pinned in `requirements.txt`, and an upgrade is a small, staged procedure.

**When:** once a month with the harness review, and sooner when the changelog
fixes something we have hit (a failed turn, a resume problem) or a security
issue in a tool on our allowlist (Bash, WebFetch, Read/Write/Edit).

## 1. Read everything that changed

    python scripts/agent_sdk_changes.py --to <version> --out docs/sdk_upgrades/<date>_<from>_to_<to>.md

It prints the SDK release notes, the SDK commits and files (notes are often
empty), and the whole CLI changelog between the two bundled CLIs. Do not
filter by keyword. Read it all and sort every item that touches this setup
into: **fixes for problems we hit or can hit**, **new capabilities we can
use**, **behaviour changes to verify**. Say in one line what the rest is
(desktop, cloud, gateway, …). Keep the sorted note in `docs/sdk_upgrades/`;
the previous notes show what was decided and why.

## 2. Stage the new version beside the live one

    pip download --no-deps -d <stage>/wheels claude-agent-sdk==<version>
    pip install --no-deps --target <stage>/sdk-<version> <stage>/wheels/claude_agent_sdk-<version>-*.whl

Compare the wheel's `Requires-Dist` with the installed one first; if they
differ, stage in a copy of the env instead. The live server is untouched.

## 3. Offline: contract tests on both versions

    python -m pytest -q tests/test_claude_code_contract.py
    PYTHONPATH=<stage>/sdk-<version> python -m pytest -q tests/test_claude_code_contract.py
    PYTHONPATH=<stage>/sdk-<version> python -m pytest -q tests

They build our options, the legal MCP server, parse a CLI stream with the
SDK's own parser through our translator, and read a recorded plan-usage
answer. A failure here is a code change to make before switching.

## 4. Live, zero quota: the handshake check on both versions

    python scripts/agent_sdk_check.py --out <stage>/check_<old>.json
    PYTHONPATH=<stage>/sdk-<version> python scripts/agent_sdk_check.py --out <stage>/check_<new>.json

Starts the CLI with a real turn's options and sends only control requests
(server info, MCP status, context usage, plan usage, `auth status`). No model
call. Diff the two printed reports: the legal tool count, context overhead,
compaction threshold, login and its expiry must be what you expect.

## 5. Smoke turns — these use plan quota: ask the user first

On the isolated test server with the staged SDK on `PYTHONPATH`, two short
turns in one chat: one that calls a legal tool, then a follow-up (resume).
Check the answer, the tool rows, the follow-up resumed the session. Skip it
when steps 3–4 pass and the changelog holds no change to the loop or the
stream; the first real turn then is the smoke test, and rollback is quick.

## 6. Switch, verify, record

When nobody is using the server (no running chat run, no recent turn):

    pip install claude-agent-sdk==<version>        # into the llm env
    # set the same version in requirements.txt
    # restart serve on :8010
    python scripts/agent_sdk_check.py              # now on the live env

Upgrade and restart together: a running server would start the new CLI on
its next turn while its Python half is still the old SDK.

**Rollback:** `pip install claude-agent-sdk==<previous>`, restore the pin,
restart.

Add the result lines (tests, check diff, smoke) to the upgrade note.
