# Shared project memory

- Company GitHub organization: hitmakers-games.
- Repositories: aof-agent, aof-infra, aof-back, aof-front, aof-play, aof-rustore,
  aof-content-manager, aof-apple.
- aof-agent and aof-infra use master directly. Pull before every push. Preserve concurrent
  edits and never force-push. Other game repositories normally use task branches from develop.
- No pull requests unless the team changes this preference explicitly.
- The agent runs in Kubernetes namespace aof-agent. Repositories/workspaces and account
  state live on persistent volumes. Task state and received Telegram history live in PostgreSQL.
- Terraform uses the existing company S3 state aof-k8s.tfstate in aof-infra/k8s/selectel.
  Do not create a separate state. Self deployment is a detached job with rollback.
- This directory is the shared, reviewed identity and nonsecret memory. Update confirmed
  facts here immediately when asked to remember them. Future designs belong under agent/sdd/.
- A chat's private details are not automatically shared project facts. Use retained history
  explicitly when relevant and authorized. Bot API cannot retrieve messages it never received.
