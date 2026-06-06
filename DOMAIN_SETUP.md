# Domain Setup with Cloudflare Tunnels

Follow these instructions to replace temporary `trycloudflare.com` URLs with a permanent, static domain (e.g., `yourproject.codes`) for your MIT project.

> Public exposure note: a Cloudflare tunnel gives you HTTPS reachability, but it does not harden
> the app's signup/login, sharing, upload, or model-key flows. Review the security checklist in
> [TODO.md](TODO.md#security--hardening-for-public-exposure) before treating this as safe for
> public internet use.

---

## 1. Get a Free Domain
If you don't own a domain, use the **GitHub Student Developer Pack**:
1.  Sign up at [education.github.com/pack](https://education.github.com/pack) using your `@mit.edu` email.
2.  Claim a free domain from **Namecheap** (.me) or **Name.com**.
3.  Register your name (e.g., `lockedin.codes`).

---

## 2. Connect to Cloudflare
1.  Create a free [Cloudflare account](https://dash.cloudflare.com/).
2.  **Add a Site**: Enter your domain and choose the **Free** plan.
3.  **Update Nameservers**: Copy the two nameservers Cloudflare gives you (e.g., `amy.ns.cloudflare.com`) and paste them into your domain registrar's "Custom DNS" settings.
4.  Wait for the status to show as **Active**.

---

## 3. Set up the Zero Trust Tunnel
1.  Go to the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
2.  Navigate to **Networks** -> **Tunnels** (or **Connectors** -> **Cloudflare Tunnels**).
3.  Click **Add a tunnel** -> **Cloudflared**.
4.  Name it (e.g., `lockedin-laptop`) and **Save**.
5.  **Install Connector**: Copy the token provided and run it on your machine:
    ```bash
    cloudflared tunnel run --token <YOUR_TOKEN>
    ```

---

## 4. Map the Domain to your App
1.  In the Tunnel settings, go to the **Published application routes** (or **Public Hostname**) tab.
2.  Click **Add a route** / **Add a public hostname**.
3.  Configure the route:
    *   **Domain**: Select your domain (`lockedin.codes`).
    *   **Service Type**: `HTTP`
    *   **URL**: `localhost:8080` (Ensure this matches your `uv run lockedin serve` port).
4.  **Save**.

---

## 5. Usage
1.  Start your app:
    ```bash
    uv run lockedin serve --port 8080
    ```
2.  Start your tunnel (if not installed as a service):
    ```bash
    cloudflared tunnel run --token <YOUR_TOKEN>
    ```
3.  Visit your site at **`https://yourdomain.codes`**.

---

## Pro Tip: Running as a Service
To keep the tunnel running in the background automatically:
```bash
sudo cloudflared service install <YOUR_TOKEN>
sudo systemctl start cloudflared
```
