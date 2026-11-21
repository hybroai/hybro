# Behind a reverse proxy

Hybro serves plain HTTP on one port. To reach it from other machines over
HTTPS, put a reverse proxy such as nginx or Caddy in front of it: the proxy
holds the certificate and forwards each request to Hybro. This page says what
to set in Hybro and in the proxy, with an nginx and a Caddy example for one
proxy on the machine that runs Hybro at `https://hybro.example.com`.

## What to set

1. **`backend.proxy_hops`**: the number of reverse proxies in front of Hybro,
   1 here. With the default, 0, Hybro believes no forwarded header, so behind
   a proxy it sees every request as plain HTTP from the proxy's address: the
   sign-in cookie is not marked secure, and the delay after wrong passwords
   counts every browser as one address. With 1, it believes the scheme and
   host the proxy forwards (`X-Forwarded-Proto`, `X-Forwarded-Host`) and takes
   the browser's address from the last entry of `X-Forwarded-For`, the one the
   proxy added. With two proxies in a row, set 2, and have the inner proxy pass
   the outer one's `X-Forwarded-Proto` and `X-Forwarded-Host` on unchanged.
2. **`frontend.server_url`**: the address people open,
   `https://hybro.example.com`. Hybro refuses a name it was not given with
   421 `unknown_host`, which keeps other websites out through DNS rebinding
   ([Addresses Hybro answers to](configuration.md#addresses-hybro-answers-to)).
3. **The `Host` header**: the proxy keeps the browser's `Host`. Hybro compares
   it with the page's `Origin` before it accepts a change from the Panel. nginx
   replaces it by default, so the example sets it back. A proxy that cannot
   keep it must send a loopback address (`127.0.0.1:<port>`) as `Host`, or
   with `--listen` any IP address, and the browser's host in
   `X-Forwarded-Host`, which Hybro reads only with `backend.proxy_hops` set. A `Host` replaced with another name, such as a
   container name, is refused with 421 whatever the proxy forwards.
4. **The listener**: keep the default, `127.0.0.1`, with the proxy on the
   same machine. That is the recommended arrangement: only local processes on
   this machine reach Hybro's port. With `backend.proxy_hops` above 0 and
   `frontend.server_url` set, the loopback listener also answers to that one
   name. A proxy on another machine needs `--listen` (below).

In `~/.hybro/config.json`:

```json
{
  "version": 1,
  "backend": { "proxy_hops": 1 },
  "frontend": { "server_url": "https://hybro.example.com" }
}
```

Both are read when Hybro starts, and `proxy_hops` is part of the generated
Caddy configuration, so start Hybro again with `hybro stop` and `hybro`; the
restart button on the Settings page cannot apply it.

## Let only the proxy reach the port

With `backend.proxy_hops` set, Hybro believes the forwarded headers of any
connection to its port, not only the proxy's. A caller that reaches the port
directly, without going through the proxy, can then:

- claim HTTPS, so the sign-in cookie it receives is marked `Secure`. That
  changes only its own cookie.
- claim any client address, so the delay after its wrong passwords counts
  against an address it chose. The delay per account still applies.
- name another host in `X-Forwarded-Host`. Hybro still refuses a name it does
  not answer to (421 `unknown_host`), so this gains nothing beyond what a
  correct `Host` already allows.
- not pass the cross-site check for a browser: a web page cannot set these
  headers on another site's request, and a caller outside a browser has no
  owner's cookie.

So Hybro must be reachable only through the proxy:

- **Recommended: the proxy on the same machine, Hybro on the default
  binding.** `hybro -d` publishes the port on `127.0.0.1` only, so other
  machines cannot reach it. Local processes on this machine still can; on a
  machine you share, use the next option or keep the count at 0.

  ```bash
  hybro -d --port 3000
  ```

- **Alternative: `--listen`**, for a proxy on another machine or in another
  network. Bind Hybro to an address only the proxy can reach, such as a
  private interface, or on Linux the Docker bridge address (`172.17.0.1` by
  default; `ip -4 addr show docker0` prints it). Hybro then prints a warning at
  every start, because any address that reaches the port can forge the
  headers. Do not rely on a host firewall such as `ufw` to close a port that
  `--listen 0.0.0.0` published: Docker's published ports bypass its rules.

## nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name hybro.example.com;
    ssl_certificate     /etc/letsencrypt/live/hybro.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hybro.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        # Keep the browser's host name; nginx would send 127.0.0.1:3000.
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # Chat events and MCP answers arrive piece by piece.
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}

server {
    listen 80;
    server_name hybro.example.com;
    return 301 https://$host$request_uri;
}
```

`$proxy_add_x_forwarded_for` appends the browser's address to whatever the
browser sent; Hybro takes the last entry, so a value the browser made up is
ignored. Set `X-Forwarded-Proto` to `$scheme`, a single value: Hybro does not
believe a list such as `https, http`.

## Caddy

A Caddy in front of Hybro gets the certificate itself, keeps `Host`, sets the
three forwarded headers, and passes event streams on at once:

```caddyfile
hybro.example.com {
	reverse_proxy 127.0.0.1:3000
}
```

## Check it

Open `https://hybro.example.com` and sign in. In the browser's developer tools
the `hybro_session` cookie is marked `Secure`. If something is wrong:

| You see | Cause |
|---|---|
| 421 `unknown_host`, or a 421 with no body | `frontend.server_url` does not name this address, the proxy sends another `Host`, or, on the default binding, `backend.proxy_hops` is 0. |
| 403 `cross_site_request` on every change | The proxy replaced `Host` with an IP address and `backend.proxy_hops` is 0. |
| A cookie without `Secure` | `backend.proxy_hops` is 0, or the proxy does not send `X-Forwarded-Proto` as one value. |
| The setup or sign-in page warns about plain HTTP | The page itself was opened over `http://`. |

Client API and MCP clients use the same address, `https://hybro.example.com`
and `https://hybro.example.com/mcp`, with a client token.

With `backend.proxy_hops` above 0, the `client_ip` in Hybro's own access log
(`caddy |` lines in `hybro logs`) is the leftmost `X-Forwarded-For` entry,
which a client can write. Read the client's address from the proxy's log.
