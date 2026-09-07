import os
import json

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.email_utils.gmail_api import (
    build_authorization_url,
    exchange_callback_code,
    gmail_configured,
    store_gmail_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


GOOGLE_CLIENT_ID = settings.google_client_id
GOOGLE_CLIENT_SECRET = settings.google_client_secret
GOOGLE_REDIRECT_URI = settings.google_redirect_uri

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_INFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
] or ["http://127.0.0.1:3000", "http://localhost:3000"]


class GoogleTokenRequest(BaseModel):
    code: str
    redirect_uri: str


def _google_exchange_code(code: str, redirect_uri: str, db: Session) -> str:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env, then restart the server.",
        )

    token_response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not token_response.ok:
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code")

    tokens = token_response.json()
    id_token_str = tokens.get("id_token")
    if not id_token_str:
        raise HTTPException(status_code=400, detail="No ID token received")

    try:
        tokeninfo_resp = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token_str},
            timeout=30,
        )
        if not tokeninfo_resp.ok:
            raise HTTPException(status_code=400, detail="Google ID token is invalid or expired")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to verify Google ID token")

    user_info = requests.get(
        GOOGLE_USER_INFO_URL,
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=30,
    )
    if not user_info.ok:
        raise HTTPException(status_code=400, detail="Failed to get user info from Google")

    user_google_email = user_info.json().get("email")
    if not user_google_email:
        raise HTTPException(status_code=400, detail="Google account email not available")

    user = db.query(models.User).filter(models.User.email == user_google_email).first()
    if not user:
        user = models.User(
            email=user_google_email,
            hashed_password=hash_password(os.urandom(16).hex()),
            oauth_tokens=json.dumps({"google": tokens["access_token"]}),
        )
        db.add(user)
    else:
        existing_tokens = json.loads(user.oauth_tokens or "{}")
        existing_tokens["google"] = tokens["access_token"]
        user.oauth_tokens = json.dumps(existing_tokens)

    db.commit()
    db.refresh(user)
    return create_access_token(user.id)


def _user_out(user: models.User) -> schemas.UserOut:
    return schemas.UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        location=user.location,
        linkedin=user.linkedin,
        github=user.github,
        smtp_username=user.smtp_username,
        smtp_configured=bool(user.smtp_username and user.smtp_app_password),
    )


@router.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(get_current_user)):
    return _user_out(current_user)


@router.put("/me", response_model=schemas.UserOut)
def update_me(
    payload: schemas.UserUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit profile fields used in the resume header, and per-user SMTP
    credentials so the Send step emails from the user's OWN Gmail account.
    smtp_app_password is write-only and never returned."""
    data = payload.model_dump(exclude_unset=True)

    if "smtp_username" in data or "smtp_app_password" in data:
        new_user = data.get("smtp_username")
        new_pwd = data.get("smtp_app_password")
        if new_user is None and not new_pwd:
            # Explicitly clear per-user SMTP.
            current_user.smtp_username = None
            current_user.smtp_app_password = None
        else:
            if new_user:
                current_user.smtp_username = new_user
            if new_pwd:
                current_user.smtp_app_password = new_pwd

    for field in ("full_name", "phone", "location", "linkedin", "github"):
        if field in data:
            setattr(current_user, field, data[field])

    db.commit()
    db.refresh(current_user)
    return _user_out(current_user)


@router.post("/register", response_model=schemas.TokenResponse)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = models.User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        oauth_tokens=json.dumps({}),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return schemas.TokenResponse(access_token=create_access_token(user.id))


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return schemas.TokenResponse(access_token=create_access_token(user.id))


@router.get("/google/login")
def google_login(state: str | None = None, redirect_uri: str | None = None):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env, then restart the server.",
        )

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    if state:
        params["state"] = state
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return {"auth_url": f"{GOOGLE_AUTH_URL}?" + requests.compat.urlencode(params)}


@router.post("/google/token", response_model=schemas.TokenResponse)
def google_token(payload: GoogleTokenRequest, db: Session = Depends(get_db)):
    token = _google_exchange_code(payload.code, payload.redirect_uri, db)
    return schemas.TokenResponse(access_token=token)


@router.get("/google/callback")
def google_callback(code: str, state: str | None = None, db: Session = Depends(get_db)):
    token = _google_exchange_code(code, GOOGLE_REDIRECT_URI, db)

    if state == "frontend":
        return HTMLResponse(content=f"""<!doctype html><html><body><script>
          (function() {{
            if (window.opener) {{
              window.opener.postMessage({{ type: 'google_oauth', token: {json.dumps(token)} }}, {json.dumps(ALLOWED_ORIGINS[0])});
            }}
            window.close();
          }})();
        </script>Logged in. You can close this window.</body></html>""")

    return JSONResponse(content={"access_token": token, "token_type": "bearer"})


@router.post("/logout")
def logout(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.oauth_tokens = json.dumps({})
    db.commit()
    return {"message": "Logged out successfully"}


@router.get("/gmail/status")
def gmail_status(current_user: models.User = Depends(get_current_user)):
    """Whether Gmail API sending is available and whether this user has
    connected their Gmail (vs relying on SMTP + App Password)."""
    tokens = json.loads(current_user.oauth_tokens or "{}")
    connected = bool(tokens.get("gmail", {}).get("refresh_token"))
    return {
        "configured": gmail_configured(),
        "connected": connected,
        "email": current_user.email,
    }


@router.get("/gmail/connect")
def gmail_connect(current_user: models.User = Depends(get_current_user)):
    """Start: return the Gmail consent URL so the user can authorize sending
    from their own account. No password is ever entered."""
    return {"auth_url": build_authorization_url()}


@router.get("/gmail/callback")
def gmail_callback(code: str, state: str | None = None, db: Session = Depends(get_db)):
    # The consent popup posts back with {gmail_code}; resolve the user from a
    # lightweight state token. For simplicity the web app passes the JWT in
    # state; the extension uses its own flow.
    if not code:
        return HTMLResponse(content="<body>Missing OAuth code.</body>", status_code=400)

    # Resolve which user is connecting. If state is a JWT, use it; otherwise
    # fall back to the most recently connected user id stored in a cookie.
    token_dict = exchange_callback_code(code)

    current_user = None
    if state:
        try:
            current_user = get_current_user(token=state, db=db)
        except Exception:
            current_user = None

    if current_user is None:
        return HTMLResponse(
            content="""<body>Your Gmail connection failed: we could not identify your session.
            Open the app again, go to your profile, and click 'Connect Gmail'.</body>""",
            status_code=400,
        )

    store_gmail_token(current_user, db, token_dict)

    # Close the consent popup and tell the page it worked.
    if state == "frontend-gmail":
        return HTMLResponse(content=f"""<!doctype html><html><body><script>
          (function() {{
            if (window.opener) {{ window.opener.postMessage({{ type: 'gmail_connected', ok: true }}, {json.dumps(ALLOWED_ORIGINS[0])}); }}
            window.close();
          }})();
        </script>Gmail connected. You can close this window.</body></html>""")

    return HTMLResponse(content="<!doctype html><html><body>Gmail connected. You can close this window.</body></html>")
