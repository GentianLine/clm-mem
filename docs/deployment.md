# Deployment: TLS, reverse proxy, and non-loopback use

`purslane` (the `shatterling` HTTP/MCP proxy) is a **plain-HTTP** server. It
does **not** terminate TLS or mTLS itself — it speaks HTTP on the port you give
it. For any deployment reachable beyond localhost you must put a
**TLS-terminating reverse proxy** in front of it and enable bearer-key auth.
This document describes the supported topologies with working configs.

## Invariants (enforced at startup)

These are not optional warnings — see `purslane/security.py`:

1. **Non-loopback bind without an API key refuses to start.** The proxy exposes
   sandboxed exec, file write/delete, and web fetch over HTTP; binding it to a
   network address with no auth is an unauthenticated RCE primitive (audit C-1).
2. **Loopback-only (`127.0.0.1`) without auth is the safe default** for local,
   single-machine use.
3. **Auth + non-loopback warns you to terminate TLS**, because the
   `Authorization: Bearer <key>` header would otherwise travel in cleartext.

So there are two supported shapes: bind loopback behind a proxy, or bind
non-loopback with auth *and* still front it with TLS. The first is strongly
preferred.

## Topology A (preferred): loopback app + TLS reverse proxy

Bind `clm-mem` to `127.0.0.1` and let a reverse proxy own the public, TLS
terminating endpoint. The app never sees untrusted network traffic directly.

```text
        :443 (TLS)                     :8080 (HTTP, loopback only)
client ────────►  reverse proxy  ─────────────────►  clm-mem serve
                  (nginx/caddy)                      --host 127.0.0.1
```

```bash
# app: loopback-only, auth on so the proxy must present a key (defense in depth)
clm-mem serve --use-mock \
    --host 127.0.0.1 --port 8080 \
    --api-key "${CLM_MEM_API_KEY}"
```

### Caddy (automatic Let's Encrypt TLS — simplest)

`Caddyfile`:

```caddy
clm.example.com {
    reverse_proxy 127.0.0.1:8080 {
        # /turns streams Server-Sent Events; disable buffering or the stream
        # is held until it completes and clients see no live deltas.
        flush_interval -1
    }
}
```

Caddy handles cert issuance/renewal, HTTP->HTTPS redirect, and HSTS for you.
If the proxy and app are on the same host you can also have Caddy inject the
key per request so operators never type it into a client:

```caddy
clm.example.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
        header_up Authorization "Bearer {env.CLM_MEM_API_KEY}"
    }
}
```

### nginx (manual certs / ACME via certbot)

```nginx
server {
    listen 443 ssl http2;
    server_name clm.example.com;

    ssl_certificate     /etc/letsencrypt/live/clm.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/clm.example.com/privkey.pem;

    # SSE for /turns: do not buffer or compress the stream.
    location /v1/clm/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;          # critical for streaming deltas
        proxy_cache off;
        gzip off;
        proxy_read_timeout 3600s;     # long-lived turn streams
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }
}

server {                       # redirect all HTTP to HTTPS
    listen 80;
    server_name clm.example.com;
    return 301 https://$host$request_uri;
}
```

> **Streaming gotcha.** `/v1/clm/sessions/<id>/turns` responds with
> Server-Sent Events. Any proxy in the path must turn **response buffering off**
> (nginx: `proxy_buffering off`; Caddy: `flush_interval -1`) and avoid
> compressing the stream, or clients receive the whole reply at once instead of
> live deltas.

## Topology B: non-loopback bind + auth + TLS proxy

If you need the app to listen on a network interface (e.g. container-to-container
over a private bridge), you may bind non-loopback — but **auth is mandatory**
(the server refuses to start otherwise) and you should still terminate TLS at a
proxy so the bearer key is not cleartext on the wire:

```bash
clm-mem serve --use-mock \
    --host 10.0.0.5 --port 8080 \
    --api-key "${CLM_MEM_API_KEY}"   # required for non-loopback
```

Reserve this for trusted networks; prefer Topology A whenever possible.

## mTLS (mutual TLS)

`clm-mem` does not verify client certificates. If you need **mutual** TLS, enforce
it at the reverse proxy, which is where TLS terminates:

- **Caddy:** add a `tls` block with `client_intermediates` + `trusted_ca` and set
  `client_auth` to require a valid client certificate for the location.
- **nginx:**

  ```nginx
  ssl_client_certificate     /etc/ssl/ca/internal-ca.crt;
  ssl_verify_client          on;      # or "optional" to inspect, not require
  ```

mTLS gives you network-layer identity (only holders of a CA-signed client cert
can reach the proxy). It is an *additional* layer — the app-level bearer key is
still the primary authorization for each request, so keep `--api-key` set either
way.

## CORS and the web studio behind a proxy

When the browser UI is served from the same origin as the API (the normal case:
proxy serves both `/` and `/v1/*`), CORS stays same-origin-only and no extra
config is needed. If you split the UI and API across different origins, allow the
UI origin explicitly with `--cors-origins https://ui.example.com` — never `*`
while auth is disabled.

## Checklist for a non-local deployment

- [ ] `clm-mem` bound to `127.0.0.1` (Topology A) or non-loopback **with** `--api-key`.
- [ ] TLS terminated at a reverse proxy; HTTP redirects to HTTPS.
- [ ] Reverse proxy response buffering disabled for `/v1/clm/` (SSE).
- [ ] `--api-key` / `CLM_MEM_API_KEY` set on the app and in every client.
- [ ] CORS same-origin-only, or an explicit trusted origin list (never `*`).
- [ ] (Optional) mTLS at the proxy for network-layer identity.
