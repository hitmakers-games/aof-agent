# Shared project memory

## База знаний команды

Перед любой работой прочитай целиком два файла:

- ./md-common/README.md
- ./md-aof/README.md

В каждом есть блок «Всегда» и таблица «Перед работой — прочитать».
Найди в таблице свою задачу и прочитай указанные файлы до того,
как менять код, контент или данные. Правила из этих файлов обязательны.

Пути указаны от корня рабочей папки текущего чата или задачи. Если локальной
копии ещё нет, прочитай файл через `memory.read` или получи репозиторий через
`git.checkout` на ветке `master`.

- Company GitHub organization: hitmakers-games.
- Before AOF edits read md-aof/product.md and md-aof/do-not-fix.md.
- Keep reusable verified knowledge in md-common (general methods) or md-aof (AOF).
  Follow md-common/knowledge.md. Agent architecture and repository workflow are in
  md-aof/agent.md. Keep this directory for identity and working requirements.
- aof-agent, aof-infra, md-common and md-aof use master directly. Pull before every
  push, preserve concurrent edits and never force-push. Game repositories normally
  use task branches from develop. No PRs unless the team explicitly asks.
- A chat's private details are not automatically shared facts. Use retained history
  only when relevant and authorized. Bot API cannot retrieve messages it never received.
