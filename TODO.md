# TODO

## Security — hardening for public exposure

`lockedin` was built for trusted/local-LAN use (see the `auth.py` module docstring), but it's now
reachable publicly via a custom domain + Cloudflare tunnel. Transport is fine (HTTPS end-to-end,
`HttpOnly`+`Secure`+`SameSite=Lax` session cookie, PBKDF2-200k password hashes). The gaps below are
at the *application* layer and matter now that signup/login are open to the internet.

- [ ] **Raise the minimum password length.** Currently `MIN_PASSWORD_LEN = 4` (`auth.py`). Four chars
      is trivially brute-forceable — bump to a sane floor (e.g. 8–12).
- [ ] **Add rate limiting / lockout on `/api/login` and `/api/signup`.** `auth.py` has none, so login
      is brute-forceable. Mitigate in-app and/or with Cloudflare WAF + rate-limiting rules on those routes.
- [ ] **Gate signup.** `/api/signup` is fully open — anyone with the URL can create an account.
      Consider an invite code / allowlist / disable-signup toggle.
- [ ] **Security review of the public `/share/<token>` routes.** Not yet audited — verify the share
      token has sufficient entropy (`secrets`-based) and that toggling `share_active` off truly revokes.
- [ ] **Security review of file/asset path handling.** PDF upload + asset serving paths not yet audited
      for path traversal / unsafe filenames.
- [ ] **Security review of model-key handling.** Confirm per-user API keys / Claude tokens aren't
      leaked in logs or to other users.

> Note: the above security items have **not** been through a full review — they're concerns raised
> from a partial read of `auth.py` and the auth/cookie paths in `server.py`. Run `/security-review`
> (or a focused audit of sharing + uploads) before treating this app as safe for genuinely public use.

## Reliability / product hardening

- [ ] **Centralize validation for route IDs that touch the filesystem.** Add shared validators for
      `pdf_id`, bubble `slug`, page slug, chat/news session IDs, and filenames before constructing
      paths. Some file-serving routes already sanitize filenames; apply the same pattern
      consistently across assets, bubbles, pages, chats, sharing, and news history.
- [ ] **Validate direct PDF uploads.** URL-based fetches enforce a 50 MB limit and verify PDF
      headers/magic bytes, but direct uploads should get the same size/type checks before writing
      `paper.pdf`.
- [ ] **Harden URL fetching against SSRF.** `/api/assets/upload-url` and Slack bare-link ingestion
      fetch arbitrary URLs. If the app is reachable publicly, block private/internal IP ranges,
      localhost, link-local addresses, and unsafe schemes before following redirects.
- [ ] **Move file-backed state to SQLite, or add file locks.** Atomic YAML writes help, but account
      changes, share-index updates, autosave, background ingest, and news sessions can still race.
      SQLite would preserve the local-first deployment model while improving consistency.
- [ ] **Add single-flight / queueing for long background work.** The news crawler and ingest tasks
      can overlap per user. Add explicit per-user locks or a small job queue so duplicate crawls,
      repeated accepts/discards, or concurrent ingest writes cannot corrupt session state.

## Frontend maintainability

- [ ] **Split the single-file frontend.** `src/lockedin/web/index.html` currently contains CSS,
      markup, and all client logic in one large file. Split it into static JS/CSS modules or a
      minimal build step so editor, chat, assets, news, sharing, and account flows can be changed
      independently.
- [ ] **Add browser smoke tests.** Use Playwright or equivalent for the core flows: signup/login,
      PDF upload, bubble approval, page autosave conflict, chat streaming, public share preview,
      and mobile layout.
- [ ] **Audit responsive UI after each major feature.** The app has mobile-specific CSS, but the
      dense editor/chat/news surfaces should be checked for text overflow, hidden controls, and
      unusable scroll regions.

## API and model integration

- [ ] **Tighten request/response schemas.** Replace loose `dict` / `list[dict]` request bodies with
      explicit Pydantic models for chat messages, model config, news instructions, saved sessions,
      and crawler events. Add length limits for user-supplied strings.
- [ ] **Improve model configuration safety.** Validate provider names, model names, base URLs, and
      auth methods before saving `active_model.yaml`; avoid persisting browser-held Claude
      subscription tokens server-side.
- [ ] **Add observability for streaming failures.** Chat/news streams currently surface many errors
      as string details. Add structured server logs with user, route, provider, model, duration,
      and terminal event type, while redacting prompts, API keys, and OAuth tokens.
- [ ] **Document live-test requirements.** The deterministic suite passes without network/LLMs, but
      live Qwen tests require Ollama and seeded PDFs. Make that split explicit in contributor docs
      and CI expectations.

## Documentation cleanup

- [ ] **Update README architecture text for the current read-only chat design.** The code no longer
      exposes report generation/edit streaming as a primary workflow; editing is manual in the
      Markdown editor or via DEV_MODE.
- [ ] **Refresh Slack docs to match current commands.** `SLACKBOT_SETUP.md` describes an
      `ask <bubble>: ...` style command, while the current bot primarily uses `select`, `list`,
      `news`, `crawl`, PDF upload/link ingestion, and active-bubble Q&A.
- [ ] **Clarify public-deployment posture.** `DOMAIN_SETUP.md` and `ops/README.md` make public
      exposure easy. Link them back to the security checklist so deploying through Cloudflare does
      not imply the app is fully hardened.

