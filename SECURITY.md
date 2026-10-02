# 🔐 Security

## The short version

Haunt Director is a **home appliance for a trusted network.** It has **no login**. Anyone who can reach it can play scenes, fire the fogger, change settings and reboot the Pi. That's convenient on your own Wi-Fi and a bad idea anywhere else.

- ✅ Use it on your home network.
- ✅ To check on the show from elsewhere, use a private VPN like [Tailscale](https://tailscale.com) or WireGuard.
- ❌ **Don't port-forward it** to the internet, and don't put it on a guest or public network.

## What it does to protect you anyway

- **Cross-site writes are refused.** Browsers stamp an `Origin` on requests one site makes to another. Any change request (POST, PUT, DELETE) arriving with a foreign `Origin` gets a 403, so a random web page a guest opens on your Wi-Fi can't quietly fire your fogger. Cross-site *reading* isn't allowed either (no permissive CORS). Scripts on your own network, like `curl`, send no `Origin` and still work. If you really need a frontend on another origin, list it in `HD_CORS_ORIGINS`.
- **Disruptive actions ask twice** in the UI, and setup fixes that restart things are refused while the show is armed.
- **Root access is written down, command by command.** The System tab needs `sudo` for a short list of things: restart FPP and its web services, restart this app, reboot, update three specific FPP config files, and add a systemd override. If your user doesn't already have passwordless sudo, the installer adds exactly those commands (exact files, no wildcard paths) in [`deploy/sudoers.d/haunt-director`](deploy/sudoers.d/haunt-director). Be clear-eyed about it, though: anyone who can write a systemd override can run anything as root, so **treat the app's user as root-equivalent**. Prefer not to? Install with `--no-sudoers` and do the fixes by hand.
- **Your API key stays on the Pi.** The optional AI key lives in `backend/.env`, which git ignores. It's sent only to the provider you chose, and only when you press Generate.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (the repository's *Security* tab → *Report a vulnerability*) rather than a public issue. Include steps to reproduce and what you think the impact is. This is a hobby project, so replies may take a few days. 🎃
