"""Authenticated dashboard routes. Device execution additionally requires its pairing token."""
import importlib.util
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

_spec = importlib.util.spec_from_file_location("hermes_mobile_device_access_store", Path(__file__).parents[1] / "bridge.py")
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)
_reply_spec = importlib.util.spec_from_file_location("hermes_mobile_device_access_replies", Path(__file__).parents[1] / "continuation.py")
_replies = importlib.util.module_from_spec(_reply_spec)
_reply_spec.loader.exec_module(_replies)


def follow_up(value):
    """Schedule the profile's follow-up replies that are ready once the phone claims or answers.
    The agent's tools schedule the rest, including requests that expire unanswered."""
    try:
        _replies.reconcile(value)
    except Exception:
        logging.getLogger("hermes-mobile-device-access").exception("Device follow-up reconciliation failed; will retry")


router = APIRouter()


def home(requested=None):
    """The Hermes home of the profile a request is for. In the dashboard process Hermes binds the
    home ?profile= names (every supported version), and that binding wins. A profile's plugin host
    (plugins.isolation: host) binds nothing and runs in its profile's home: it reads ?profile=
    here, and without one serves the process home, as after Hermes 0.22.2, which forwards a
    secondary profile's requests to its host without ?profile=."""
    from hermes_constants import (
        PROFILE_ID_RE,
        get_default_hermes_root,
        get_hermes_home,
        get_hermes_home_override,
        named_profile_is_live,
        profile_name_for_home,
    )
    bound = get_hermes_home_override()
    if bound and profile_name_for_home(bound):
        return Path(bound)
    if requested is None:
        return get_hermes_home()
    root = get_default_hermes_root()
    if requested == "default":
        return root
    if not PROFILE_ID_RE.match(requested):
        raise HTTPException(400, f"Invalid profile name '{requested}'.")
    if not named_profile_is_live(root / "profiles" / requested):
        raise HTTPException(404, f"Profile '{requested}' does not exist.")
    return root / "profiles" / requested


def profile_names():
    """default and every live named profile, as Hermes lists them."""
    from hermes_constants import (
        PROFILE_ID_RE,
        get_default_hermes_root,
        named_profile_is_live,
    )
    profiles = get_default_hermes_root() / "profiles"
    homes = sorted(profiles.iterdir()) if profiles.is_dir() else []
    return ["default", *(home.name for home in homes
                         if home.name != "default" and PROFILE_ID_RE.match(home.name) and named_profile_is_live(home))]


def store(profile=None):
    return _bridge.open_store(home(profile))


@contextmanager
def errors():
    try:
        yield
    except _bridge.DeviceUnauthorized as error:
        # Not 401: to the apps that means the dashboard session expired. The session is fine; the device's token is not.
        raise HTTPException(403, str(error)) from None
    except _bridge.BridgeError as error:
        raise HTTPException(409, str(error)) from None
    except sqlite3.OperationalError as error:
        # Another process held the database past the busy timeout; the phone retries.
        if "locked" not in str(error) and "busy" not in str(error):
            raise
        raise HTTPException(503, "Device storage is busy; try again") from None


def token(value):
    if not value or len(value) < 32:
        raise HTTPException(401, "Device pairing token required")
    return value


class Registration(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    grants: list[str] = Field(max_length=16)
    token: str = Field(min_length=32, max_length=128)
    scope: dict
    # What the app can do beyond its grants. Older apps send none.
    features: list[str] | None = Field(default=None, max_length=64)


class Completion(BaseModel):
    lease: str = Field(min_length=32, max_length=32)
    result: dict


@router.get("/setup")
def setup():
    return {"protocol_version": 2, "plugin_version": _bridge.VERSION, "state": "ready", "profiles": profile_names()}


@router.put("/devices/{device_id}")
def register(device_id: str, body: Registration, profile: str | None = None):
    with errors():
        store(profile).register(device_id, body.name, body.grants, body.token, body.scope, body.features)
    return {"status": "registered"}


@router.post("/devices/{device_id}/pending")
def pending(device_id: str, profile: str | None = None, x_hermes_mobile_device_token: str = Header(default="")):
    credential = token(x_hermes_mobile_device_token)
    with errors():
        value = store(profile)
        claimed = value.claim(device_id, credential)
    follow_up(value)
    return {"requests": claimed}


@router.post("/devices/{device_id}/results/{request_id}")
def complete(device_id: str, request_id: str, body: Completion, profile: str | None = None, x_hermes_mobile_device_token: str = Header(default="")):
    credential = token(x_hermes_mobile_device_token)
    with errors():
        value = store(profile)
        completed = value.complete(device_id, credential, request_id, body.lease, body.result)
    follow_up(value)
    return completed


@router.get("/requests/{request_id}")
def result(request_id: str, profile: str | None = None):
    with errors():
        return store(profile).result(request_id)
