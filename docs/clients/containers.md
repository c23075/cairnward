# Clients in containers, with no network of their own (FR-ISO-4)

Server-style clients (Open WebUI, LibreChat) can run in containers whose only way out is Cairnward. Templates: `deploy/containers/open-webui/compose.yaml` and `deploy/containers/librechat/docker-compose.override.yml`.

## How it works
```
browser ──► 127.0.0.1:3000 ──► ui (socat) ──► client container ──(internal network only)
                                                  │ 127.0.0.1:8484 (cairnward-link, same network namespace)
                                                  ▼
                              <state>/clients/gateway.sock ──► Cairnward gateway
```
- The client container is on a Docker/Podman network with `internal: true`: no internet, no route to this machine.
- `cairnward-link` shares the client's network namespace and connects `127.0.0.1:8484` there to Cairnward's **client socket**, `<state folder>/clients/gateway.sock`. `cairnward serve` creates it (owner-only, in its own folder, so the management socket is never mounted). Requests look local to the client, so Cairnward's Host check accepts them, and they still need the client's API key.
- `ui` publishes the web interface on 127.0.0.1 only; it is the only container on a normal network and only relays to the client's web port.

## Notes
- Pin image versions in `.env` (tags, or better digests); the templates refuse to start without them.
- Rootless Podman maps the container's root to your user, so the owner-only socket works as is. With rootful Docker, the container's root can open it too; that is what the mount is for.
- If Cairnward restarts, the socket is recreated in the same folder; the folder (not the file) is mounted, so the link keeps working.
- Plugins that fetch from the internet (web search, URL readers) simply fail inside such a container.
- Desktop clients (Jan) run on the host: they can't be confined this way; see [jan.md](jan.md).
