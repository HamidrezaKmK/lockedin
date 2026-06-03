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
