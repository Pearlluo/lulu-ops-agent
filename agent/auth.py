"""
auth.py — email login for the Lulu Operations Center.

Why: the safety chain's role gating only closes when the ROLE comes from a verified
identity, not a dropdown. Each user logs in with their email; their role and their
conversation history are bound to it.

Storage: users.yaml  { email: {name, role, salt, pw} }   (pw = sha256(salt + password))
First run: no users.yaml -> the login page becomes a one-time "create admin" form.
Admin_IT users get an in-app "user management" panel (add / deactivate).

This is identification for a trusted internal network — NOT internet-grade security.
For production deploy to Azure App Service with Entra ID (Easy Auth) and replace
`require_login()` with reading X-MS-CLIENT-PRINCIPAL; everything else stays the same.
"""

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

import streamlit as st
import yaml

AGENT_DIR = Path(__file__).resolve().parent
USERS_PATH = AGENT_DIR / "users.yaml"
SECRET_PATH = AGENT_DIR / ".auth_secret"     # signs the stay-logged-in token; do not commit

ROLES = ["default", "HR_Manager", "Finance", "Admin_IT"]
TOKEN_TTL = 7 * 24 * 3600                    # stay logged in for 7 days


# ---------------- stay-logged-in token (survives browser refresh via URL param) ----------------
def _secret():
    if not SECRET_PATH.exists():
        SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_PATH.read_text(encoding="utf-8").strip()


def _sign(payload):
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(email):
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{email}|{exp}"
    return f"{payload}|{_sign(payload)}"


def verify_token(token):
    try:
        email, exp, sig = token.rsplit("|", 2)
    except (ValueError, AttributeError):
        return None
    payload = f"{email}|{exp}"
    if not hmac.compare_digest(sig, _sign(payload)) or int(exp) < time.time():
        return None
    u = load_users().get(email)
    if not u or not u.get("active", True):
        return None
    return {"email": email, "name": u["name"], "role": u["role"]}


# ---------------- user store ----------------
def load_users():
    if not USERS_PATH.exists():
        return {}
    return yaml.safe_load(USERS_PATH.read_text(encoding="utf-8")) or {}


def save_users(users):
    USERS_PATH.write_text(
        "# Lulu Ops Center users — managed via the in-app admin panel (auth.py).\n"
        "# pw = sha256(salt + password). Do not commit this file to git.\n"
        + yaml.safe_dump(users, allow_unicode=True, sort_keys=True),
        encoding="utf-8")


def _hash(salt, password):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def add_user(email, name, role, password, active=True):
    email = email.strip().lower()
    users = load_users()
    salt = secrets.token_hex(8)
    users[email] = {"name": name.strip(), "role": role, "salt": salt,
                    "pw": _hash(salt, password), "active": bool(active)}
    save_users(users)
    return email


def verify(email, password):
    u = load_users().get(email.strip().lower())
    if not u or not u.get("active", True):
        return None
    if _hash(u["salt"], password) == u["pw"]:
        return {"email": email.strip().lower(), "name": u["name"], "role": u["role"]}
    return None


# ---------------- Entra ID (Azure Container Apps / App Service Easy Auth) ----------------
def _entra_user():
    """When the app sits behind Container Apps built-in auth, the platform injects the
    verified identity as request headers. Trust it: no password page, SSO with the
    Acme M365 account. Role comes from users.yaml (least-privilege 'default' if the
    email isn't provisioned yet). Returns None locally (no header) -> password login."""
    try:
        headers = st.context.headers or {}
    except Exception:
        return None
    email = ""
    for k in ("X-MS-Client-Principal-Name", "x-ms-client-principal-name"):
        if headers.get(k):
            email = headers.get(k).strip().lower()
            break
    if not email or "@" not in email:
        return None
    u = load_users().get(email)
    if u and u.get("active", True):
        return {"email": email, "name": u["name"], "role": u["role"]}
    return {"email": email, "name": email.split("@")[0], "role": "default"}


# ---------------- streamlit gate ----------------
def require_login():
    """Render login (or first-run admin setup) until authenticated.
    Returns {email, name, role}; calls st.stop() when not logged in yet."""
    if st.session_state.get("auth_user"):
        return st.session_state.auth_user

    # behind Entra Easy Auth: adopt the platform-verified identity (SSO, no password page)
    eu = _entra_user()
    if eu:
        st.session_state.auth_user = eu
        return eu

    # browser refresh: restore the session from the signed URL token (7-day TTL)
    tok = st.query_params.get("auth")
    if tok:
        u = verify_token(tok)
        if u:
            st.session_state.auth_user = u
            return u

    _cat = AGENT_DIR / "static" / "lulu_cat.svg"
    if _cat.exists():
        st.image(str(_cat), width=84)
    st.title("Lulu Operations Center")
    users = load_users()

    if not users:                                # ---- first run: create the admin ----
        st.subheader("Initial setup — create the admin account")
        st.caption("No users yet. The first account gets the Admin_IT role and can add colleagues in-app.")
        with st.form("setup"):
            email = st.text_input("Email", placeholder="you@company.com.au")
            name = st.text_input("Name", placeholder="Admin Luo")
            pw1 = st.text_input("Password", type="password")
            pw2 = st.text_input("Confirm password", type="password")
            if st.form_submit_button("Create admin ▶", type="primary"):
                if not email or "@" not in email:
                    st.error("Please enter a valid email address.")
                elif len(pw1) < 6:
                    st.error("Password must be at least 6 characters.")
                elif pw1 != pw2:
                    st.error("Passwords don't match.")
                else:
                    add_user(email, name or email.split("@")[0], "Admin_IT", pw1)
                    st.success("Admin account created — please sign in.")
                    st.rerun()
        st.stop()

    st.subheader("Sign in")                       # ---- normal login ----
    with st.form("login"):
        email = st.text_input("Email", placeholder="you@company.com.au")
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in ▶", type="primary"):
            user = verify(email, pw)
            if user:
                st.session_state.auth_user = user
                st.query_params["auth"] = make_token(user["email"])   # survive refresh
                st.rerun()
            else:
                st.error("Wrong email or password (or the account is disabled).")
    st.caption("No account? Ask an Admin_IT colleague to add you in User Management.")
    st.stop()


def logout_button():
    if st.button("Log out", use_container_width=True):
        st.session_state.pop("auth_user", None)
        st.query_params.pop("auth", None)        # invalidate the stay-logged-in token
        for k in ("conv_id", "chat", "engine_history", "last_pill"):
            st.session_state.pop(k, None)
        st.rerun()


def admin_panel(current_user):
    """User management — only rendered for Admin_IT."""
    if current_user["role"] != "Admin_IT":
        return
    with st.expander("User Management (Admin only)"):
        users = load_users()
        st.dataframe([{"email": e, "name": u["name"], "role": u["role"],
                       "active": u.get("active", True)} for e, u in sorted(users.items())],
                     use_container_width=True, hide_index=True)
        with st.form("add_user"):
            c1, c2 = st.columns(2)
            email = c1.text_input("Email")
            name = c2.text_input("Name")
            c3, c4 = st.columns(2)
            role = c3.selectbox("Role", ROLES, index=0)
            pw = c4.text_input("Initial password", type="password")
            if st.form_submit_button("Add / update user"):
                if "@" not in email or len(pw) < 6:
                    st.error("Invalid email or password shorter than 6 characters.")
                else:
                    add_user(email, name or email.split("@")[0], role, pw)
                    st.success(f"Saved {email} ({role})")
                    st.rerun()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="manage Lulu users from the CLI")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("email"); a.add_argument("name"); a.add_argument("role", choices=ROLES)
    a.add_argument("password")
    sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "add":
        add_user(args.email, args.name, args.role, args.password)
        print(f"added {args.email} ({args.role})")
    else:
        for e, u in sorted(load_users().items()):
            print(f"{e:40} {u['name']:20} {u['role']:12} active={u.get('active', True)}")
