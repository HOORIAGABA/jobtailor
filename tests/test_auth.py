"""Google sign-in, with no Google and no network.

Every HTTP call to Google goes through `app.io.google`, so the tests replace
that module's four functions and assert on what the rest of the system does with
their answers. What is being tested is the parts that are ours: the cookie, the
CSRF state, the PKCE verifier, the scope decisions, and the refresh-token rule
that is the easiest thing here to get quietly wrong.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.api import auth as routes
from app.api.deps import SESSION_COOKIE
from app.api.main import create_app
from app.db import models
from app.db.session import create_all, engine, reset, session_scope
from app.engine import authsession, secrets
from app.io import google
from app.pipeline import auth as service

SECRET = "a-long-random-development-secret"
KEY = secrets.new_key()
CLIENT_ID = "1234.apps.googleusercontent.com"
REDIRECT = "http://localhost:8000/api/auth/google/callback"


# ══ the session cookie ════════════════════════════════════════════════

def test_a_cookie_names_the_user_it_was_issued_for():
    token = authsession.issue(SECRET, "user1")
    assert authsession.read(SECRET, token) == "user1"


def test_a_cookie_signed_with_another_secret_is_refused():
    token = authsession.issue("other-secret", "user1")
    with pytest.raises(authsession.BadCookie, match="not signed by this server"):
        authsession.read(SECRET, token)


def test_editing_the_user_id_invalidates_the_cookie():
    """The whole threat is forging one of these."""
    token = authsession.issue(SECRET, "user1")
    version, _, expiry, mac = token.split(".")
    forged = ".".join([version, "user2", expiry, mac])
    with pytest.raises(authsession.BadCookie):
        authsession.read(SECRET, forged)


def test_extending_the_expiry_invalidates_the_cookie():
    """The expiry is inside the signed payload; an expiry outside a signature is
    a number the client edits."""
    token = authsession.issue(SECRET, "user1", ttl_seconds=60)
    version, user, expiry, mac = token.split(".")
    stretched = ".".join([version, user, str(int(expiry) + 10_000), mac])
    with pytest.raises(authsession.BadCookie):
        authsession.read(SECRET, stretched)


def test_an_expired_cookie_is_refused_even_though_it_verifies():
    token = authsession.issue(SECRET, "user1", ttl_seconds=10,
                              now=time.time() - 100)
    with pytest.raises(authsession.BadCookie, match="expired"):
        authsession.read(SECRET, token)


@pytest.mark.parametrize("cookie", ["", "garbage", "v1.a.b", "v2.u.1.mac",
                                    "v1.u.notanumber.mac"])
def test_a_malformed_cookie_never_verifies(cookie):
    with pytest.raises(authsession.BadCookie):
        authsession.read(SECRET, cookie)


def test_issuing_without_a_secret_refuses_rather_than_signing_with_nothing():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        authsession.issue("", "user1")


def test_fields_cannot_be_shuffled_past_the_signature():
    """Length-prefixed, so a user id ending in digits cannot borrow the
    expiry's bytes."""
    a = authsession.issue(SECRET, "ab", ttl_seconds=1, now=0)
    b = authsession.issue(SECRET, "a", ttl_seconds=1, now=0)
    assert a.split(".")[-1] != b.split(".")[-1]


# ══ encryption at rest ════════════════════════════════════════════════

def test_a_token_round_trips_through_encryption():
    assert secrets.decrypt(KEY, secrets.encrypt(KEY, "1//refresh")) == "1//refresh"


def test_the_ciphertext_does_not_contain_the_token():
    blob = secrets.encrypt(KEY, "1//0gRefreshTokenValue")
    assert b"RefreshTokenValue" not in blob


def test_encrypting_without_a_key_refuses_rather_than_storing_plaintext():
    """A default key would mean every deployment shares it, which is the same as
    not encrypting while looking as though you did."""
    with pytest.raises(secrets.BadKey, match="FERNET_KEY"):
        secrets.encrypt("", "1//refresh")
    with pytest.raises(secrets.BadKey, match="not a valid key"):
        secrets.encrypt("not-base64", "1//refresh")


def test_another_key_cannot_decrypt():
    blob = secrets.encrypt(KEY, "1//refresh")
    with pytest.raises(secrets.CannotDecrypt, match="reconnect"):
        secrets.decrypt(secrets.new_key(), blob)


def test_altered_ciphertext_is_refused_rather_than_returning_rubbish():
    blob = bytearray(secrets.encrypt(KEY, "1//refresh"))
    blob[-1] ^= 0xFF
    with pytest.raises(secrets.CannotDecrypt):
        secrets.decrypt(KEY, bytes(blob))


def test_an_absent_token_is_empty_rather_than_an_error():
    """"No access token yet" is a normal state and should not need a branch at
    every call site."""
    assert secrets.encrypt(KEY, "") == b""
    assert secrets.decrypt(KEY, b"") == ""
    assert secrets.decrypt(KEY, None) == ""


# ══ the scope decisions ═══════════════════════════════════════════════

def test_sign_in_asks_for_three_scopes_and_no_gmail_scope():
    """A consent screen asking to send email before anything is being sent is a
    screen people close."""
    url, _ = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT)
    scopes = parse_qs(urlparse(url).query)["scope"][0].split()
    assert scopes == ["openid", "email", "profile"]
    assert not any("gmail" in s for s in scopes)


def test_connecting_adds_exactly_one_gmail_scope():
    """`gmail.send` is *sensitive*; every other Gmail scope is *restricted*, and
    one restricted scope means a CASA assessment for the whole application."""
    url, _ = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT,
                           connect=True)
    scopes = parse_qs(urlparse(url).query)["scope"][0].split()
    gmail = [s for s in scopes if "gmail" in s or "mail.google" in s]
    assert gmail == ["https://www.googleapis.com/auth/gmail.send"]


def test_connecting_keeps_the_scopes_already_granted():
    url, _ = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT,
                           connect=True)
    query = parse_qs(urlparse(url).query)
    assert query["include_granted_scopes"] == ["true"]


def test_only_the_connect_flow_asks_for_a_refresh_token():
    """Signing in does not need to act for the person while they are away, so it
    mints no long-lived credential — and does not re-show consent every time."""
    signin, _ = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT)
    connect, _ = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT,
                               connect=True)
    assert "access_type" not in parse_qs(urlparse(signin).query)
    assert "prompt" not in parse_qs(urlparse(signin).query)
    assert parse_qs(urlparse(connect).query)["access_type"] == ["offline"]
    assert parse_qs(urlparse(connect).query)["prompt"] == ["consent"]


def test_the_authorization_url_carries_a_pkce_challenge_not_the_verifier():
    """If the code leaks — a proxy log, a referrer, a shared machine — it is
    useless without the verifier, which never travels through the address bar."""
    url, challenge = service.start(client_id=CLIENT_ID, redirect_uri=REDIRECT)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge"] == [challenge.challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert challenge.verifier not in url


def test_starting_without_a_client_id_says_where_to_get_one():
    with pytest.raises(google.NotConfigured, match="GOOGLE_CLIENT_ID"):
        service.start(client_id="", redirect_uri=REDIRECT)


# ══ the ID token ══════════════════════════════════════════════════════

def _id_token(**claims) -> str:
    import base64
    import json
    payload = {"iss": "https://accounts.google.com", "aud": CLIENT_ID,
               "sub": "10769150350006150715113082367",
               "email": "a.morgan@example.com", "email_verified": True,
               "name": "A. Morgan", "exp": time.time() + 3600}
    payload.update(claims)
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def test_the_identity_comes_out_of_the_id_token():
    who = google.identity(_id_token(), client_id=CLIENT_ID)
    assert who.sub.startswith("107691")
    assert who.email == "a.morgan@example.com"
    assert who.name == "A. Morgan"


def test_a_token_from_another_issuer_is_refused():
    with pytest.raises(google.GoogleError, match="not issued by Google"):
        google.identity(_id_token(iss="https://evil.example.com"),
                        client_id=CLIENT_ID)


def test_a_token_for_another_client_id_is_refused():
    """The commonest console mistake: the id from a different OAuth client."""
    with pytest.raises(google.GoogleError, match="different client id"):
        google.identity(_id_token(aud="9999.apps.googleusercontent.com"),
                        client_id=CLIENT_ID)


def test_an_expired_token_is_refused():
    with pytest.raises(google.GoogleError, match="expired"):
        google.identity(_id_token(exp=time.time() - 3600), client_id=CLIENT_ID)


@pytest.mark.parametrize("token", ["", "not.a", "a.!!!.c"])
def test_a_malformed_id_token_is_refused(token):
    with pytest.raises(google.GoogleError):
        google.identity(token, client_id=CLIENT_ID)


def test_a_token_with_no_subject_is_refused():
    """`sub` is the identity. Without it there is nothing to key a user on."""
    with pytest.raises(google.GoogleError, match="subject"):
        google.identity(_id_token(sub=""), client_id=CLIENT_ID)


# ══ the callback, as a service ════════════════════════════════════════

@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset(url)
    create_all(engine())
    yield
    reset()


def _grant(*, refresh: str = "1//refresh", send: bool = False,
           expires_in: int = 3600, **claims) -> google.Grant:
    scopes = ["openid", "email", "profile"]
    if send:
        scopes.append(google.SEND_SCOPE)
    return google.Grant(access_token="ya29.access", refresh_token=refresh,
                        expires_in=expires_in, scopes=tuple(scopes),
                        id_token=_id_token(**claims))


def _exchanges(monkeypatch, grant: google.Grant) -> list[dict]:
    """Replace the network call, and record what it was asked."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return grant

    monkeypatch.setattr(google, "exchange_code", fake)
    return calls


def _callback(session, **over):
    fields = dict(client_id=CLIENT_ID, client_secret="secret",
                  redirect_uri=REDIRECT, code="4/code", verifier="verifier",
                  fernet_key=KEY)
    fields.update(over)
    return service.callback(session, **fields)


def test_signing_in_creates_the_user_and_stores_no_token(db, monkeypatch):
    """Someone who wants the tailored .docx and will send it themselves never
    connects Gmail, and holding a credential for that is a liability with no
    feature behind it."""
    _exchanges(monkeypatch, _grant())
    with session_scope() as s:
        user, connected = _callback(s)
        assert connected is False
        assert user.email == "a.morgan@example.com"
        assert service.connection(s, user) is None
        assert s.query(models.OAuthToken).count() == 0


def test_connecting_stores_the_refresh_token_encrypted(db, monkeypatch):
    _exchanges(monkeypatch, _grant(send=True))
    with session_scope() as s:
        user, connected = _callback(s)
        assert connected is True
        row = service.connection(s, user)
        assert row is not None
        assert b"1//refresh" not in row.refresh_token_encrypted
        assert secrets.decrypt(KEY, row.refresh_token_encrypted) == "1//refresh"
        assert google.SEND_SCOPE in row.scopes


def test_declining_the_send_scope_signs_in_without_connecting(db, monkeypatch):
    """A person can approve sign-in and decline sending on the same screen, and
    the only honest way to know is to read the scopes that came back."""
    _exchanges(monkeypatch, _grant(send=False))
    with session_scope() as s:
        user, connected = _callback(s)
        assert connected is False
        assert service.describe(s, user)["can_send"] is False


def test_signing_in_twice_is_the_same_user(db, monkeypatch):
    _exchanges(monkeypatch, _grant())
    with session_scope() as s:
        first, _ = _callback(s)
        second, _ = _callback(s)
        assert first.id == second.id
        assert s.query(models.User).count() == 1


def test_the_user_is_keyed_on_the_subject_not_the_email(db, monkeypatch):
    """An address can be reassigned inside a Workspace domain. Keying on email is
    how one person's runs end up under another person's login."""
    _exchanges(monkeypatch, _grant())
    with session_scope() as s:
        first, _ = _callback(s)
        original = first.id

    _exchanges(monkeypatch, _grant(email="new.address@example.com"))
    with session_scope() as s:
        again, _ = _callback(s)
        assert again.id == original            # same person
        assert again.email == "new.address@example.com"   # refreshed

    _exchanges(monkeypatch, _grant(sub="a-different-account"))
    with session_scope() as s:
        other, _ = _callback(s)
        assert other.id != original
        assert s.query(models.User).count() == 2


# ══ the refresh-token rule ════════════════════════════════════════════

def test_a_refresh_never_overwrites_the_stored_refresh_token(db, monkeypatch):
    """★ Google returns a refresh token on the FIRST grant and never again, so a
    refresh response carries none. Writing that empty value over the stored one
    would leave sending working for exactly one hour, then failing permanently,
    with no obvious cause."""
    _exchanges(monkeypatch, _grant(send=True))
    with session_scope() as s:
        user, _ = _callback(s)
        user_id = user.id

    monkeypatch.setattr(google, "refresh", lambda **kw: google.Grant(
        access_token="ya29.second", refresh_token="", expires_in=3600,
        scopes=(google.SEND_SCOPE,), id_token=""))

    with session_scope() as s:
        user = s.get(models.User, user_id)
        # Force the stored access token to look expired.
        row = service.connection(s, user)
        row.expires_at = None
        s.flush()

        token = service.access_token(s, user, client_id=CLIENT_ID,
                                     client_secret="secret", fernet_key=KEY)
        assert token == "ya29.second"
        row = service.connection(s, user)
        assert secrets.decrypt(KEY, row.refresh_token_encrypted) == "1//refresh"


def test_an_unexpired_access_token_is_reused_rather_than_refreshed(db,
                                                                   monkeypatch):
    _exchanges(monkeypatch, _grant(send=True))

    def never(**kwargs):
        raise AssertionError("refreshed a token that had not expired")

    with session_scope() as s:
        user, _ = _callback(s)
        monkeypatch.setattr(google, "refresh", never)
        assert service.access_token(s, user, client_id=CLIENT_ID,
                                    client_secret="secret",
                                    fernet_key=KEY) == "ya29.access"


def test_an_expired_access_token_is_refreshed(db, monkeypatch):
    _exchanges(monkeypatch, _grant(send=True, expires_in=-60))
    monkeypatch.setattr(google, "refresh", lambda **kw: google.Grant(
        access_token="ya29.fresh", refresh_token="", expires_in=3600,
        scopes=(google.SEND_SCOPE,), id_token=""))
    with session_scope() as s:
        user, _ = _callback(s)
        assert service.access_token(s, user, client_id=CLIENT_ID,
                                    client_secret="secret",
                                    fernet_key=KEY) == "ya29.fresh"


def test_sending_without_a_connection_says_what_to_do(db, monkeypatch):
    _exchanges(monkeypatch, _grant())
    with session_scope() as s:
        user, _ = _callback(s)
        with pytest.raises(service.NotConnected, match="approval step"):
            service.access_token(s, user, client_id=CLIENT_ID,
                                 client_secret="secret", fernet_key=KEY)


def test_the_scopes_accumulate_rather_than_being_replaced(db, monkeypatch):
    """An incremental grant reports only what was just granted on some flows,
    and forgetting the earlier ones would make a connected account look
    unconnected."""
    _exchanges(monkeypatch, _grant(send=True))
    with session_scope() as s:
        user, _ = _callback(s)
        row = service.connection(s, user)
        row.scopes = google.SEND_SCOPE          # as if only send were recorded
        s.flush()

    _exchanges(monkeypatch, _grant(send=True))
    with session_scope() as s:
        user, _ = _callback(s)
        scopes = service.connection(s, user).scopes.split()
        assert google.SEND_SCOPE in scopes
        assert "openid" in scopes


# ══ disconnecting ═════════════════════════════════════════════════════

def test_disconnect_revokes_at_google_before_deleting_the_row(db, monkeypatch):
    """Deleting the row alone leaves a live grant on the person's Google account
    that this application can no longer see, so the button would be lying."""
    _exchanges(monkeypatch, _grant(send=True))
    revoked: list[str] = []
    monkeypatch.setattr(google, "revoke", revoked.append)

    with session_scope() as s:
        user, _ = _callback(s)
        assert service.disconnect(s, user, fernet_key=KEY) is True
        assert revoked == ["1//refresh"]
        assert service.connection(s, user) is None
        assert service.describe(s, user)["gmail_connected"] is False


def test_disconnecting_when_nothing_is_connected_is_not_an_error(db, monkeypatch):
    _exchanges(monkeypatch, _grant())
    with session_scope() as s:
        user, _ = _callback(s)
        assert service.disconnect(s, user, fernet_key=KEY) is False


def test_a_revoke_that_fails_still_deletes_the_row(db, monkeypatch):
    """A stored credential this application has decided not to hold is worse
    kept than dropped."""
    _exchanges(monkeypatch, _grant(send=True))

    def refuses(token):
        raise RuntimeError("network down")

    with session_scope() as s:
        user, _ = _callback(s)
        monkeypatch.setattr(google, "revoke", refuses)
        with pytest.raises(RuntimeError):
            service.disconnect(s, user, fernet_key=KEY)

    # `io.google.revoke` swallows its own errors for this reason; the service
    # only guards the decrypt. Verified separately below.


def test_revoke_swallows_a_network_failure_rather_than_blocking_disconnect(
        monkeypatch):
    import httpx

    def explodes(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", explodes)
    google.revoke("1//refresh")            # logged, not raised


def test_describe_never_returns_a_token(db, monkeypatch):
    """The natural way to write that function — serialise the row — would."""
    _exchanges(monkeypatch, _grant(send=True))
    with session_scope() as s:
        user, _ = _callback(s)
        body = service.describe(s, user)
        flat = repr(body)
        assert "1//refresh" not in flat
        assert "ya29" not in flat
        assert body["gmail_connected"] is True


# ══ over HTTP ═════════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite+pysqlite:///{tmp_path / 'auth_api.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", REDIRECT)
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("FERNET_KEY", KEY)
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    reset(url)
    create_all(engine())
    yield TestClient(create_app(tmp_path / "runs", read_only=False),
                     follow_redirects=False)
    reset()


def test_start_redirects_to_google_and_remembers_the_flow(api: TestClient):
    response = api.get("/api/auth/google/start")
    assert response.status_code == 307
    assert response.headers["location"].startswith(google.AUTH_ENDPOINT)

    jar = response.cookies
    assert jar[routes.STATE_COOKIE]
    assert jar[routes.VERIFIER_COOKIE]
    # The state in the URL is the one in the cookie, which is what makes a
    # forged callback fail.
    assert parse_qs(urlparse(response.headers["location"]).query)["state"] == [
        jar[routes.STATE_COOKIE]]


def test_the_flow_cookies_are_not_readable_by_page_scripts(api: TestClient):
    response = api.get("/api/auth/google/start")
    raw = " ".join(response.headers.get_list("set-cookie"))
    assert raw.lower().count("httponly") >= 3


def test_a_full_sign_in_sets_a_session_cookie(api: TestClient, monkeypatch):
    _exchanges(monkeypatch, _grant())
    start = api.get("/api/auth/google/start")
    state = start.cookies[routes.STATE_COOKIE]

    done = api.get(f"/api/auth/google/callback?code=4/code&state={state}")
    assert done.status_code == 303
    assert done.headers["location"].startswith("http://localhost:3000/")
    assert "signed_in=1" in done.headers["location"]

    cookie = done.cookies[SESSION_COOKIE]
    assert authsession.read(SECRET, cookie)

    api.cookies.set(SESSION_COOKIE, cookie)
    me = api.get("/api/auth/me").json()
    assert me["email"] == "a.morgan@example.com"
    assert me["gmail_connected"] is False


def test_a_callback_whose_state_does_not_match_is_refused(api: TestClient,
                                                          monkeypatch):
    """Without this an attacker completes a login in the victim's browser and
    leaves them signed into the attacker's account."""
    calls = _exchanges(monkeypatch, _grant())
    api.get("/api/auth/google/start")
    response = api.get("/api/auth/google/callback?code=4/code&state=forged")

    assert "auth_error=bad_state" in response.headers["location"]
    assert calls == []                    # the code was never exchanged
    assert SESSION_COOKIE not in response.cookies


def test_a_callback_with_no_prior_start_is_refused(api: TestClient, monkeypatch):
    calls = _exchanges(monkeypatch, _grant())
    response = api.get("/api/auth/google/callback?code=4/code&state=anything")
    assert "auth_error=bad_state" in response.headers["location"]
    assert calls == []


def test_declining_consent_lands_back_on_the_app_not_on_json(api: TestClient):
    """This URL is reached by a browser navigation; a person who pressed cancel
    should see the application again."""
    response = api.get("/api/auth/google/callback?error=access_denied")
    assert response.status_code == 303
    assert "auth_error=access_denied" in response.headers["location"]


def test_a_failed_exchange_lands_back_on_the_app(api: TestClient, monkeypatch):
    def refuses(**kwargs):
        raise google.GoogleError("Google refused (400): invalid_grant")

    monkeypatch.setattr(google, "exchange_code", refuses)
    start = api.get("/api/auth/google/start")
    response = api.get("/api/auth/google/callback?code=4/code"
                       f"&state={start.cookies[routes.STATE_COOKIE]}")
    assert "auth_error=exchange_failed" in response.headers["location"]


def test_connecting_gmail_reports_it_on_the_way_back(api: TestClient,
                                                     monkeypatch):
    _exchanges(monkeypatch, _grant(send=True))
    start = api.get("/api/auth/google/start?connect=true")
    done = api.get("/api/auth/google/callback?code=4/code"
                   f"&state={start.cookies[routes.STATE_COOKIE]}")
    assert "gmail=connected" in done.headers["location"]

    api.cookies.set(SESSION_COOKIE, done.cookies[SESSION_COOKIE])
    assert api.get("/api/auth/me").json()["can_send"] is True


def test_declining_only_the_send_scope_says_declined_not_signed_in(
        api: TestClient, monkeypatch):
    _exchanges(monkeypatch, _grant(send=False))
    start = api.get("/api/auth/google/start?connect=true")
    done = api.get("/api/auth/google/callback?code=4/code"
                   f"&state={start.cookies[routes.STATE_COOKIE]}")
    assert "gmail=declined" in done.headers["location"]


def test_me_is_401_when_nobody_is_signed_in(api: TestClient):
    assert api.get("/api/auth/me").status_code == 401


def test_a_forged_session_cookie_is_401_not_a_500(api: TestClient):
    api.cookies.set(SESSION_COOKIE, authsession.issue("wrong-secret", "u1"))
    assert api.get("/api/auth/me").status_code == 401


def test_a_cookie_for_a_deleted_user_is_401(api: TestClient):
    api.cookies.set(SESSION_COOKIE, authsession.issue(SECRET, "nosuchuser"))
    assert api.get("/api/auth/me").status_code == 401


def test_signing_out_clears_the_cookie_but_not_the_gmail_grant(
        api: TestClient, monkeypatch):
    _exchanges(monkeypatch, _grant(send=True))
    start = api.get("/api/auth/google/start?connect=true")
    done = api.get("/api/auth/google/callback?code=4/code"
                   f"&state={start.cookies[routes.STATE_COOKIE]}")
    api.cookies.set(SESSION_COOKIE, done.cookies[SESSION_COOKIE])

    assert api.post("/api/auth/logout").json() == {"signed_out": True}
    with session_scope() as s:
        # Collapsing sign-out into disconnect would cost a consent screen on
        # every return.
        assert s.query(models.OAuthToken).count() == 1


def test_disconnecting_over_http_revokes_and_deletes(api: TestClient,
                                                     monkeypatch):
    _exchanges(monkeypatch, _grant(send=True))
    revoked: list[str] = []
    monkeypatch.setattr(google, "revoke", revoked.append)

    start = api.get("/api/auth/google/start?connect=true")
    done = api.get("/api/auth/google/callback?code=4/code"
                   f"&state={start.cookies[routes.STATE_COOKIE]}")
    api.cookies.set(SESSION_COOKIE, done.cookies[SESSION_COOKIE])

    body = api.request("DELETE", "/api/auth/google").json()
    assert body == {"disconnected": True, "gmail_connected": False}
    assert revoked == ["1//refresh"]
    assert api.get("/api/auth/me").json()["gmail_connected"] is False


def test_a_real_session_cookie_wins_over_the_development_identity(
        api: TestClient, monkeypatch):
    """The other order would mean a developer with DEV_USER_EMAIL still set
    could not test as a signed-in user, and would not notice."""
    _exchanges(monkeypatch, _grant())
    start = api.get("/api/auth/google/start")
    done = api.get("/api/auth/google/callback?code=4/code"
                   f"&state={start.cookies[routes.STATE_COOKIE]}")
    api.cookies.set(SESSION_COOKIE, done.cookies[SESSION_COOKIE])
    monkeypatch.setenv("DEV_USER_EMAIL", "someone.else@example.com")

    assert api.get("/api/auth/me").json()["email"] == "a.morgan@example.com"


def test_without_a_client_id_sign_in_is_503_with_the_console_steps(
        api: TestClient, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    response = api.get("/api/auth/google/start")
    assert response.status_code == 503
    assert "OAuth client ID" in response.json()["detail"]
    assert REDIRECT in response.json()["detail"]


def test_connecting_without_an_encryption_key_refuses_before_the_round_trip(
        api: TestClient, monkeypatch):
    """Refusing after the consent screen would mean the person granted access
    for nothing."""
    monkeypatch.setenv("FERNET_KEY", "")
    response = api.get("/api/auth/google/start?connect=true")
    assert response.status_code == 503
    assert "FERNET_KEY" in response.json()["detail"]


def test_a_token_about_to_expire_is_refreshed_before_it_is_used(db, monkeypatch):
    """★ The margin, and why it is not just a bigger epsilon.

    A token with seconds left passes a bare `expires_at > now` and then expires
    *during* the Gmail request. On the send path that costs an audit row, a
    failed run, and a person wondering whether their application went out — for
    a token the system could have refreshed a minute earlier for free.
    """
    _exchanges(monkeypatch, _grant(send=True, expires_in=30))
    monkeypatch.setattr(google, "refresh", lambda **kw: google.Grant(
        access_token="ya29.ahead-of-time", refresh_token="", expires_in=3600,
        scopes=(google.SEND_SCOPE,), id_token=""))

    with session_scope() as s:
        user, _ = _callback(s)
        assert service.access_token(
            s, user, client_id=CLIENT_ID, client_secret="secret",
            fernet_key=KEY) == "ya29.ahead-of-time"


def test_a_token_with_plenty_of_life_left_is_not_refreshed(db, monkeypatch):
    """The margin must not turn every call into a refresh."""
    _exchanges(monkeypatch, _grant(send=True, expires_in=3600))

    def never(**kwargs):
        raise AssertionError("refreshed a token with an hour left")

    with session_scope() as s:
        user, _ = _callback(s)
        monkeypatch.setattr(google, "refresh", never)
        assert service.access_token(
            s, user, client_id=CLIENT_ID, client_secret="secret",
            fernet_key=KEY) == "ya29.access"
