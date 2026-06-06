# Domain Setup With Cloudflare Tunnels

Use this to replace temporary `trycloudflare.com` URLs with a permanent HTTPS domain such as
`yourdomain.example` or `yourdomain.codes`.

> Public exposure note: a Cloudflare tunnel gives you HTTPS reachability, but it does not harden
> the app's signup/login, sharing, upload, or model-key flows. Review the security checklist in
> [README.md](../README.md#security) before treating this as safe for public internet use.

## 1. Get A Domain

Use any domain you control. If you are eligible for a student developer program, you can also use
one of its free domain offers.

## 2. Connect The Domain To Cloudflare

1. Create a [Cloudflare account](https://dash.cloudflare.com/).
2. Add your domain and choose the Free plan if that fits your needs.
3. Copy Cloudflare's nameservers into your domain registrar's custom DNS settings.
4. Wait until Cloudflare marks the site active.

## 3. Create A Zero Trust Tunnel

1. Open the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
2. Go to **Networks** -> **Tunnels** or **Connectors** -> **Cloudflare Tunnels**.
3. Add a Cloudflared tunnel.
4. Name it, save it, and copy the connector token.

You can test the token manually:

```bash
cloudflared tunnel run --token <YOUR_TOKEN>
```

## 4. Route The Domain To lockedin

In the tunnel's public hostname settings, add a route:

- Domain: `yourdomain.example`
- Service type: `HTTP`
- URL: `localhost:8080`

Use the same port you use for lockedin. If you changed `LOCKEDIN_PORT`, update the Cloudflare URL
to match, for example `localhost:9000`.

## 5. Run The App

```bash
uv run lockedin serve --host 127.0.0.1 --port 8080
cloudflared tunnel run --token <YOUR_TOKEN>
```

Then visit `https://yourdomain.example/`.

## 6. Optional Systemd Setup

For persistent service management, use the portable user units in `ops/`:

```bash
cp ops/lockedin.env.example ops/lockedin.env
printf '%s\n' 'CLOUDFLARE_TUNNEL_TOKEN=<YOUR_TOKEN>' > ops/tunnel.env
./ops/install-systemd-user.sh
systemctl --user enable --now lockedin-serve.service
systemctl --user enable --now lockedin-tunnel.service
```

If you use the ops monitor, set your public URL in `ops/lockedin.env`:

```bash
LOCKEDIN_PUBLIC_URL=https://yourdomain.example/
```
