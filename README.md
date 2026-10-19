# Mandipay Deposit Console (hosted build)

Single-file stdlib Python console for the Mandipay UPI payin flow. This build is
the Render/mobile variant of the local console: same UI, same two fire modes,
but configured entirely through environment variables and reachable from a phone.

## What changed vs. the local build

| Area | Local build | This build |
|---|---|---|
| Bind | `127.0.0.1:8777` | `0.0.0.0:$PORT` |
| Credentials | embedded constants | env vars only |
| Outbound HTTP | host network | residential proxy (`PROXY_URL`) |
| UI access | localhost only | HTTP Basic auth gate |
| Health | none | `GET /healthz` (unauthenticated) |
| Layout | desktop | mobile breakpoint, 16px inputs (no iOS zoom) |

## Deploy on Render

1. Push this folder to a GitHub repo.
2. Render → **New → Web Service** → connect the repo.
3. Runtime **Python 3**, build `pip install -r requirements.txt`,
   start `python app.py`, health check path `/healthz`, region **Singapore**.
4. Add the environment variables below, then deploy.

`render.yaml` in the repo can also drive the whole service via **New → Blueprint**.

## Environment variables

| Key | Required | Purpose |
|---|---|---|
| `APP_USER` / `APP_PASS` | yes | Basic-auth gate for the UI. Unset = open (do not run open on the internet). |
| `API_USER` / `API_PASS` | yes | API-realm identity used for token minting. |
| `MERCHANT_USER` | yes | Blank-password merchant used for the merchant session. |
| `KEYS_JSON` | yes | Merchant key table, `{"MID":"SALT"}` or `[{"mid":..,"salt":..}]`. |
| `DEFAULT_MID` | no | Fallback MID when an order carries none. |
| `PROXY_URL` | yes | Indian residential proxy, e.g. `http://user:pass_country-in_city-gurgaon_session-abc_lifetime-168h@geo.iproyal.com:12321`. |
| `LOGIN_SALT` | no | Login derivation salt (default `TEST_SALT`). |
| `HOST` / `PORT` | no | Default `0.0.0.0` / Render's `$PORT`. |
| `LOG_PATH` | no | Operator action log, defaults to `/tmp/mandipay_ops/console_ops_98sd5.jsonl`. |

The proxy must be set: Render's own egress IP is a US/EU datacenter address and
the auth service is geo-gated. With the proxy in place every outbound call
(`internal.mandipay.com`, the raw gateway, `upi.mandipay.com`) leaves from the
Indian residential IP, and the UI stays reachable from anywhere.

## Operator flow

1. **Fetch** — paste the fresh deposit link (or its JSON). The console resolves
   requestId, txnId, amount, MID and the payin record ID by itself.
2. **Mode A — via Mandipay** — submit UTR, then fire the merchant callback.
   The gateway requires a 12+ digit UTR for this path.
3. **Mode B — direct to receiver** — sign the payload with the merchant salt
   from `KEYS_JSON` and POST it straight to the callback URL resolved from the
   order. Mandipay's own gateway record stays pending by design.

A `200` from the receiver is an unconditional ack, not proof of wallet credit.
Confirm in the player's wallet before treating a deposit as settled.

## Local run

```bash
cp .env.example .env          # fill it in
set -a; . ./.env; set +a
python3 app.py                # http://127.0.0.1:8777
```

## Files

- `app.py` — the whole console (server + UI)
- `render.yaml` — Render blueprint
- `Procfile` — `web: python app.py`
- `requirements.txt` — stdlib only
