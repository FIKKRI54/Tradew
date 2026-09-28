# Deployment templates

These files are reusable examples, not a turnkey production installer. Review every path and setting on a test host first.

1. Build `frontend/` and create a release archive without ignored runtime files.
2. Run `install-user.sh <archive>` as the unprivileged service account.
3. Edit `$HOME/.config/tradew/scan_config.json`, `llm_config.json`, and `tradew.env`.
4. Install and start the user services after verifying the MT5/Wine paths.
5. Generate a private client CA with `generate-device-certificate.sh`.
6. As root, run `install-nginx-root.sh <domain> <origin-ip> <app-user>`.

The application service reads private configuration from `$HOME/.config/tradew`; release archives contain only the public examples. Do not commit service environment files, browser profiles, certificates, tokens, account exports, or audit data.

The service templates use systemd `%h` for the service user's home directory. The MT5 bridge binds to loopback and requires `TRADEW_BRIDGE_TOKEN`. Nginx terminates TLS and requires a client certificate in the included deployment pattern.
