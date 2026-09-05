# insta-parser agent skills

A portable skill document describing how to use and operate the
`insta-parser` service. It's plain Markdown with YAML frontmatter (`name`,
`description`), which is the common agent-skill convention — no tooling or
vendor lock-in, and readable as ordinary docs.

| Skill | Use for |
|---|---|
| `insta-parser/SKILL.md` | Calling the service: endpoints, the async polling flow, interpreting output, error handling |
| `insta-parser/reference/operations.md` | Running the container: rate limiting, ffmpeg/tesseract failures, slow transcription, disk usage, session login |

It lives with the app so it deploys alongside it — chezmoi applies
`private_apps/` to `~/apps/` on homelab machines, putting this at
`~/apps/insta-parser/skills/`.

## Loading it

**Claude Code** — symlink or copy into a skills directory:

```bash
mkdir -p ~/.claude/skills
ln -s ~/apps/insta-parser/skills/insta-parser ~/.claude/skills/
```

**n8n AI Agent node** — paste `SKILL.md`'s body into the system prompt, or
serve it as a text resource the agent can fetch. `reference/operations.md` is
generally not useful to a workflow agent; it's for whoever is fixing the
container.

**Any other agent** — the frontmatter `description` says when the skill
applies and the body says how. Load it however that agent takes context.

## Keeping it accurate

This describes a real running service. If you change the API surface, error
codes, or env vars in `../app/` or `../docker-compose.yaml`, update the
matching part of the skill in the same commit — a stale skill is worse than
none, because an agent will follow it confidently.
