"""The two small web pages emailed links fall back to when the mobile app isn't installed.

``/verify-email?token=...`` and ``/reset-password?token=...`` finish the job in the browser
by calling the same JSON API the app uses. Nothing from the request is ever written into
the HTML on the server (the token is read by the page's script and removed from the address
bar), so there is nothing to inject into. Every response gets a fresh CSP nonce.
"""

from __future__ import annotations

import html
import secrets

_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#14181f;--muted:#5b6472;--line:#dfe3e8;--accent:#0b6bcb;--ok:#13795b;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--text:#eef1f5;--muted:#9aa4b2;--line:#2a313b;--accent:#5aa9ff;--ok:#4cc9a0;--bad:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}
h1{margin:0 0 8px;font-size:1.35rem}
p{margin:0 0 16px;color:var(--muted)}
.brand{font-weight:700;letter-spacing:.02em;margin-bottom:18px}
label{display:block;font-size:.9rem;margin:14px 0 6px}
input{width:100%;padding:12px;font:inherit;color:var(--text);background:transparent;border:1px solid var(--line);border-radius:10px}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button,.btn{display:block;width:100%;margin-top:18px;padding:13px;font:inherit;font-weight:600;text-align:center;text-decoration:none;color:#fff;background:var(--accent);border:0;border-radius:10px;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
.btn.secondary{color:var(--accent);background:transparent;border:1px solid var(--line)}
.status{margin:0 0 4px;font-weight:600}
.ok{color:var(--ok)}.bad{color:var(--bad)}
[hidden]{display:none!important}
"""

_COMMON_SCRIPT = """
var params = new URLSearchParams(window.location.search);
var token = params.get("token");
// Take the one-time token out of the address bar and browser history.
history.replaceState(null, "", window.location.pathname);
var root = document.querySelector("main");
var scheme = root.getAttribute("data-scheme");
function show(id) {
  ["working", "success", "failure", "form"].forEach(function (name) {
    var el = document.getElementById(name);
    if (el) el.hidden = name !== id;
  });
}
function say(id, text) { document.getElementById(id).textContent = text; }
function post(path, body) {
  var timer = setTimeout(function () {
    var slow = document.getElementById("slow");
    if (slow) slow.hidden = false;
  }, 4000);
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).finally(function () { clearTimeout(timer); });
}
var openApp = document.getElementById("open-app");
if (openApp && scheme && token) {
  openApp.href = scheme + "://" + window.location.pathname.replace(/^\\//, "") + "?token=" + encodeURIComponent(token);
  openApp.hidden = false;
}
"""

_VERIFY_SCRIPT = _COMMON_SCRIPT + """
function verify() {
  show("working");
  if (!token) { say("failure-text", "This link is incomplete. Request a new confirmation email from the app."); show("failure"); return; }
  post("/auth/verify-email", { token: token }).then(function (res) {
    if (res.ok) { show("success"); }
    else if (res.status === 400) { say("failure-text", "This confirmation link is invalid or has expired. Request a new one from the app."); show("failure"); }
    else { say("failure-text", "Something went wrong on our side. Please try again in a moment."); show("failure"); }
  }).catch(function () {
    say("failure-text", "Could not reach the server. Check your connection and try again.");
    show("failure");
  });
}
document.getElementById("retry").addEventListener("click", verify);
verify();
"""

_RESET_SCRIPT = _COMMON_SCRIPT + """
var form = document.getElementById("reset-form");
var submit = document.getElementById("submit");
function problem(text) { say("form-error", text); document.getElementById("form-error").hidden = false; }
if (!token) { say("failure-text", "This link is incomplete. Request a new reset link from the app."); show("failure"); }
else { show("form"); }
form.addEventListener("submit", function (event) {
  event.preventDefault();
  var one = document.getElementById("password").value, two = document.getElementById("confirm").value;
  document.getElementById("form-error").hidden = true;
  if (one.length < 8) { problem("Use at least 8 characters."); return; }
  if (one.length > 128) { problem("Use at most 128 characters."); return; }
  if (one !== two) { problem("The two passwords don't match."); return; }
  submit.disabled = true;
  post("/auth/reset-password", { token: token, new_password: one }).then(function (res) {
    if (res.ok) { show("success"); }
    else if (res.status === 400) { say("failure-text", "This reset link is invalid, expired or already used. Request a new one from the app."); show("failure"); }
    else if (res.status === 422) { problem("That password isn't accepted. Use 8 to 128 characters."); submit.disabled = false; }
    else { problem("Something went wrong on our side. Please try again."); submit.disabled = false; }
  }).catch(function () {
    problem("Could not reach the server. Check your connection and try again.");
    submit.disabled = false;
  });
});
"""

_OPEN_APP = '<a id="open-app" class="btn secondary" href="#" hidden>Open in the app</a>'


def _page(title: str, body: str, script: str, nonce: str, app_scheme: str | None) -> str:
    scheme_attr = html.escape(app_scheme or "", quote=True)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{html.escape(title)}</title><style nonce=\"{nonce}\">{_STYLE}</style></head>"
        f"<body><main data-scheme=\"{scheme_attr}\"><div class=\"brand\">routeWise</div>{body}</main>"
        f"<script nonce=\"{nonce}\">(function(){{{script}}})();</script></body></html>"
    )


def verify_email_page(nonce: str, app_scheme: str | None = None) -> str:
    body = (
        '<section id="working"><h1>Confirming your email…</h1>'
        '<p>One moment.</p><p id="slow" hidden>The server is waking up, this can take up to a minute.</p></section>'
        '<section id="success" hidden><h1 class="ok">Email confirmed</h1>'
        "<p>Your email address is confirmed. Go back to the app and log in.</p>" + _OPEN_APP + "</section>"
        '<section id="failure" hidden><h1 class="bad">Could not confirm</h1>'
        '<p id="failure-text"></p><button id="retry" type="button">Try again</button></section>'
    )
    return _page("Confirm your email", body, _VERIFY_SCRIPT, nonce, app_scheme)


def reset_password_page(nonce: str, app_scheme: str | None = None) -> str:
    body = (
        '<section id="working"><h1>Reset your password</h1><p id="slow" hidden>One moment…</p></section>'
        '<section id="form" hidden><h1>Choose a new password</h1>'
        '<p>You will be signed out on all your devices.</p>'
        '<form id="reset-form" novalidate>'
        '<label for="password">New password</label>'
        '<input id="password" type="password" autocomplete="new-password" minlength="8" maxlength="128" required>'
        '<label for="confirm">Confirm new password</label>'
        '<input id="confirm" type="password" autocomplete="new-password" minlength="8" maxlength="128" required>'
        '<p id="form-error" class="bad" role="alert" hidden></p>'
        '<button id="submit" type="submit">Update password</button></form>' + _OPEN_APP + "</section>"
        '<section id="success" hidden><h1 class="ok">Password updated</h1>'
        "<p>Your password was changed and you were signed out everywhere. Go back to the app and log in.</p></section>"
        '<section id="failure" hidden><h1 class="bad">Could not reset</h1><p id="failure-text"></p></section>'
    )
    return _page("Reset your password", body, _RESET_SCRIPT, nonce, app_scheme)


def new_nonce() -> str:
    return secrets.token_urlsafe(18)
