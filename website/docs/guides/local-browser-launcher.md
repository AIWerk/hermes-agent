# Local browser launcher over reverse SSH

Hermes can connect its browser tools to a visible Chromium-family browser running on the user's own Linux desktop. The desktop opens reverse SSH tunnels to the agent host. Hermes only sees localhost endpoints on the agent host.

This is useful for Rocky or VPS agents where the browser should appear on the user's Ubuntu desktop, not inside the VPS.

## Security boundary

- Do not use the user's main browser profile.
- CDP must bind to loopback only: `127.0.0.1`, never `0.0.0.0`.
- The user desktop initiates SSH. The agent host does not get general shell access to the desktop.
- The launcher HTTP server binds only to desktop localhost and is exposed to the agent host only as a reverse tunnel on host localhost.
- The control endpoints require a shared bearer token, so other local processes on the agent host cannot drive the browser even over loopback. Only `/health` stays unauthenticated as a liveness probe.
- The launcher only supports: `/health`, `/status`, `/open` or `/up`, `/down` or `/close`.
- No credential capture, password-store access, 2FA/CAPTCHA/payment/order-submit automation.
- The user stays in control for sensitive actions.
- This is a separate, visible browser window/profile, not hidden remote desktop.

## Hermes config

In the tenant Hermes profile, enable the launcher:

```yaml
browser:
  local_launcher:
    enabled: true
    launcher_port: 18765
    launcher_url: http://127.0.0.1:18765
    cdp_port: 9222
    cdp_url: http://127.0.0.1:9222
    ssh_target: user@agent.example
    ssh_port: 22
    ssh_identity_file: ~/.ssh/tenant-browser-tunnel
    launcher_token: <paste the desktop launcher token here>
    browser_profile_dir: ~/.hermes/rocky-browser
    browser_binary: ""
    start_url: about:blank
    cdp_poll_timeout_s: 20
```

`launcher_token` must match the token the desktop launcher uses (see below). Without it Hermes gets `HTTP 401` from every control call.

Do not put tenant hosts, usernames, or key paths into core Hermes defaults.

## Linux desktop setup

Install the helper on the user desktop:

```bash
install -Dm755 scripts/local-browser-connector/linux/rocky-browser.sh ~/.local/bin/rocky-browser
rocky-browser launcher-install
```

Edit the generated env file:

```bash
chmod 600 ~/.config/rocky-browser.env
$EDITOR ~/.config/rocky-browser.env
```

Minimum values:

```bash
ROCKY_BROWSER_SSH_TARGET=user@agent.example
ROCKY_BROWSER_SSH_PORT=22
ROCKY_BROWSER_SSH_IDENTITY_FILE=$HOME/.ssh/tenant-browser-tunnel
ROCKY_BROWSER_CDP_PORT=9222
ROCKY_BROWSER_VPS_PORT=9222
ROCKY_BROWSER_LAUNCHER_PORT=18765
ROCKY_BROWSER_PROFILE_DIR=$HOME/.hermes/rocky-browser
ROCKY_BROWSER_START_URL=about:blank
# Optional: ROCKY_BROWSER_BIN=/usr/bin/chromium
# Optional: ROCKY_BROWSER_LAUNCHER_TOKEN=  (blank = auto-generate)
```

If you leave `ROCKY_BROWSER_LAUNCHER_TOKEN` blank, the launcher generates one on first start and stores it (mode 600) at `~/.hermes/rocky-browser-launcher.token`. Copy that value into the Hermes profile as `browser.local_launcher.launcher_token`:

```bash
cat ~/.hermes/rocky-browser-launcher.token
```

Start the user service:

```bash
systemctl --user daemon-reload
systemctl --user enable rocky-browser-launcher.service
systemctl --user start rocky-browser-launcher.service
systemctl --user status rocky-browser-launcher.service
```

The service does not open a browser automatically. It only keeps the localhost launcher reverse tunnel alive.

## Use from Hermes

Open and connect in one step:

```text
/browser launch
```

Or:

```text
/browser connect --launch
```

Hermes calls `http://127.0.0.1:18765/open`, waits for CDP at `http://127.0.0.1:9222`, then connects the browser tools.

Close the local browser and CDP tunnel:

```text
/browser close-local
```

Existing behavior remains available:

```text
/browser connect http://127.0.0.1:9222
/browser disconnect
/browser status
```

If no launcher is configured, `/browser connect` keeps the previous local CDP behavior. If the launcher is configured but unreachable, Hermes prints:

```text
systemctl --user start rocky-browser-launcher.service
```

and falls back to the normal connect path where possible.

## Troubleshooting

### Port 22 vs 22222

Set `ROCKY_BROWSER_SSH_PORT` to the SSH port exposed by the agent host. Some deployments use `22`; Rocky staging may use `22222`. A wrong port makes the reverse tunnel fail before Hermes sees the launcher.

### Too many authentication failures

Use a dedicated key and force it:

```bash
ROCKY_BROWSER_SSH_IDENTITY_FILE=$HOME/.ssh/tenant-browser-tunnel
```

The helper uses `ssh -o IdentitiesOnly=yes -i "$ROCKY_BROWSER_SSH_IDENTITY_FILE"`.

### Missing key

Check:

```bash
test -f ~/.ssh/tenant-browser-tunnel
chmod 600 ~/.ssh/tenant-browser-tunnel
```

Then restart:

```bash
systemctl --user restart rocky-browser-launcher.service
journalctl --user -u rocky-browser-launcher.service -n 80 --no-pager
```

### No VPS listener

From the agent host:

```bash
curl -fsS http://127.0.0.1:18765/health
```

If this fails, the desktop launcher service or its reverse SSH tunnel is not up. `/health` is unauthenticated; the action endpoints are not.

### HTTP 401 from the launcher

A `401 unauthorized` means the token in the Hermes profile does not match the desktop launcher. On the desktop, read the active token and copy it into `browser.local_launcher.launcher_token`:

```bash
cat ~/.hermes/rocky-browser-launcher.token
```

If you set `ROCKY_BROWSER_LAUNCHER_TOKEN` explicitly in the env file, use that value instead and restart the service.

### CDP not ready

From the agent host:

```bash
curl -fsS http://127.0.0.1:9222/json/version
```

If `/health` works but CDP does not, the browser did not start, the CDP port differs, or the CDP reverse tunnel failed. On the desktop inspect:

```bash
rocky-browser status
journalctl --user -u rocky-browser-launcher.service -n 120 --no-pager
sed -n '1,120p' ~/.hermes/logs/rocky-browser.log
sed -n '1,120p' ~/.hermes/logs/rocky-browser-tunnel.log
```
