# Background snapshots and authenticated private access

This release changes both data collection and API access. Prepare credentials and listener settings before rolling it out across a fleet. Updating code does not automatically change an existing service's bind address. The updater refuses the initial security upgrade on a wildcard listener without verified persisted trusted origins; configure an explicit listener or its allowed origins first.

## Collection contract

Each web daemon owns one persistent local scanner subprocess. A scan has an eight-second parent deadline and a twelve-second OS alarm in the child. Successful scans retain Git/transcript/model caches across cycles. The scanner reads requests from stdin and exits on EOF if the parent disappears. Optional project icon detection remains separately bounded in the parent.

The local snapshot refreshes in the background after two seconds; hub peer snapshots after three seconds. There is at most one local collection and four peer refreshes at a time, one job per host, at most 64 peer registrations and no unbounded work queue. Failed refreshes retry after five seconds. A blocked worker retains its slot until completion/reaping. Each feed response is capped at 4 MiB.

`GET /api/agents`, `/api/fleet/host` and `/api/fleet` read shared memory snapshots. They never scan tmux, transcripts, tasks or Git in the HTTP handler. HTTP request concurrency is capped at 32; excess connections receive `503` and `Retry-After: 1`.

A cold response contains no invented sessions: `snapshot.state=cold`, `available=false`, and `generated_at=null`. The browser retries warming hosts after two seconds. Returned snapshots include a revision, generation time, monotonic age, refresh state, last error, and stale flag. Last-good values remain visible for at most one hour, explicitly marked stale after failure. Expired or unavailable hosts are not treated as an empty online host, preserving saved later/inbox metadata. Peer source metadata is exposed separately as `source_snapshot`; degraded/expired source feeds do not replace a hub's last-good value.

Successful send/kill/ack/answer/new-work operations invalidate the affected host snapshot. Revision-bound acknowledgement, pane/PID verification and the owner host's authoritative state remain unchanged. Detailed previews, full messages, quota and files retain their explicit on-demand endpoints.

## Authentication contract

Every host has its own random owner key in `~/.config/tmux-attn/auth.key`, generated atomically with mode0600. `TMUX_ATTN_AUTH_KEY_FILE` can select another absolute private path; use the same setting for the service and CLI. Keys are never returned by info/health, included in URLs, or printed automatically during service startup.

Obtain the key explicitly on the relevant host:

```sh
tmux-attn auth-token
```

When preparing an upgrade before the installed CLI has this command, run `python3 attention.py auth-token` from the updated checkout. Treat the result as a credential.

Browser requests to the dashboard first show a login page. The key is exchanged over `POST /api/auth/session` for a random HttpOnly, SameSite=Strict cookie. Sessions expire after24hours, are bounded to128 per daemon, and are invalidated by restart. HTTPS sessions use Secure cookies. The key is not stored in localStorage/sessionStorage. Browser mutation requests must have an exact trusted Origin/Host, JSON Content-Type and the custom same-origin header. The dashboard adds that header automatically. Forwarded headers are not trusted to expand allowed origins.

Machine calls use `Authorization: Bearer <destination-host-owner-key>`. The same Origin/Host and JSON checks still apply; machine calls without an Origin may authenticate with Bearer. All sensitive APIs, including file/message reads and peer enrollment, require authentication. `/api/health` exposes cheap process/version information publicly; collector paths, errors and detailed snapshots are only included for authenticated requests.

## Listener choices

New installations and bare `tmux-attn web` default to127.0.0.1. For a private HTTPS ingress, keep this loopback bind and configure Tailscale Serve to proxy the local port. Explicitly allow its public HTTPS origin:

```sh
TMUX_ATTN_ALLOWED_ORIGINS='https://your-host.your-tailnet.ts.net' sh install_web_service.sh
```

Configure Serve separately according to the Tailscale CLI on that host. Keep the service private to your tailnet. Browser access keys still apply; this release does not rely on proxy-provided identity headers.

For direct private HTTP access, explicitly bind the machine's actual Tailscale IP:

```sh
TMUX_ATTN_WEB_HOST="$(tailscale ip -4)" sh install_web_service.sh
```

The numeric bind address is trusted as a direct HTTP origin. If you access it using a DNS name, add that exact origin with `TMUX_ATTN_ALLOWED_ORIGINS`. Additional origins are comma separated; wildcards, userinfo and URL paths are rejected. A new wildcard listener requires explicit allowed origins. Tailscale grants/firewall rules still determine which devices can reach this authenticated service.

## Fleet rollout

1. Update each checkout, generate that host's key, and record it privately. Use the updated checkout's `update.py` for this first rollout so it knows the new module manifest and migration checks. Do not reuse one key on all machines. Run `git pull --ff-only` in each clean checkout first; use `python3 attention.py auth-token` to prepare the key and `python3 update.py --no-fetch --ref HEAD` after configuring the listener. An old updater cannot install the new support-module manifest by itself.
2. Configure explicit listener/origin settings on each host. Keep the previous runtime available until ready.
3. On the hub, add each destination host's key as `token` in its `peers.json`, retaining existing names and URLs. Enrollment writes this file atomically with mode0600; if editing manually, apply that mode yourself.
4. Update peers and hub, then verify authenticated `/api/agents` and fleet slices. An old or misconfigured peer is reported unavailable; the hub never silently falls back to unauthenticated calls.
5. Log into each browser using the hub key. Cookies do not cross hostnames. Different ports are different Origins and must each be configured; cookies themselves are not isolated by port.

Example private hub configuration:

```json
{"peers":[{"name":"workstation","url":"http://100.64.0.10:7788","token":"<that-workstation-owner-key>"}]}
```

Peer URLs must use a private numeric address or a Tailscale `.ts.net` name, without embedded credentials, paths or redirects. Authenticated peer requests ignore ambient proxies and never follow redirects, preventing credential forwarding to another origin.

For authenticated enrollment from a client, set `SERVER_TOKEN` to the hub key and run the existing join/install command. The client sends its own local key as the peer credential. The web popover's copied command reads `TMUX_ATTN_SERVER_TOKEN` from the client environment instead of embedding a key. For local hub administration, `join_peer.py --apply` requires `PEER_TOKEN` from the destination host. The old uncredentialed SSH enrollment path is no longer invoked.

## Validation and limits

Monitor authenticated `/api/health` for pool activity, source errors and freshness. Empty-but-successfully-collected hosts are valid; a daemon without recent browser requests is not considered unhealthy. A source failure affects its own snapshot rather than the availability of other hosts or HTTP handlers.

The background scan deadline isolates filesystem stalls but does not make filesystem permissions disappear. Persistent source errors can leave the last snapshot stale. This release does not change terminal-command outcome semantics or large-file buffering; those remain separate follow-up work.
