# DEV MODE - Research-report assistant

Run `./scientist.sh` to launch a CLI report assistant for one approved bubble. The launcher:

- authenticates with `DEV_USERNAME` / `DEV_PASSWORD` from `.env`;
- lists approved bubbles by title and slug;
- prompts for `<model> <bubble-slug>`, where model is `codex`, `claude`, or `agy`;
- generates backend-scoped context for the selected bubble only;
- injects that context plus the Editing Guide into the chosen CLI.

The generated context uses the same bubble membership filter as the web chat. It includes only
papers attached to the active bubble, grouped by relevance score, and lists allowed paper paths
for deeper local reads. Unrelated assets in the user's global `ASSETS/` directory are not
included. Web research is still allowed when the user asks for new papers, recent results, or
external sources; the assistant should prioritize existing assets first and cite web findings
with links.
Paper summaries in the startup context are clipped so CLI sessions receive the operating
instructions and Editing Guide reliably; the generated context lists allowed paths for deeper
full-text/PDF reads when needed.

## Setup

```bash
cp .env.example .env
# edit DEV_USERNAME and DEV_PASSWORD to match your lockedin account
./scientist.sh
```

At the prompt, enter for example:

```text
codex diffusion-models
claude diffusion-models
agy diffusion-models
```

You can also pass the selection directly:

```bash
./scientist.sh codex diffusion-models
```

## Tooling Policy

- `claude` is launched with report file tools plus `WebSearch` and `WebFetch`.
- `codex` is launched in read-only sandbox mode with approval on request, native live web search
  enabled, and multi-agent tools disabled.
- `agy` uses the project-local `.agents/` permission hook. The launcher refreshes
  `.agents/hooks.json` with the correct absolute hook path before starting. The hook denies shell
  commands but allows agy's native web tools such as `search_web` and `read_url_content`.

All scientist sessions are instructed to work only inside `data/users/<DEV_USERNAME>/`, use only
the active bubble context, and read direct asset text/PDF files only for paper ids listed in that
context. Higher-relevance papers should be prioritized for reading, retrieval, comparison, and
citation unless the user explicitly asks otherwise. Web search is allowed for discovery and
external context; local asset confinement does not prohibit online research.

## Useful Commands

```bash
uv run lockedin devmode
uv run lockedin scientist-list
uv run lockedin scientist-list --json
uv run lockedin scientist-context <bubble-slug>
uv run lockedin editguide
```

`devmode` prints the older workspace summary for manual inspection. `scientist-context` prints
the exact generated context used by `./scientist.sh`.

## Agent Role Notes

When working as the report assistant:

1. Treat the generated active-bubble context as authoritative.
2. Edit only report Markdown under `REPORTS/<bubble-slug>/pages/`.
3. Re-read a page before editing it.
4. Read only the active bubble citation file, `REPORTS/<slug>/_lockedin_citations.md`, when
   citations are relevant.
5. Never invent BibTeX keys.
6. Describe intended edits before writing; if the CLI does not automatically prompt for approval,
   ask explicitly before editing.
