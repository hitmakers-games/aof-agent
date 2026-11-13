# AOF agent

The agent's application source, shared instructions, prompts, database migrations,
and image definitions live here. Kubernetes/Helm/Terraform definitions live in
[hitmakers-games/aof-infra](https://github.com/hitmakers-games/aof-infra), under
`k8s/agent-*` and `k8s/selectel`. Both repositories use `master`.
Temporary files and running containers are never the source of an implementation.

## Code map

| Directory | Responsibility |
| --- | --- |
| `telegram/` | Bot API, received updates/media, retained history, delivery |
| `gateway/` | Roles, commands, conversation routing, steering, Jira intake, subscriptions |
| `developer/` | Trusted developer tools: Git, Jira, attachments, Kubernetes, work and memory |
| `worker/` | Codex process adapter, conversation/background workers, supervision, sandbox checks |
| `coding/` | Older controller/executor workflow, checkout, publication and result verification |
| `operations/` | Detached build/deploy, Terraform, rollback and recovery |
| `repositories/` | GitHub App authentication and repository inventory |
| `database/` | PostgreSQL schema, explicit migrations and compatibility adapter |
| `vpn/` | Network proxy, tunnel setup and failover |
| `agent/` | Shared identity, working requirements and nonsecret project memory |

Telegram updates enter the gateway and are retained in PostgreSQL. Direct requests
run through the conversation worker and Codex adapter; trusted tools enforce the
sender's role outside the shell sandbox. Explicit background work uses the shared
queue. The older coding controller/executor remains supported; it is not a second
source tree. Detached operations build committed snapshots and report their result
after restart. See `operations/runner.py` for deployment recovery.

## Instructions and configuration

- `agent/identity.md`, `agent/requirements.md`, `agent/memory.md` are authoritative
  shared Markdown. `gateway/agent_memory.py` refreshes a runtime cache from `master`,
  falling back to the image's version during outages. Memory writes commit to GitHub.
- `coding/prompt.py`, `worker/chat.py` and `worker/background.py` construct prompts.
- `worker/requirements.toml` defines enforced Codex policy. Images copy it into
  `/etc/codex/requirements.toml`; `developer/session.py` builds per-request settings.
- Model/reasoning, roles, subscriptions and queue state are mutable database settings,
  managed through authenticated tools; they are not hidden source configuration.
- Legacy `k8s/agent-worker` in aof-infra refers to an externally provisioned
  `aof-agent-workspace-policy` ConfigMap. It was absent during the November 2026
  audit. Do not treat that chart as a complete reconstruction of the active developer
  service; active conversation definitions are in `k8s/agent-telegram`.

## Runtime locations

| Location | Contents and source |
| --- | --- |
| `/opt/aof-agent` | Image copy of this repository's application directories |
| `/etc/codex/requirements.toml` | Copy of `worker/requirements.toml` |
| `/workspace/conversations`, `/workspace/tasks`, `/workspace/repos` | Persistent Git checkouts and work in progress; changes must be committed/pushed |
| PostgreSQL | Retained Telegram messages, queue, settings and delivery/operation records; schema in `database/` |
| `/state` | Persistent authentication/account state and Codex runtime files; not source |
| `/transport` | Internal coordination/cache files and legacy SQLite path names routed through the database adapter |
| `/workspace/operations` | Generated operation snapshots/results; see `operations/client.py` |
| `/run/secrets` | Mounted credentials, managed outside Git; never copy into documentation or source |
| `/tmp` | Disposable downloads, readiness markers and tool scratch files |

External Codex binaries and bundled system skills belong to the pinned dependency,
not agent-owned source. Their installation is defined in `worker/Dockerfile`.
Other tool versions/install steps are in `worker/install-tools.sh`. No credentials,
raw conversations, database dumps or account files belong in Git.

## Builds and checks

`worker/Dockerfile` builds the full runtime; `worker/app.Dockerfile` updates application
code on a pinned runtime. Telegram, coding and database images have their own
Dockerfiles. Application images copy whole source directories so adding a module
cannot silently omit it from another service. `.dockerignore` excludes tests,
bytecode, local environments and Git metadata. Runtime dependencies still come from
the pinned base images and their build definitions.

The VPN suites require `cryptography` (provided by the VPN image; install it in
a local virtual environment for host testing). Prefer the smallest relevant check
or manual exercise; tests are not required for every change. Keep regression tests
for concrete failure risks rather than prompt wording or implementation details.
Select component suites to run in isolated processes:

```sh
python3 tools/check.py gateway worker
```

Use `python3 tools/check.py --all` only when a full check is useful, or
`python3 tools/check.py database --postgres` for database integration tests.

PostgreSQL integration tests require their explicitly designated disposable test
database; never point them at the live service. A successful test run or pushed
commit does not prove deployment. Deployment is a separate, explicitly authorized
operation and completes only when its detached result succeeds.

Group conversations choose text, a sparse reaction, or silence. Queued/running group
messages never trigger typing; text delivery emits typing only once text is ready.
Reaction-only model responses use `[[REACT:👍]]` (see `telegram/reactions.py`). The
original Telegram message ID is retained in PostgreSQL settings as
`telegram-source:<update_id>`; batched responses react to the original request.
Reactions reuse the durable outbox and its recipient permission checks, require no
schema migration, and skip unavailable reactions without sending fallback chatter.
