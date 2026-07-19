# iPhone Mirror — WiFi Screen Mirroring

## Project Overview

macOS desktop app die je iPhone-scherm spiegelt en bedient via WiFi.
Gebruikt pymobiledevice3 voor device discovery/screenshots en WebDriverAgent voor touch control.

## Stack

- **Taal:** Python 3.10+
- **GUI:** PyQt6
- **Device communicatie:** pymobiledevice3 (WiFi + USB fallback)
- **Touch control:** WebDriverAgent (WDA) via HTTP REST API
- **Image processing:** Pillow

---

## Globale rules actief

De volgende rules zijn globaal geladen via `~/.claude/rules/ecc/`:

- `common/` — coding style, testing, security, git workflow, agents, patterns, performance
- `web/` — frontend design, hooks, security, performance, testing (als web project)
- `typescript/` — TypeScript specifieke regels
- `python/` — Python specifieke regels
- `golang/` — Go specifieke regels
- `rust/` — Rust specifieke regels

---

## Beschikbare agents

Alle agents in `~/.claude/agents/` zijn actief:

| Agent | Gebruik voor |
|-------|-------------|
| `planner` | Feature planning voor je begint |
| `architect` | Architectuur beslissingen |
| `tdd-guide` | Tests schrijven voor code |
| `code-reviewer` | Code review na elke wijziging |
| `security-reviewer` | Security check voor commit |
| `build-error-resolver` | Build fouten oplossen |
| `refactor-cleaner` | Dead code verwijderen |
| `performance-optimizer` | Performance verbeteren |
| `silent-failure-hunter` | Verborgen fouten vinden |
| `doc-updater` | Documentatie bijhouden |
| `e2e-runner` | E2E tests schrijven/uitvoeren |
| `typescript-reviewer` | TypeScript/JS code review |
| `python-reviewer` | Python code review |
| `go-reviewer` | Go code review |
| `rust-reviewer` | Rust code review |
| `database-reviewer` | Database/SQL review |
| `a11y-architect` | Toegankelijkheid (WCAG) |

---

## Beschikbare skills (selectie)

Via `~/.claude/skills/`:

**Development:** `tdd-workflow`, `backend-patterns`, `frontend-patterns`, `api-design`, `verification-loop`, `error-handling`, `git-workflow`, `search-first`

**Testing:** `e2e-testing`, `eval-harness`, `python-testing`, `golang-testing`, `react-testing`, `rust-testing`

**Design:** `design-system`, `frontend-design-direction`, `motion-ui`, `frontend-a11y`

**Security:** `security-review`, `security-scan`

**Infra:** `deployment-patterns`, `docker-patterns`, `database-migrations`

---

## Snelle commando's

```
/plan "feature"        → planner agent
/code-review           → code-reviewer agent
/tdd                   → tdd-guide agent
/security-scan         → security-reviewer agent
/build-fix             → build-error-resolver agent
/refactor-clean        → refactor-cleaner agent
```

---

## Documentatie afspraak

- `CLAUDE.md` — context voor nieuwe sessies (dit bestand)
- `FINDINGS.md` — technisch logboek, alles wat we ontdekken

Alles wat we ontdekken schrijven we in `FINDINGS.md`.
