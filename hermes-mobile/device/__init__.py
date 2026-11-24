"""Hermes tool adapter. All Apple operations execute on an explicitly paired device."""
import asyncio
import json
import logging
import threading
import urllib.request
from functools import wraps
from urllib.parse import quote, urlsplit

from .bridge import (
    CONTACT_FIELDS,
    HEALTH_INTERVALS,
    HEALTH_METRICS,
    HEALTH_WRITE_METRICS,
    REMINDER_PRIORITIES,
    REMINDER_STATUSES,
    TERMINAL,
    VERSION,
    AccessUnavailable,
    BridgeError,
    open_store,
)
from .continuation import cron_api_available, reconcile


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def setup_instruction(owner):
    return (f"Ask the user to open Hermes Agent Settings → Device Access, select agent '{owner}', "
            "and choose the calendars or data categories to share. Stop here until they enable access. "
            "Retrying tools or inspecting server files cannot grant iPhone access.")


def store():
    """The store of the profile this tool call runs for: Hermes binds each call to its profile's home."""
    from hermes_constants import get_hermes_home
    return open_store(get_hermes_home())


def secret(name):
    """A credential of this profile, through Hermes' secret scope."""
    from agent.secret_scope import get_secret
    return get_secret(name) or ""


# Plain HTTP only to a relay on this machine (a local test relay): the install key rides in
# the wake's Authorization header. The push adapter applies the same rule to the same URL
# (`_relay_url_allowed` in push/adapter.py): keep both copies aligned.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def relay_url_allowed(relay_url):
    try:
        parts = urlsplit(str(relay_url or "").strip())
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))


def wake(device_id):
    relay = secret("HERMES_MOBILE_PUSH_RELAY_URL").rstrip("/")
    key = secret("HERMES_MOBILE_PUSH_INSTALL_KEY")
    if not relay or not key:
        return "Push is not configured; Hermes Agent will fetch this request when opened."
    if not relay_url_allowed(relay):
        logging.getLogger(__name__).warning("Device wake skipped: HERMES_MOBILE_PUSH_RELAY_URL must start with https:// "
                                            "(http:// only for localhost), as the install key travels with the request.")
        return ("Background refresh unavailable: the push relay URL must start with https:// (http:// only for "
                "localhost). Hermes Agent will fetch this request when opened.")
    try:
        request = urllib.request.Request(f"{relay}/devices/{quote(device_id, safe='')}/wake",
            data=b"{}", method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                "User-Agent": f"hermes-mobile/{VERSION}"})
        with urllib.request.urlopen(request, timeout=4) as response:
            response.read(1024)
        return "Background refresh requested; iOS delivery is best effort."
    except Exception as error:
        logging.getLogger(__name__).debug("Device wake failed: %s", error)
        return "Background refresh unavailable; Hermes Agent will fetch this request when opened."


def can_deliver_followup():
    # The push plugin sends nothing to a relay this rule refuses, so no follow-up would arrive.
    if not secret("HERMES_MOBILE_PUSH_INSTALL_KEY") or not relay_url_allowed(secret("HERMES_MOBILE_PUSH_RELAY_URL")):
        return False
    from gateway.platform_registry import platform_registry
    if not platform_registry.is_registered("push"):
        return False
    return cron_api_available()


async def await_result(value, request_id, seconds):
    deadline = asyncio.get_running_loop().time() + max(0, min(float(seconds), 20))
    while True:
        result = value.result(request_id)
        if result["status"] in TERMINAL or asyncio.get_running_loop().time() >= deadline:
            return result
        await asyncio.sleep(0.25)


def can_update_reminders():
    """Whether a phone whose app can edit reminders shares them with this profile.

    Hermes asks before advertising mobile_reminder_update and caches the answer briefly;
    the bridge still refuses the request when no such phone is there."""
    try:
        return any("reminders.update" in device.get("features", []) and "reminders.write" in device["grants"]
                   for device in store().devices())
    except Exception:
        logging.getLogger(__name__).exception("Device feature check failed")
        return False


def follow_up(value=None):
    """Schedule this profile's follow-up replies that are ready, as the dashboard routes do.
    False when another process was already doing so or it failed."""
    try:
        return reconcile(value or store())
    except Exception:
        logging.getLogger(__name__).exception("Device follow-up reconciliation failed; will retry")
        return False


EXPIRY_RETRIES = 6  # tries after the first, backing off up to 30 seconds


def follow_up_at_expiry(value, request_id, attempt=0):
    """Schedule a deferred reply when its request expires, as no claim or result will if the
    phone never answers. The timer fires once the request's deadline passes (later while a
    phone's lease on it runs) and again, backing off, while the reply is unscheduled: another
    process held the replies lock or scheduling failed. It lives in this process; after a
    restart the profile's next device call or the phone's next claim schedules the reply."""
    try:
        due = value.reply_due(request_id)
    except Exception:
        logging.getLogger(__name__).exception("Device follow-up check failed; will retry")
        due = value.clock()
    if due is None or attempt > EXPIRY_RETRIES:
        return
    timer = threading.Timer(max(0, due - value.clock()) + min(2 ** attempt, 30), at_expiry, (value, request_id, attempt + 1))
    timer.daemon = True
    timer.start()


def at_expiry(value, request_id, attempt):
    try:
        value.prune()  # results stay bounded even when no other write comes
    except Exception:
        logging.getLogger(__name__).exception("Device storage pruning failed")
    follow_up(value)
    follow_up_at_expiry(value, request_id, attempt)


def with_current_access(value, result):
    """Refresh access after waiting, including when polling an older request."""
    try:
        result["current_access"] = {"profile": value.profile, "devices": value.devices()}
    except Exception:
        logging.getLogger(__name__).exception("Device access refresh failed")
        return result
    result["access_note"] = (
        "current_access is the latest server-synced sharing configuration, not the scope of an older result. "
        "It supersedes calendar/access details remembered from earlier conversation. "
        "An empty event list does not mean calendar access is missing. "
        "If the user changed sharing and these details disagree, refresh mobile_devices; "
        "the iPhone may still need to sync."
    )
    return result


def register(ctx):
    def add(name, description, properties, required, handler, check_fn=None):
        @wraps(handler)
        async def guarded(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception:
                logging.getLogger(__name__).exception("Device tool failed")
                return dumps({"status": "failed", "error": "Device access is temporarily unavailable."})
            finally:
                await asyncio.to_thread(follow_up)
        # Hermes Agent's chat shows tool calls as status rows without their data.
        description += " The user does not see this result: write out the data they asked for in your answer."
        options = {"check_fn": check_fn} if check_fn else {}
        ctx.register_tool(name=name, toolset="mobile_device", is_async=True,
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": properties,
                                   "required": required, "additionalProperties": False}}, handler=guarded, **options)

    async def list_devices(args, **kwargs):
        value = store()
        devices = value.devices()
        result = {"profile": value.profile, "devices": devices}
        if not devices:
            result.update(setup_required=True, message=setup_instruction(value.profile))
        return dumps(result)

    async def get_result(args, **kwargs):
        value = store()
        try:
            result = await await_result(value, args["request_id"], args.get("wait_seconds", 0))
            return dumps(with_current_access(value, result))
        except (BridgeError, ValueError, KeyError) as error:
            return dumps(with_current_access(value, {"status": "failed", "error": str(error)}))

    add("mobile_devices", "List current devices, selected calendars (IDs/names), Health categories shared with this Hermes profile (health_metrics to read, health_write_metrics to log). Refresh this after the user changes sharing and before claiming which calendars are accessible; earlier conversation may be stale. Enable individual access in Hermes Agent Settings → Device Access.", {}, [], list_devices)
    add("mobile_request_result", "Read a previous device request. Pending is not success. Check status and observed_at before using results.",
        {"request_id": {"type": "string"}, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 20}}, ["request_id"], get_result)

    text = {"type": "string"}
    date = {"type": "string", "description": "ISO 8601 date and time with timezone"}
    calendar_ids = {"type": "array", "items": text, "description": "Optional subset of shared calendar IDs from mobile_devices. Defaults to all shared calendars."}
    metrics = {"type": "array", "items": {"type": "string", "enum": sorted(HEALTH_METRICS)}, "description": "Subset of shared Health categories (see health_metrics in mobile_devices). Defaults to every category this agent may read; ask only for what the question needs."}
    interval = {"type": "string", "enum": sorted(HEALTH_INTERVALS), "description": "Bucket size for each series. Defaults to day for ranges up to 92 days, else week. hour allows up to 4 days, day up to 92 days, week and month up to two years."}
    log_entries = {"type": "array", "minItems": 1, "maxItems": 20, "description": "Entries saved at the same time. Units are fixed: water mL, caffeine mg, sodium mg, dietary_energy kcal, protein/carbohydrates/fat/sugar/fiber g, weight kg, body_fat percent (e.g. 21.5), mindful_minutes minutes (the session ends at date).",
                   "items": {"type": "object", "additionalProperties": False, "required": ["metric", "value"],
                             "properties": {"metric": {"type": "string", "enum": sorted(HEALTH_WRITE_METRICS)}, "value": {"type": "number", "exclusiveMinimum": 0}}}}
    day = {"type": "string", "description": "A date as YYYY-MM-DD: an all-day reminder, with no time and no alert"}
    reminder_list = {"type": "string", "description": "The title of a Reminders list, matched case-insensitively. Use list_id when a list result named it."}
    reminder_list_id = {"type": "string", "description": "The id of a Reminders list from lists in a mobile_reminders_list result"}
    priority = {"type": "string", "enum": sorted(REMINDER_PRIORITIES)}
    reminder_query = {
        "status": {"type": "string", "enum": sorted(REMINDER_STATUSES), "description": "Default incomplete."},
        "due_start": {**date, "description": "Only reminders due at or after this ISO 8601 date and time with timezone."},
        "due_end": {**date, "description": "Only reminders due before this (exclusive). For overdue reminders pass the current time with status incomplete."},
        "include_undated": {"type": "boolean", "description": "Also return reminders with no due date. Default true without a due range, false with one."},
        "completed_start": {**date, "description": "Only with status completed: completed at or after this. Defaults to one year ago."},
        "completed_end": {**date, "description": "Only with status completed: completed before this (exclusive)."},
        "list_ids": {"type": "array", "items": text, "maxItems": 50, "description": "Only these lists, by id from lists in an earlier result."},
        "query": {"type": "string", "maxLength": 200, "description": "Case-insensitive text to find in the title or notes."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 50."},
        "offset": {"type": "integer", "minimum": 0, "description": "Skip this many matches; pass next_offset from the previous page."},
    }
    reminder_update = {
        "id": {"type": "string", "description": "The reminder's id from mobile_reminders_list or mobile_reminder_create."},
        "title": text, "notes": {"type": "string", "description": "New notes; an empty string clears them."},
        "due": date, "due_date": day, "clear_due": {"type": "boolean", "enum": [True], "description": "Remove the due date."},
        "list_id": reminder_list_id, "list": reminder_list, "priority": priority,
        "completed": {"type": "boolean", "description": "false reopens a completed reminder."},
    }
    definitions = [
        ("mobile_calendar_events", "calendar.events", "Read events only from calendars shared with this agent, in a range of at most 31 days.", {"start": date, "end": date, "calendar_ids": calendar_ids}, ["start", "end"]),
        ("mobile_calendar_create", "calendar.create", "Create one event in a shared writable calendar. Pass calendar_id from mobile_devices, or use the shared default/only writable calendar.", {"title": text, "start": date, "end": date, "notes": text, "calendar_id": text}, ["title", "start", "end"]),
        ("mobile_reminders_list", "reminders.list", ("Read reminders, newest page first: incomplete ones by default (soonest due first, undated last), or completed ones (most recently completed first, from the last year unless completed_start says otherwise). "
         "Filter by due date range, completion date range, lists and text. Each reminder has id, title, list and list_id, notes (shortened), priority, and due (a date and time) or due_date with all_day. "
         "The result also lists the user's Reminders lists (id, title, writable, default), total matches and next_offset; page with offset = next_offset rather than widening the range."),
         reminder_query, []),
        ("mobile_reminder_create", "reminders.create", "Create one reminder, in the default list unless list_id or list names another writable one.",
         {"title": text, "due": date, "due_date": day, "notes": text, "list_id": reminder_list_id, "list": reminder_list, "priority": priority}, ["title"]),
        ("mobile_reminder_complete", "reminders.complete", "Complete the reminder identified by a previous list result.", {"id": text}, ["id"]),
        ("mobile_reminder_update", "reminders.update", ("Change one reminder identified by a previous list or create result; only the fields given change; nothing is deleted. "
         "Move it to another list with list_id or list, set due (date and time) or due_date (all day) or clear_due, set priority, or reopen it with completed false."),
         reminder_update, ["id"]),
        ("mobile_health_summary", "health.summary", ("Read only Health categories shared with this agent for a health, fitness, nutrition or sleep question, over a range of up to two years (end is exclusive). Request the entire range in ONE call; never one call per day. "
         "The answer lists buckets (start, end) for the interval in the device time zone. metrics.<category> holds each measured category: summed ones (steps, distance, energy, exercise, nutrition, water) give unit, total and series (one total per bucket); others (heart rate, HRV, weight, blood oxygen, ...) give unit, average, minimum, maximum, latest (value, date) and series (one average per bucket). blood_pressure has systolic and diastolic. "
         "sleep gives asleep, in-bed, awake and stage hours (core, deep, REM) plus nights (each with its stages); mindfulness gives sessions, total minutes and series; workouts lists up to 60 newest workouts with activity_type, duration, source, statistics (energy, distance, heart rate, power, cadence, ...), elevation, weather, laps, pauses, effort scores and multisport activities. "
         "Unshared categories are omitted; null means no readable data, never zero."), {"start": date, "end": date, "metrics": metrics, "interval": interval}, ["start", "end"]),
        ("mobile_health_log", "health.log", "Log entries to Apple Health, only in categories this agent may log (health_write_metrics in mobile_devices), such as a glass of water, a coffee, a meal's nutrients, a weight reading or a meditation. Confirm amounts the user did not state before logging. Each entry becomes its own Health sample; nothing existing is changed or deleted.",
         {"entries": log_entries, "date": {"type": "string", "description": "ISO 8601 date and time with timezone when it happened. Defaults to now; at most a year ago."}, "food": {"type": "string", "description": "Optional food or drink name, saved with nutrition entries."}}, ["entries"]),
        ("mobile_location", "location.current", "Request current location. Background requests may be unavailable; inspect timestamp and accuracy.", {}, []),
        ("mobile_alarms_list", "alarms.list", "List the alarms and timers Hermes Agent set on the device for this agent, with their id, label, kind, state, and schedule or fire time. Alarms in Apple's Clock app are not visible.", {}, []),
        ("mobile_alarm_create", "alarms.create", "Set one alarm on the device that rings through Silent mode and Focus. Pass either at (one alarm at a date and time) or time (HH:MM on the device's clock, the next occurrence), optionally with weekdays to repeat weekly. The user can snooze it.", {"at": date, "time": {"type": "string", "description": "24-hour HH:MM in the device's local time"}, "weekdays": {"type": "array", "items": {"type": "string", "enum": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]}, "description": "Repeat on these days. Only with time."}, "label": {"type": "string", "description": "Short title shown when it rings, at most 100 characters"}}, []),
        ("mobile_timer_start", "timers.start", "Start one countdown timer on the device. It shows on the Lock Screen and Dynamic Island and rings when it ends.", {"duration_seconds": {"type": "number", "minimum": 1, "maximum": 86400}, "label": {"type": "string", "description": "Short title, at most 100 characters"}}, ["duration_seconds"]),
        ("mobile_alarm_cancel", "alarms.cancel", "Cancel one alarm or timer identified by a previous mobile_alarms_list, mobile_alarm_create, or mobile_timer_start result.", {"id": text}, ["id"]),
        ("mobile_contacts_search", "contacts.search", "Look up one person in the user's contacts by name, email address, or phone number, to reach them or to answer a question about their details. Search returns only matches; there is no way to list the address book. Only the detail categories the user shared are returned, and iOS may limit the search to contacts the user picked. Do not save returned details to memory unless the user asks.", {"query": {"type": "string", "description": "A name, email address, or phone number to match. Required."}, "limit": {"type": "number", "minimum": 1, "maximum": 10, "description": "Maximum matches to return. Default 5."}, "fields": {"type": "array", "items": {"type": "string", "enum": sorted(CONTACT_FIELDS)}, "description": "Optional subset of shared contact details. Defaults to everything this agent is allowed to read. Ask for only what the task needs."}}, ["query"]),
    ]
    from .bridge import WRITES
    for name, operation, description, fields, required in definitions:
        fields = {**fields, "device_id": text, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 20},
                  "notify_when_ready": {"type": "boolean", "description": "Default true. When pending and push is configured, schedule one Hermes reply after data arrives or expires."}}
        if operation in WRITES:
            fields["idempotency_key"] = {"type": "string", "description": "Unique key for this action. Reuse the same key for retries; never create a new key to retry."}
            required = [*required, "idempotency_key"]

        def make_handler(op):
            async def handle(args, **kwargs):
                value = store()
                try:
                    arguments = {k: v for k, v in args.items() if k not in {"device_id", "wait_seconds", "idempotency_key", "notify_when_ready"}}
                    request = value.request(op, arguments, device=args.get("device_id"), idempotency_key=args.get("idempotency_key"))
                    wake_status = None
                    if request["status"] not in TERMINAL:
                        wake_status = await asyncio.to_thread(wake, request["device_id"])
                    result = await await_result(value, request["id"], args.get("wait_seconds", 20))
                    if result["status"] not in TERMINAL:
                        result["message"] = "Request saved, not completed. Use mobile_request_result with this ID; do not repeat a write with a new idempotency key."
                        result["wake_status"] = wake_status
                        if args.get("notify_when_ready", True) and can_deliver_followup():
                            from hermes_constants import get_hermes_home
                            value.defer_reply(request["id"], kwargs.get("user_task") or f"Report the result of {op}.", str(get_hermes_home()))
                            follow_up_at_expiry(value, request["id"])
                            result["followup"] = "A one-shot Hermes reply will be scheduled after the result arrives or the request expires."
                    return dumps(with_current_access(value, result))
                except AccessUnavailable as error:
                    if error.update_required:
                        return dumps(with_current_access(value,
                            {"status": "unavailable", "error": str(error), "retryable": False, "update_required": True,
                             "message": "Tell the user to update Hermes Agent on the iPhone. Stop here; retrying cannot help."}))
                    return dumps(with_current_access(value,
                        {"status": "unavailable", "error": str(error), "retryable": False,
                         "setup_required": True, "message": setup_instruction(value.profile)}))
                except (BridgeError, ValueError, KeyError) as error:
                    return dumps(with_current_access(value, {"status": "failed", "error": str(error)}))
            return handle
        add(name, description, fields, required, make_handler(operation),
            check_fn=can_update_reminders if operation == "reminders.update" else None)
