# Shared project memory

- Company GitHub organization: hitmakers-games.
- Read md-common/README.md and md-aof/README.md at the start of work, then relevant
  topics. Before AOF edits read md-aof/product.md and md-aof/do-not-fix.md.
- Keep reusable verified knowledge in md-common (general methods) or md-aof (AOF).
  Follow md-common/knowledge.md. Agent architecture and repository workflow are in
  md-aof/agent.md. Keep this directory for identity and working requirements.
- aof-agent, aof-infra, md-common and md-aof use master directly. Pull before every
  push, preserve concurrent edits and never force-push. Game repositories normally
  use task branches from develop. No PRs unless the team explicitly asks.
- A chat's private details are not automatically shared facts. Use retained history
  only when relevant and authorized. Bot API cannot retrieve messages it never received.
