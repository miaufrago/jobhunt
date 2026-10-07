# LinkedIn source — setup, login, and limits

LinkedIn is where postings from employers with **no public ATS feed** reach the
board: Microsoft, Meta, Alphabet, Apple, Oracle, Starbucks, Costco, Blue Origin.
Unauthenticated scraping caps out around the tenth page and returns a handful of
results per employer; an authenticated session removes that ceiling.

The session is also the most sensitive thing in this stack. Everything below is
built around protecting it.

---

## What runs

| Piece | Where | What it is |
|---|---|---|
| `linkedin-mcp` container | `192.168.1.20:8080/mcp` on `<your-macvlan-network>` | [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server), pinned by digest, holding the session |
| `jobhunt/sources/linkedin_mcp.py` | inside the jobhunt container | Calls three tools, parses the postings, hands them to the normal pipeline |
| `/srv/appdata/linkedin-mcp` | the Docker host, mode 700, uid 1000 | The Chromium profile holding the live session |

The MCP server is a **browser**, not an API client. It drives real Chromium
against LinkedIn using your logged-in profile.

---

## The one-time login

There is **no cookie to copy.** The container runs the login browser itself and
publishes a viewer you drive from your own browser. Nothing exports a credential
to a file, and the invasive `browser_import` code path — which decrypts your
desktop Chrome cookie store — never runs in a container.

```bash
IMG=stickerdaniel/linkedin-mcp-server@sha256:6fbdeb5bea31955c10d76ff202b043228c74899366836aab2beb6808c712e701

docker rm -f linkedin-mcp linkedin-mcp-login 2>/dev/null
docker run -d --name linkedin-mcp-login \
  --network <your-macvlan-network> --ip 192.168.1.20 \
  -e TIMEOUT=120000 \
  -v /srv/appdata/linkedin-mcp:/home/pwuser/.linkedin-mcp \
  $IMG --login --login-viewer

docker logs linkedin-mcp-login | grep vnc_lite
```

The log prints a URL beginning `http://127.0.0.1:6080/...`. **Replace
`127.0.0.1` with the container's address** — `192.168.1.20` — and open it from a
desktop browser. The server always prints loopback because it assumes loopback
port publishing; on `<your-macvlan-network>` the container has its own address and no published port.

Sign in normally. 2FA, CAPTCHA, and security challenges all work — it is a real
browser. You have 30 minutes. The container exits `0` and logs
`Profile saved to /home/pwuser/.linkedin-mcp/profile` when it is done; the
profile is about 25 MB.

Then start the long-lived server:

```bash
docker run -d --name linkedin-mcp --restart unless-stopped \
  --network <your-macvlan-network> --ip 192.168.1.20 \
  -e TIMEOUT=120000 -e LOG_LEVEL=INFO \
  -v /srv/appdata/linkedin-mcp:/home/pwuser/.linkedin-mcp \
  $IMG --transport streamable-http --host 0.0.0.0 --port 8080 --path /mcp
```

### `TIMEOUT=120000` is required, not optional

Without it the login fails with:

```
Profile creation failed: Page.goto: Timeout 30000ms exceeded.
  - navigating to "https://www.linkedin.com/login", waiting until "load"
```

That is Playwright's built-in 30-second navigation default. LinkedIn's login page
does not reach a full `load` event that fast in a GPU-less container. The symptom
looks exactly like a network fault; it is not. Before blaming the network, check
that a plain HTTPS request from a `br0` container succeeds — it will:

```bash
docker run --rm --network <your-macvlan-network> --ip 192.168.1.11 --entrypoint sh $IMG -c \
  'python -c "import urllib.request;print(urllib.request.urlopen(\"https://www.linkedin.com/login\",timeout=15).status)"'
```

### When the session expires

Symptom: searches return zero `job_ids`, or the Sources page shows the LinkedIn
source failing. Re-run the login block above — it overwrites the profile in place.
Sessions last months, not days.

---

## Why the login is on `<your-macvlan-network>` and not a published port

Nginx Proxy Manager runs on `<your-macvlan-network>` macvlan and **cannot reach the Docker host's own
IP**. Anything jobhunt must talk to needs its own `br0` address rather than a
published host port. That applies to this container too: jobhunt (`.42`) reaches
the MCP server (`.45`) directly, host-port publishing would not work between them.

Pick an address no container already holds. `docker inspect` is authoritative —
ping is not, and a free-looking ping cost us a failed start on `.44` (qdrant).

```bash
docker ps -a --format '{{.Names}}' | while read n; do
  docker inspect $n --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'"$n"
done | sort -t. -k4 -n
```

---

## Limits, and why they are what they are

Set under `sources.linkedin_mcp` in `config.yaml`:

| Setting | Default | Reason |
|---|---|---|
| `searches_per_run` | 6 | Bounded work per run |
| `seconds_between_searches` | 45 | Human-paced, not machine-paced |
| `seconds_between_details` | 6 | Each detail fetch is a real page load |
| `max_details_per_run` | 40 | Hard ceiling; the remainder waits for the next run |
| `max_pages` | 1 | Deep pagination is the classic scraping signature |
| `date_posted` | `past_week` | Keeps the working set small |

**Cadence is what distinguishes automation, not IP address.** The container and
your desktop share one public IP — verified, they are identical — so "it runs on
the server" is not itself a risk. What differs is that a schedule runs the same
searches forever, unattended. Do not raise these limits to catch up after a
missed run; let it catch up over the following runs instead.

Whatever the source, the run is capped by `sources.run_budget_seconds`, and
LinkedIn is in the `feeds` phase so it cannot starve the employer feeds.

---

## Tool surface: three of nineteen

The server exposes 19 tools. This integration calls three:

```
search_jobs        get_job_details        get_saved_jobs   (available, unused)
```

It never calls `send_message`, `connect_with_person`, `get_inbox`, `get_feed`,
`search_people`, or `search_posts`. `ALLOWED_TOOLS` in `linkedin_mcp.py` records
that intent. The server does not enforce it — **nothing stops a future change or
another MCP client pointed at `192.168.1.20:8080/mcp` from calling the rest.**
The endpoint has no authentication, so treat LAN access to it as equivalent to
account access.

---

## What the audit found

Audited at commit `0cd1e5f` (2026-08-23); the pinned image was built the same day,
so the audit covers exactly this build.

- **No outbound POST or PUT anywhere** in non-test code — it cannot push your data
  out.
- Outbound GETs only to LinkedIn, `pypi.org` (version check, 24h cache, 2s
  timeout), and `api.github.com` (issue *search*, only when building a bug report).
- No telemetry or analytics libraries.
- Error diagnostics are built locally and include the *path* to cookies, never
  values.
- No `eval`, `pickle`, or `os.system`. Subprocess use is confined to the
  installer, browser version handling, and local cookie import.
- Dockerfile pins its base image and `uv` by digest, uses `uv.lock --frozen`, and
  runs as non-root `pwuser`.
- **No encryption at rest** for the stored session. `/srv/appdata/linkedin-mcp`
  holds a live LinkedIn session in a Chromium profile: mode 700, never on a share.

**Re-audit before changing `LI_DIGEST`.** Tracking `:latest` would put unaudited
code in front of a live session.

---

## Running a second person's board

Sessions cannot be shared. A second instance needs its own MCP container, its own
`br0` address, its own appdata directory, and its own login — performed by that
person, in their own browser.

Set `POLITENESS_SCALE=2.0` on the second instance and stagger its cron. Both
instances share one public IP, so they share one reputation with LinkedIn.

---

## Known gap

Job titles are not known until after `get_job_details`, so some detail fetches are
spent on postings the title filter would have rejected. Prefiltering would mean
parsing titles out of the rendered search-results blob, which is brittle. Today
the cost is bounded by `max_details_per_run` instead.
