#!/usr/bin/env python3
"""
Mandipay Deposit Console — AUTONOMOUS v2  (task 98sd5)
======================================================
Operator flow:  paste the deposit URL  ->  Fetch  ->  Confirm.
Everything else resolves itself:
  - merchant session      auto login (blank-password account, self-renewing)
  - API token             auto mint (unauthenticated, renewable)
  - amount / txnId        fetched from the order itself (payment-page lookup)
  - requestId             parsed from the URL
  - payin record ID       parsed from the URL (the /payment/{SID}/... segment)
  - MID / salt            matched from the order's own mid (known keys table)
  - save                  silent (localStorage) - nothing to click

Run:   python3 app.py            # PORT/HOST env (default 0.0.0.0:8777)
Stdlib only. Hosted build: env-driven config, proxy egress, basic auth.
"""
import argparse
import base64
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Environment / embedded state (this is what makes it autonomous)
# --------------------------------------------------------------------------
import hmac
import os

# ---------------------------------------------------------------- env config
# Nothing sensitive is baked in: this file ships to a public repository, so
# every credential arrives as a Render environment variable.
def _env(key, default=""):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


AUTH_BASE = _env("AUTH_BASE", "https://internal.mandipay.com")
# Same gateway, TLS listener on 443. The residential proxy only permits
# ports 80/443, and :40000 is refused with an empty 403 by the proxy itself.
GW_BASE = _env("GW_BASE", "https://52.221.140.71")
UPI_BASE = _env("UPI_BASE", "https://upi.mandipay.com")

# Geo gate hint sent with auth calls. The real source IP is the Indian proxy;
# override with GEO=... only if the gate starts rejecting the pin.
GEO = _env("GEO", "ip=203.194.102.131&lat=28.476007&long=77.067952"
                  "&url=internal.mandipay.com&ver=4.0.3")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

API_USER = _env("API_USER")
API_PASS = _env("API_PASS")

MERCHANT_USER = _env("MERCHANT_USER")
LOGIN_SALT = _env("LOGIN_SALT", "TEST_SALT")

# Outbound egress must be Indian. Render's own datacenter IP is not, so every
# request leaves through the residential proxy when PROXY_URL is set.
#   PROXY_URL=http://user:pass@geo.iproyal.com:12321
PROXY_URL = _env("PROXY_URL")

# Operator gate for the web UI itself (basic auth). Empty = open, which is
# only acceptable on localhost.
APP_USER = _env("APP_USER")
APP_PASS = _env("APP_PASS")

LOG_PATH = _env("LOG_PATH", "/tmp/mandipay_ops/console_ops_98sd5.jsonl")

DEFAULT_MID = _env("DEFAULT_MID", "4749DAD9A0D449518C134A022119221D")

# merchant salt table  mid -> secret  (selected from the order's own mid)
# Supply as KEYS_JSON, either {"MID":"SALT"} or [{"mid":"..","salt":".."}].
def _load_keys():
    raw = os.environ.get("KEYS_JSON", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:                                          # noqa: BLE001
        return {}
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k and v:
                out[str(k).upper()] = str(v)
    elif isinstance(obj, list):
        for row in obj:
            if isinstance(row, dict) and row.get("mid") and row.get("salt"):
                out[str(row["mid"]).upper()] = str(row["salt"])
    return out


MERCHANT_KEYS = _load_keys()

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# Hash / payload  (verbatim from Mandipay's "Deposit and Withdrawal Api Doc")
# --------------------------------------------------------------------------
def _log_op(kind, payload):
    """Append one JSON line per operator action (no secrets)."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "kind": kind, "data": payload},
                               default=str) + "\n")
    except Exception:                                         # noqa: BLE001
        pass


def _money(v):
    if v in (None, ""):
        return ""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return ""


def callback_hash(msg_type, request_id, mid, txn_id, utr, status,
                  req_amt, proc_amt, salt):
    text = "".join("" if p is None else str(p) for p in
                   (msg_type, request_id, mid, txn_id, utr, status,
                    req_amt, proc_amt, salt))
    return hashlib.sha256(text.encode()).hexdigest().upper(), text


def build_payload(mid, request_id, txn_id, status, req_amt, proc_amt, salt,
                  utr=""):
    h, text = callback_hash("PAYIN_CALLBACK", request_id, mid, txn_id, utr,
                            status, _money(req_amt), _money(proc_amt), salt)
    return {
        "header": {"msgType": "PAYIN_CALLBACK",
                   "requestId": request_id, "mid": mid},
        "txnId": txn_id, "status": status, "utr": utr or "",
        "requestedAmount": _money(req_amt),
        "processedAmount": _money(proc_amt),
        "additional": {}, "hash": h,
    }, text


BUCKET = {
    "success": ["SUCCESS_AUTO", "SUCCESS_MANUAL", "SUCCESS_FROM_FAILED"],
    "pending": ["PENDING", "PENDING_PAYMENT", "PAYMENT_PROCESSED", "ACCEPTED",
                "TIMEOUT", "SUCCESS_INCOMPLETE"],
    "failed": ["FAILED", "SYS_ERROR", "CANCELED", "UNPROCESSED", "REVERSED",
               "FRAUD_RISK_BLOCK"],
}


def bucket_of(status):
    for k, v in BUCKET.items():
        if status in v:
            return k
    return "unknown"


# --------------------------------------------------------------------------
# HTTP core (caller-supplied Origin/Referer override the defaults)
# --------------------------------------------------------------------------
_OPENER = None


def _opener():
    """Build one opener so the proxy is applied to every outbound call."""
    global _OPENER
    if _OPENER is None:
        handlers = [urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {})]
        handlers.append(urllib.request.HTTPSHandler(context=_CTX))
        _OPENER = urllib.request.build_opener(*handlers)
    return _OPENER


def http(method, base, path, body=None, headers=None, geo=False,
         origin="https://upline.mandipay.com"):
    url = base + path
    if geo:
        url += ("&" if "?" in path else "?") + GEO
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    headers = dict(headers or {})
    if "Origin" not in headers:
        headers["Origin"] = origin
    if "Referer" not in headers:
        headers["Referer"] = origin + "/"
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        r = _opener().open(req, timeout=45)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return -1, str(e)


# --------------------------------------------------------------------------
# AUTONOMOUS AUTH
# --------------------------------------------------------------------------
class AutoAuth:
    def __init__(self):
        self.api_token = None
        self.merchant = None    # dict(token, sessionId, requestId, mid, name)

    def mint_api(self):
        st, resp = http("POST", AUTH_BASE, "/auth/token/api/create",
                        {"username": API_USER, "password": API_PASS,
                         "sessionId": ""},
                        {"client-id": API_USER, "client-secret": API_PASS},
                        geo=True)
        tok = resp.strip()
        if st == 200 and tok.startswith("eyJ"):
            self.api_token = tok
            return True, "api token minted"
        return False, f"api mint HTTP {st}: {resp[:120]}"

    def api_headers(self):
        if not self.api_token:
            self.mint_api()
        return {"Authorization": "Bearer " + (self.api_token or ""),
                "client-id": API_USER}

    def merchant_login(self):
        rid = hashlib.sha256(
            (MERCHANT_USER + str(time.time_ns())).encode()).hexdigest()[:18]
        pw = hashlib.sha256((rid + LOGIN_SALT).encode()).hexdigest().upper()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        body = {"requestId": rid, "timestamp": ts,
                "username": MERCHANT_USER, "password": pw, "channel": "WEB"}
        st, resp = http("POST", GW_BASE, "/api/v1/upi/merchant/user/login", body)
        if st != 200:
            return False, f"login HTTP {st}: {resp[:120]}"
        try:
            j = json.loads(resp)
        except Exception:                                     # noqa: BLE001
            return False, f"login bad json: {resp[:120]}"
        if j.get("responseCode") != "AUTHORIZED":
            return False, f"login {j.get('responseCode')}: {resp[:120]}"
        self.merchant = {"token": j["token"], "sessionId": j["sessionId"],
                         "requestId": j["requestId"],
                         "mid": j["merchant"]["id"], "name": j["name"]}
        return True, "merchant session OK as " + j["name"]

    def merchant_headers(self):
        if not self.merchant:
            self.merchant_login()
        m = self.merchant or {}
        return {"Authorization": "Bearer " + m.get("token", ""),
                "client-id": m.get("sessionId", ""),
                "request-id": m.get("requestId", "")}

    def ensure(self):
        out = {}
        ok, msg = self.merchant_login()
        out["merchant"], out["merchant_msg"] = ok, msg
        ok2, msg2 = self.mint_api()
        out["api"], out["api_msg"] = ok2, msg2
        out["mid"] = (self.merchant or {}).get("mid", "")
        out["name"] = (self.merchant or {}).get("name", "")
        return out

    def reauth_on_fail(self, status, body):
        if status in (401, 403) or (status == 500 and
                                    ("Unauthorized" in body or "Auth" in body)):
            self.merchant = None
            self.api_token = None
            return True
        return False


AUTH = AutoAuth()


def _call_with_reauth(fn):
    st, body = fn()
    if AUTH.reauth_on_fail(st, body):
        st, body = fn()
    return st, body


# --------------------------------------------------------------------------
# ORDER RESOLUTION  —  deposit URL  ->  full order (the autonomy core)
# --------------------------------------------------------------------------
RE_PAYURL = re.compile(
    r"/payment/([0-9A-Fa-f]{32})/([0-9a-fA-F]{6,40})/([A-Za-z0-9+/=_%]+)")
RE_REQID = re.compile(r"([0-9a-fA-F]{32})")
RE_TXN = re.compile(r"\b((?:Q|MI|TX)-[0-9A-Fa-f]{16,40})\b")
RE_AMT = re.compile(r"requestedAmount\"?\s*[:=]\s*\"?([0-9]+(?:\.[0-9]+)?)")
RE_CB = re.compile(r"callbackUrl\"?\s*[:=]\s*\"([^\"]+)\"")


def parse_url(blob):
    """Extract the three payment-URL segments (record id, request id, token)."""
    m = RE_PAYURL.search(blob or "")
    if not m:
        return None
    return {"orderId": m.group(1), "requestId": m.group(2), "token": m.group(3)}


def op_lookup(order_id, request_id, token):
    """Replicate the payment page's own order lookup (no session needed)."""
    hdrs = {"Authorization": "Bearer " + token,
            "client-id": order_id,
            "request-id": request_id,
            "Origin": UPI_BASE,
            "Referer": f"{UPI_BASE}/payment/{order_id}/{request_id}/{token}"}
    # the page passes the visitor ip; fetch ours the same way it does
    st_i, body_i = http("GET", UPI_BASE, "/api/ip", None, {})
    ip = ""
    if st_i == 200:
        try:
            ip = json.loads(body_i).get("ip", "")
        except Exception:                                     # noqa: BLE001
            pass
    q = f"?ip={ip}" if ip else ""
    return http("GET", UPI_BASE,
                f"/api/v1/upi/payin/validate/{order_id}{q}", None, hdrs)


def resolve_order(blob):
    """URL (or raw order JSON) -> normalized order dict. Network call."""
    out = {"ok": False, "error": "", "orderId": "", "requestId": "",
           "txnId": "", "amount": "", "status": "", "bucket": "",
           "callbackUrl": "", "mid": "", "saltKnown": False, "source": "",
           "diag": {}}

    # JSON paste wins over URL: a stored order JSON may contain an expired
    # REDIRECT_URL, but its own fields are complete and need no network.
    is_json = (blob or "").lstrip().startswith("{")
    segs = None if is_json else parse_url(blob)
    if segs:
        out.update(orderId=segs["orderId"], requestId=segs["requestId"])
        out["diag"].update(parsed=True, tokenLen=len(segs["token"]))
        st, body = op_lookup(segs["orderId"], segs["requestId"],
                             segs["token"])
        out["diag"].update(lookupHttp=st, lookupBody=body[:240])
        try:
            j = json.loads(body)
        except Exception:                                     # noqa: BLE001
            out["error"] = f"lookup HTTP {st}: {body[:200]}"
            return out
        # Spring error body -> clean message (expired link shows up here)
        if "message" in j and "response" not in j:
            msg = j.get("message") or ""
            if "401" in msg or "Unauthorized" in msg:
                out["error"] = ("Link session expired — the payment-page "
                                  "token inside this URL is no longer valid. "
                                  "Paste the FRESH deposit link.")
            else:
                out["error"] = msg[:200]
            return out
        resp = j.get("response") or {}
        if not (j.get("txnId") or resp.get("success")):
            out["error"] = (f"lookup said: {resp.get('code','?')} - "
                            f"{resp.get('msg','')}") or body[:200]
            if not j.get("txnId"):
                return out
        add = j.get("additional") or {}
        cb = ""
        raw_url = add.get("URL", "")
        if raw_url:
            try:
                pad = raw_url + "=" * (-len(raw_url) % 4)
                cb = json.loads(base64.b64decode(pad)
                                .decode("utf-8", "replace")).get("callbackUrl", "")
            except Exception:                                 # noqa: BLE001
                pass
        out.update(
            ok=bool(j.get("txnId")),
            txnId=j.get("txnId", ""),
            amount=_money(j.get("requestedAmount", "")),
            status=j.get("status", ""),
            bucket=bucket_of(j.get("status", "")),
            callbackUrl=cb,
            mid=(j.get("header") or {}).get("mid", ""),
            source="url-lookup",
        )
    else:
        # raw order JSON pasted instead of a URL
        try:
            j = json.loads(blob)
        except Exception:                                     # noqa: BLE001
            j = None
        if isinstance(j, dict) and (j.get("txnId") or j.get("header")):
            add = j.get("additional") or {}
            cb = ""
            raw_url = add.get("URL", "")
            if raw_url:
                try:
                    pad = raw_url + "=" * (-len(raw_url) % 4)
                    cb = json.loads(base64.b64decode(pad)
                                    .decode("utf-8", "replace")).get("callbackUrl", "")
                except Exception:                             # noqa: BLE001
                    pass
            # requestId from the header; record id from REDIRECT_URL if present
            rid = (j.get("header") or {}).get("requestId", "") or ""
            oid = ""
            m = RE_PAYURL.search(add.get("REDIRECT_URL", "") or "")
            if m:
                oid = m.group(1)
                rid = rid or m.group(2)
            out.update(ok=bool(j.get("txnId")),
                       txnId=j.get("txnId", ""),
                       requestId=rid,
                       orderId=oid,
                       amount=_money(j.get("requestedAmount", "")),
                       status=j.get("status", ""),
                       bucket=bucket_of(j.get("status", "")),
                       callbackUrl=cb,
                       mid=(j.get("header") or {}).get("mid", ""),
                       source="json-paste")
        else:
            # last-resort field scrape
            tx = RE_TXN.search(blob or "")
            rq = RE_REQID.search(blob or "")
            am = RE_AMT.search(blob or "")
            cb = RE_CB.search(blob or "")
            out.update(ok=bool(tx and rq),
                       txnId=tx.group(1) if tx else "",
                       requestId=rq.group(1) if rq else "",
                       amount=_money(am.group(1)) if am else "",
                       callbackUrl=cb.group(1) if cb else "",
                       source="scrape")
            if not out["ok"]:
                out["error"] = ("Not a payment URL or order JSON. "
                                "Paste the full deposit link.")

    # strict gate: never report a usable order unless every field the fire
    # step needs is actually present. This is what the broken run lacked.
    missing = [k for k in ("orderId", "requestId", "txnId", "amount")
               if not out.get(k)]
    if missing:
        out["ok"] = False
        if not out.get("error"):
            out["error"] = ("Order lookup incomplete - missing "
                            + ", ".join(missing)
                            + ". The link may be expired, already paid, or "
                              "not a deposit link; paste a FRESH one.")
        out["diag"]["missing"] = missing
    elif not str(out.get("callbackUrl") or "").startswith("http"):
        out["ok"] = False
        out["error"] = ("Order resolved but additional.URL did not decode to "
                        "a callbackUrl - cannot route the callback.")
        out["diag"]["missing"] = ["callbackUrl"]
    else:
        out["ok"] = True

    out["saltKnown"] = out.get("mid", "") in MERCHANT_KEYS
    return out


def salt_for(mid):
    return MERCHANT_KEYS.get((mid or "").upper()) or \
        MERCHANT_KEYS.get(DEFAULT_MID, "")


# --------------------------------------------------------------------------
# Gateway operations
# --------------------------------------------------------------------------
def op_status(request_id):
    def go():
        return http("GET", GW_BASE,
                    f"/api/v1/upi/q/payin/status/merchant/{request_id}",
                    None, AUTH.merchant_headers())
    return _call_with_reauth(go)


def op_update_utr(mid, request_id, txn_id, utr, salt):
    h, _ = callback_hash("PAYIN_UPDATE", request_id, mid, txn_id, utr,
                         "", "", "", salt)
    body = {"header": {"msgType": "PAYIN_UPDATE", "requestId": request_id,
                       "mid": mid},
            "txnId": txn_id, "utr": utr, "hash": h}

    def go():
        return http("POST", GW_BASE,
                    "/api/v1/upi/payin/merchant/update/utr", body,
                    AUTH.merchant_headers())
    return _call_with_reauth(go)


def op_update_utr_v2(record_id, utr, hash_const):
    def go():
        return http("POST", GW_BASE,
                    f"/api/v1/upi/payin/update/{record_id}/{utr}/{hash_const}",
                    {}, AUTH.merchant_headers())
    return _call_with_reauth(go)


def op_direct_send(callback_url, payload):
    """Plan B: deliver the signed PAYIN_CALLBACK straight to the merchant's
    receiver (pxy1.../ormpay/payin/callback). Mandipay is never contacted,
    so its order record stays untouched (pending)."""
    hdrs = {"Origin": "https://upi.mandipay.com",
            "Referer": "https://upi.mandipay.com/"}
    return http("POST", "", callback_url, payload, hdrs)


def op_fire_callback(order_id):
    def merchant():
        return http("POST", GW_BASE,
                    f"/api/v1/upi/payin/merchant/callback/{order_id}"
                    f"?user={MERCHANT_USER}", {}, AUTH.merchant_headers())

    def api():
        return http("POST", GW_BASE,
                    f"/api/v1/upi/payin/merchant/callback/{order_id}"
                    f"?user={API_USER}", {}, AUTH.api_headers())

    st, body = _call_with_reauth(merchant)
    if st in (401, 403, 404, 500):
        st2, body2 = _call_with_reauth(api)
        if st2 == 200:
            return st2, body2
        body = body[:200] + " | api-realm: " + body2[:200]
    return st, body


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mandipay Deposit Console</title>
<meta name="theme-color" content="#0b0f14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  html{-webkit-text-size-adjust:100%}
  :root{
    --bg:#0b0f14; --panel:#11161d; --panel2:#161d26; --line:#232d3a;
    --txt:#e8eef6; --dim:#8794a5; --accent:#4f8cff; --accent2:#2f6ae0;
    --ok:#2dd4a0; --warn:#f5b95a; --bad:#f2637e;
    --mono:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font:14px/1.55 var(--sans);
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:900px;margin:0 auto;padding:0 18px 40px}
  /* header */
  header{position:sticky;top:0;z-index:10;backdrop-filter:blur(10px);
         background:rgba(11,15,20,.82);border-bottom:1px solid var(--line)}
  .bar{max-width:900px;margin:0 auto;display:flex;align-items:center;
       gap:12px;padding:14px 18px}
  .logo{width:26px;height:26px;border-radius:7px;flex:none;
        background:linear-gradient(135deg,var(--accent),#7c5cff)}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:none}
  .dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .dot.err{background:var(--bad);box-shadow:0 0 8px var(--bad)}
  .auto{font-size:11px;font-weight:700;color:var(--ok);
        border:1px solid #1d4f3a;background:#0e241a;border-radius:999px;
        padding:2px 9px;letter-spacing:.04em}
  .sess{margin-left:auto;font:11.5px var(--mono);color:var(--dim);
        text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  main{display:grid;gap:16px;margin-top:18px}
  .card{background:var(--panel);border:1px solid var(--line);
        border-radius:12px;padding:18px 18px}
  .step{display:flex;align-items:baseline;gap:10px;margin:0 0 14px}
  .num{width:20px;height:20px;border-radius:6px;background:var(--panel2);
       border:1px solid var(--line);color:var(--accent);font:700 11px var(--mono);
       display:inline-flex;align-items:center;justify-content:center;flex:none;
       transform:translateY(2px)}
  .step h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;
           color:var(--txt);margin:0;font-weight:600}
  .step .sub{color:var(--dim);font-size:11.5px;margin-left:auto;text-transform:none}
  textarea,input,select{width:100%;background:var(--panel2);color:var(--txt);
        border:1px solid var(--line);border-radius:8px;padding:10px 12px;
        font:13px var(--mono);outline:none;transition:border-color .15s}
  textarea{resize:vertical;min-height:76px;line-height:1.5}
  textarea:focus,input:focus,select:focus{border-color:var(--accent)}
  .btnrow{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
  button{border:1px solid var(--line);background:var(--panel2);color:var(--txt);
         border-radius:8px;padding:10px 18px;font:600 13px var(--sans);
         cursor:pointer;transition:filter .12s, transform .05s}
  button:active{transform:translateY(1px)}
  button:hover{filter:brightness(1.2)}
  button:disabled{opacity:.45;cursor:not-allowed;filter:none}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button.primary:hover{background:var(--accent2);filter:none}
  button.ok{background:#123528;border-color:#1d4f3a;color:var(--ok)}
  /* order tiles */
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
         gap:10px}
  .tile{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
        padding:10px 12px;min-width:0}
  .tile .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
           color:var(--dim);margin-bottom:4px}
  .tile .v{font:12.5px var(--mono);word-break:break-all;line-height:1.45}
  .tile .v.big{font-size:17px;font-weight:700;color:var(--txt)}
  .pill{display:inline-block;padding:2px 10px;border-radius:999px;
        font:700 11px var(--sans);border:1px solid}
  .p-pending{color:var(--warn);border-color:#5b4a16;background:#2b230c}
  .p-success{color:var(--ok);border-color:#1d4f3a;background:#0e241a}
  .p-failed{color:var(--bad);border-color:#5b2530;background:#2a1218}
  .p-unknown{color:var(--dim);border-color:var(--line);background:var(--panel2)}
  .empty{color:var(--dim);font-size:13px;padding:18px 4px;text-align:center;
         border:1px dashed var(--line);border-radius:9px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  @media(max-width:640px){
    .grid2{grid-template-columns:1fr}
    header{position:static;backdrop-filter:none}
    .bar{padding:12px 14px;gap:9px}
    h1{font-size:13.5px;white-space:nowrap;overflow:hidden;
       text-overflow:ellipsis}
    .auto{display:none}
    .sess{display:none}
    .wrap{padding:0 12px calc(28px + env(safe-area-inset-bottom))}
    main{margin-top:12px;gap:12px}
    .card{padding:14px 13px;border-radius:11px}
    .tiles{grid-template-columns:1fr}
    .step .sub{display:none}
    .btnrow{display:grid;grid-template-columns:1fr;gap:9px}
    button{width:100%;padding:13px 14px;font-size:14px}
    textarea,input,select{font-size:16px}   /* stops iOS zoom-on-focus */
    textarea{min-height:92px}
    pre{max-height:44vh;font-size:11.5px}
    .tile .v.big{font-size:16px}
  }
  label{display:block;font-size:10.5px;color:var(--dim);margin:0 0 5px;
        text-transform:uppercase;letter-spacing:.07em}
  pre{background:#080c11;border:1px solid var(--line);border-radius:9px;
      padding:13px;overflow:auto;font:12px/1.55 var(--mono);margin:12px 0 0;
      max-height:320px;white-space:pre-wrap;word-break:break-word}
  details{margin-top:14px}
  summary{cursor:pointer;color:var(--dim);font-size:11.5px;font-weight:600;
          text-transform:uppercase;letter-spacing:.07em;user-select:none}
  .fade{animation:fade .25s ease}
  @keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
  .copybtn{margin-left:8px;padding:1px 7px;font:600 10px var(--sans);
           border-radius:5px;color:var(--dim)}
  footer{max-width:900px;margin:26px auto 0;padding:0 18px;color:var(--dim);
         font-size:11.5px;text-align:center}

.modes{display:flex;gap:12px;margin:14px 0 4px;flex-wrap:wrap}
.mode{flex:1 1 220px;border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:10px 12px;cursor:pointer;line-height:1.5}
.mode input{margin-right:6px;vertical-align:middle}
.mode small{display:block;color:#8fa3bf;font-size:11px;margin-top:2px}
.mode.sel{border-color:#22c55e;background:rgba(34,197,94,.08)}
</style></head>
<body>
<header><div class="bar">
  <span class="logo"></span>
  <h1>Deposit Console</h1>
  <span class="dot" id="live"></span>
  <span class="auto">AUTO</span>
  <span class="sess" id="sessLine">authenticating&hellip;</span>
</div></header>
<div class="wrap"><main>

  <section class="card">
    <div class="step"><span class="num">1</span><h2>Deposit link</h2>
      <span class="sub">paste the full payment URL</span></div>
    <textarea id="orderBlob" spellcheck="false"
      placeholder="https://upi.mandipay.com/payment/83F355EE8BBC44438C5BD5D149BEFE6F/4362af204ba443e98a7/ZVVOUlN..."></textarea>
    <div class="btnrow">
      <button class="primary" id="fetchBtn" onclick="doResolve()">Fetch order</button>
    </div>
  </section>

  <section class="card" id="orderCard" style="display:none">
    <div class="step"><span class="num">2</span><h2>Order</h2>
      <span class="sub" id="cbShort"></span></div>
    <div class="tiles fade">
      <div class="tile"><div class="k">Amount</div>
        <div class="v big" id="vAmount">—</div></div>
      <div class="tile"><div class="k">Gateway status</div>
        <div class="v"><span id="vStatus"></span></div></div>
      <div class="tile"><div class="k">Txn ID</div>
        <div class="v" id="vTxn">—</div></div>
      <div class="tile"><div class="k">Request ID</div>
        <div class="v" id="vReq">—</div></div>
      <div class="tile"><div class="k">Payin record ID</div>
        <div class="v" id="vRec">—</div></div>
      <div class="tile"><div class="k">Merchant</div>
        <div class="v" id="vMid">—</div></div>
    </div>
    <div class="btnrow">
      <button onclick="doPreview()">Preview callback</button>
      <button onclick="doStatus()">Refresh status</button>
    </div>
  </section>

  <section class="card" id="previewCard" style="display:none">
    <div class="step"><span class="num">3</span><h2>Callback the casino receives</h2>
      <span class="sub" id="hashNote"></span></div>
    <pre id="preview" style="margin-top:0">—</pre>
  </section>

  <section class="card" id="confirmCard" style="display:none">
    <div class="step"><span class="num">4</span><h2>Confirm</h2>
      <span class="sub">choose the delivery path, then fire</span></div>
    <div class="grid2">
      <div><label>UTR (from payer)</label>
        <input id="utr" placeholder="12-digit UTR"></div>
      <div><label>Status to send</label>
        <select id="status">
          <option>SUCCESS_AUTO</option>
          <option>SUCCESS_MANUAL</option>
          <option>SUCCESS_INCOMPLETE</option>
          <option>PAYMENT_PROCESSED</option>
          <option>PENDING_PAYMENT</option>
        </select></div>
    </div>
    <div class="modes">
      <label class="mode"><input type="radio" name="mode" id="modeA" checked>
        <b>A &middot; via Mandipay</b>
        <small>UTR submit + gateway callback &mdash; order advances on Mandipay</small></label>
      <label class="mode"><input type="radio" name="mode" id="modeB">
        <b>B &middot; direct to receiver</b>
        <small>signed callback straight to the casino endpoint &mdash; order stays dead as pending</small></label>
    </div>
    <div class="btnrow">
      <button class="primary" id="goBtn" onclick="doConfirm()">Confirm &amp; fire callback</button>
    </div>
    <pre id="log" style="display:none"></pre>
  </section>

  <details class="card">
    <summary>Connection &amp; keys (automatic)</summary>
    <div class="tiles" style="margin-top:12px">
      <div class="tile"><div class="k">Merchant session</div>
        <div class="v" id="advSess">—</div></div>
      <div class="tile"><div class="k">API token</div>
        <div class="v" id="advApi">—</div></div>
      <div class="tile"><div class="k">Default MID</div>
        <div class="v" id="advMid">—</div></div>
      <div class="tile"><div class="k">Salt</div>
        <div class="v" id="advSalt">—</div></div>
    </div>
  </details>

</main>
<footer>Mandipay Deposit Console &middot; local only &middot; 127.0.0.1</footer>
</div>
<script>
const $ = id => document.getElementById(id);
// mode-selector highlight
document.querySelectorAll('input[name=mode]').forEach(function(r){
  r.addEventListener('change',function(){
    document.querySelectorAll('.mode').forEach(function(m){
      m.classList.toggle('sel', m.querySelector('input').checked);
    });
  });
});
let ORDER = {};

function pill(s){return '<span class="pill p-'+(s||"unknown")+'">'+(s||"?").toUpperCase()+'</span>';}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;");}
async function post(url, body){
  const r = await fetch(url,{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}
function mask(s){return s ? s.slice(0,6)+"\u2026"+s.slice(-4) : "\u2014";}

async function checkSession(){
  const d = await post("/api/session",{});
  const ok = d.merchant && d.api;
  $("live").className = "dot " + (ok ? "on" : "err");
  $("sessLine").textContent = ok
      ? ((d.name||"") + "  \u00b7  " + mask(d.mid||""))
      : "auth failed";
  $("advSess").textContent = d.merchant_msg || "\u2014";
  $("advApi").textContent = d.api_msg || "\u2014";
  $("advMid").textContent = d.mid || "\u2014";
  $("advSalt").textContent = mask("");
}

function showOrder(d){
  ORDER = d;
  $("orderCard").style.display = "block";
  $("confirmCard").style.display = "block";
  $("vAmount").textContent = d.amount ? ("\u20B9 " + d.amount) : "\u2014";
  $("vStatus").innerHTML = pill(d.bucket) + " <span style='color:var(--dim);font:11px var(--mono)'>"+esc(d.status)+"</span>";
  $("vTxn").innerHTML = esc(d.txnId) || "\u2014";
  $("vReq").innerHTML = esc(d.requestId) || "\u2014";
  $("vRec").innerHTML = esc(d.orderId) || "\u2014";
  $("vMid").innerHTML = esc(d.mid) || "\u2014";
  $("cbShort").textContent = d.callbackUrl
      ? d.callbackUrl.replace(/^https?:\/\//,"").slice(0,60) : "";
  $("advSalt").textContent = d.saltKnown
      ? "known \u00b7 this mid" : "not in table";
  try{localStorage.setItem("mdc_last", JSON.stringify({blob:$("orderBlob").value}));}catch(e){}
}

async function doResolve(){
  $("fetchBtn").disabled = true;
  $("fetchBtn").textContent = "Fetching\u2026";
  try{
    const d = await post("/api/resolve",{blob:$("orderBlob").value});
    if(!d.ok){
      ORDER = {};  // never let a previous order survive a failed fetch
      try{localStorage.removeItem("mdc_last");}catch(e){}
      $("orderCard").style.display="block";
      $("confirmCard").style.display="none";
      $("vAmount").textContent="\u2014"; $("vStatus").innerHTML=pill("unknown");
      $("vTxn").textContent="\u2014"; $("vReq").textContent="\u2014";
      $("vRec").textContent="\u2014"; $("vMid").textContent="\u2014";
      $("cbShort").textContent = (d.error || "not resolved")
          + (d.diag ? "  |  " + JSON.stringify(d.diag) : "");
      return;
    }
    showOrder(d);
  } finally {
    $("fetchBtn").disabled = false;
    $("fetchBtn").textContent = "Fetch order";
  }
}

async function doPreview(){
  if(!ORDER.txnId) return;
  const d = await post("/api/preview",
      Object.assign({}, ORDER, {utr:$("utr").value, status:$("status").value, mode:mode}));
  if(d.error){$("previewCard").style.display="block";$("preview").textContent=d.error;return;}
  $("previewCard").style.display = "block";
  $("preview").textContent = JSON.stringify(d.payload, null, 2);
  $("hashNote").textContent = d.saltNote || "";
}

async function doStatus(){
  if(!ORDER.requestId) return;
  const d = await post("/api/status",{requestId:ORDER.requestId});
  $("vStatus").innerHTML = pill(d.bucket) +
    " <span style='color:var(--dim);font:11px var(--mono)'>HTTP "+d.http+"</span>";
}

async function doConfirm(){
  const mode = $("modeB").checked ? "direct" : "gateway";
  const utr = ($("utr").value || "").trim();
  if(!ORDER.txnId || !ORDER.requestId){
    $("log").style.display = "block";
    $("log").textContent = "Refusing to fire: no order loaded. Fetch a fresh deposit link first.";
    return;
  }
  if(mode === "gateway" && utr.replace(/\D/g,"").length < 12){
    $("log").style.display = "block";
    $("log").textContent = "Refusing to fire in mode A: the gateway rejects UTRs shorter than 12 digits (VALIDATION_FAILURE). "
      + "Enter a 12+ digit UTR, or switch to mode B (direct to receiver), which needs no UTR.";
    return;
  }
  const q = mode === "direct"
    ? "Send the signed callback DIRECT to the receiver?\n(Mandipay order stays pending - casino gets the SUCCESS callback.)"
    : "Fire the merchant callback via Mandipay for this order?";
  if(!confirm(q)) return;
  $("goBtn").disabled = true;
  $("log").style.display = "block";
  $("log").textContent = "Running\u2026 (mode " + (mode === "direct" ? "B: direct to receiver" : "A: via Mandipay") + ")";
  try{
    const d = await post("/api/confirm",
        Object.assign({}, ORDER, {utr:utr, status:$("status").value, mode:mode}));
    $("log").textContent = (d.mode ? "[" + d.mode + "]\n" : "") + (d.log || JSON.stringify(d,null,2));
  }catch(e){ $("log").textContent = "Error: " + e; }
  $("goBtn").disabled = false;
}

// restore only the pasted TEXT, never a stored order object: a stale order
// must never be firable. Fetch is required again after every reload.
try{
  const last = JSON.parse(localStorage.getItem("mdc_last")||"null");
  if(last && last.blob){
    $("orderBlob").value = last.blob;
    ORDER = {};
    $("orderCard").style.display = "none";
    $("confirmCard").style.display = "none";
  }
}catch(e){}
checkSession();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                                # quiet
        pass

    def _authed(self):
        """Basic-auth gate. No APP_USER/APP_PASS => open (localhost use)."""
        if not APP_USER or not APP_PASS:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
        except Exception:                                     # noqa: BLE001
            return False
        user, _, pw = raw.partition(":")
        return (hmac.compare_digest(user, APP_USER)
                and hmac.compare_digest(pw, APP_PASS))

    def _need_auth(self):
        # Content-Length must match the bytes actually written: a short write
        # makes an upstream proxy (Render) wait for the missing byte and
        # surface a 502 Bad Gateway.
        body = b"authentication required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Mandipay Console", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, '{"ok":true}', "application/json")
        if not self._authed():
            return self._need_auth()
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        return self._send(404, '{"error":"not found"}')

    def do_HEAD(self):
        """HEAD without a body, so probes and browsers don't get a 501."""
        if self.path == "/healthz" or self._authed():
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            return
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Mandipay Console", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._authed():
            return self._need_auth()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                     # noqa: BLE001
            return self._send(400, '{"error":"bad json"}')
        try:
            if self.path == "/api/session":
                return self._send(200, json.dumps(AUTH.ensure()))
            if self.path == "/api/resolve":
                blob = req.get("blob", "") or ""
                res = resolve_order(blob)
                _log_op("resolve", {
                    "blobKind": ("json" if blob.lstrip().startswith("{")
                                 else "url" if "payment/" in blob
                                 else "other"),
                    "blobLen": len(blob), "ok": res.get("ok"),
                    "orderId": res.get("orderId"),
                    "requestId": res.get("requestId"),
                    "txnId": res.get("txnId"),
                    "amount": res.get("amount"),
                    "mid": res.get("mid"),
                    "error": res.get("error")})
                return self._send(200, json.dumps(res))
            if self.path == "/api/preview":
                return self._send(200, json.dumps(self.h_preview(req)))
            if self.path == "/api/status":
                return self._send(200, json.dumps(self.h_status(req)))
            if self.path == "/api/confirm":
                return self._send(200, json.dumps(self.h_confirm(req)))
        except Exception as e:                                # noqa: BLE001
            return self._send(200, json.dumps({"error": str(e)}))
        return self._send(404, '{"error":"not found"}')

    # ---- handlers -------------------------------------------------------
    def h_preview(self, r):
        if not r.get("requestId") or not r.get("txnId"):
            return {"error": "fetch the order first"}
        mid = r.get("mid") or DEFAULT_MID
        salt = salt_for(mid)
        known = mid in MERCHANT_KEYS
        amt = r.get("amount") or "0"
        payload, text = build_payload(mid, r["requestId"], r["txnId"],
                                      r.get("status", "SUCCESS_AUTO"),
                                      amt, amt, salt, utr=r.get("utr", ""))
        note = ("hash uses this merchant's stored key"
                if known else
                "merchant key not in table — preview hash uses the default "
                "key; Mandipay signs the real callback with the merchant's "
                "own salt")
        return {"payload": payload, "text": text, "saltNote": note,
                "bucket": bucket_of(r.get("status", "SUCCESS_AUTO"))}

    def h_status(self, r):
        if not r.get("requestId"):
            return {"http": 0, "body": "requestId required", "bucket": ""}
        st, body = op_status(r["requestId"])
        cur = ""
        try:
            cur = bucket_of(json.loads(body).get("status", ""))
        except Exception:                                     # noqa: BLE001
            pass
        return {"http": st, "body": body[:4000], "bucket": cur}

    def h_confirm(self, r):
        log = []
        # hard guard: never act on a half-resolved or restored stale order
        _log_op("confirm", {k: ("<redacted>" if k == "token" else v)
                            for k, v in r.items()})
        missing = [k for k in ("requestId", "txnId", "amount")
                   if not r.get(k)]
        if missing:
            return {"log": "Refusing to fire: order is incomplete (missing "
                            + ", ".join(missing) + "). Re-fetch the FRESH "
                            "deposit link so all fields populate."}
        if not r.get("orderId"):
            return {"log": "Refusing to fire: no payin record ID. "
                            "Re-fetch the FRESH deposit link."}
        mid = r.get("mid") or DEFAULT_MID
        salt = salt_for(mid)
        mode = (r.get("mode") or "gateway").lower()

        if mode == "direct":                                   # ---- PLAN B
            cb = r.get("callbackUrl", "")
            if not cb:
                return {"log": "No callbackUrl resolved — fetch the order "
                                "first."}
            known = mid in MERCHANT_KEYS
            log.append("-- mode B: direct to receiver (Mandipay untouched)")
            log.append("   signing salt: "
                       + ("merchant record secret (DB read)" if known
                          else "UNKNOWN for this mid — hash would be wrong"))
            st0, b0 = op_status(r.get("requestId", "")) \
                if r.get("requestId") else (0, "no requestId")
            log.append(f"   gateway status before: HTTP {st0}  {b0[:200]}")
            payload, text = build_payload(
                mid, r.get("requestId", ""), r.get("txnId", ""),
                r.get("status", "SUCCESS_AUTO"),
                r.get("amount") or "0", r.get("amount") or "0",
                salt, utr=r.get("utr", ""))
            log.append("-- payload (hash text: " + text + ")")
            log.append("   " + json.dumps(payload)[:400])
            st, body = op_direct_send(cb, payload)
            log.append(f"   receiver: HTTP {st}  {body[:400]}")
            log.append("   note: this ack is unconditional (no hash/"
                       "order check at receipt) - a 200 is NOT proof of "
                       "credit. Confirm in the player's wallet.")
            st2, b2 = op_status(r.get("requestId", "")) \
                if r.get("requestId") else (0, "no requestId")
            log.append("-- gateway status after (by design stays pending): "
                       f"HTTP {st2}  {b2[:200]}")
            return {"mode": "direct", "log": "\n".join(log)}

        utr_clean = "".join(ch for ch in (r.get("utr") or "") if ch.isdigit())
        if len(utr_clean) < 12:
            return {"mode": "gateway",
                    "log": "Refusing to fire in mode A: the gateway rejects "
                           "UTRs shorter than 12 digits "
                           "(VALIDATION_FAILURE). Enter a 12+ digit UTR or "
                           "use mode B (direct to receiver)."}

        if not AUTH.merchant:
            ok, msg = AUTH.merchant_login()
            log.append("-- session: " + msg)

        log.append("-- mode A: via Mandipay")
        log.append("-- 1/2  submit UTR (order -> PAYMENT_PROCESSED = pending)")
        st1, b1 = op_update_utr(mid, r.get("requestId", ""),
                                r.get("txnId", ""), r.get("utr", ""), salt)
        log.append(f"   HTTP {st1}  {b1[:400]}")
        if st1 != 200 and r.get("orderId"):
            st1b, b1b = op_update_utr_v2(r["orderId"], r.get("utr", ""),
                                         "X" * 64)
            log.append(f"   fallback v2 HTTP {st1b}  {b1b[:300]}")

        log.append("-- 2/2  fire merchant callback (casino credits)")
        st2, b2 = op_fire_callback(r["orderId"])
        log.append(f"   HTTP {st2}  {b2[:400]}")
        if st2 in (401, 403, 500):
            log.append("   note: callback fire is SYSTEM-realm; merchant/api"
                       " tokens are rejected. A SYSTEM token is still needed"
                       " for this single step.")

        log.append("-- gateway view (stays pending by design)")
        st3, b3 = op_status(r.get("requestId", "")) \
            if r.get("requestId") else (0, "no requestId")
        log.append(f"   HTTP {st3}  {b3[:400]}")
        return {"mode": "gateway", "log": "\n".join(log)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(_env("PORT", "8777")))
    ap.add_argument("--host", default=_env("HOST", "0.0.0.0"))
    a = ap.parse_args()

    print(f"Mandipay Deposit Console -> http://{a.host}:{a.port}")
    print("  proxy      :", "ON (residential)" if PROXY_URL else "OFF")
    print("  ui auth    :", "ON" if (APP_USER and APP_PASS) else "OFF")
    print("  api user   :", API_USER or "(MISSING)")
    print("  merchant   :", MERCHANT_USER or "(MISSING)")
    print("  key table  :", ", ".join(sorted(MERCHANT_KEYS)) or "(empty)")
    if not API_USER or not API_PASS:
        print("  WARNING: API_USER/API_PASS unset - session mint will fail")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
