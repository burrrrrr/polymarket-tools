"""The Settings page: the farmer's settings, the mutes, the scan's triage count, and
the controls that stop the farmer or pause every market."""

from contextlib import closing
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import farm_db
from farm_db import CommandKind

from .. import forms, views
from ..context import FARMER_DEFAULTS, form_of, see_other


def routes(ctx):
    router = APIRouter()

    def settings_context(conn, now, group=None, submitted=None, errors=None):
        overrides, bad_rows = farm_db.load_settings(conn)
        status = farm_db.read_farmer_status(conn)
        return {
            "groups": [(g, views.setting_rows(
                forms.SETTING_FIELDS, g, status, overrides, bad_rows, FARMER_DEFAULTS,
                submitted if g == group else {}, errors if g == group else {}))
                for g in forms.SETTING_GROUPS],
            "status": status, "live": views.liveness(status, now),
            "stop": farm_db.latest_command(conn, CommandKind.STOP), "db_path": ctx.db_path,
        }

    def settings_response(request, conn, status_code=200, notice=None, cancel_error=None,
                          triage_error=None, **kw):
        now = ctx.clock()
        return ctx.templates.TemplateResponse(request, "settings.html", {
            "chrome": ctx.chrome(ctx.snapshot(conn, now), now), "now": now, "page": "settings",
            "notice": notice, "cancel_error": cancel_error, "mutes": farm_db.read_mutes(conn),
            "triage_auto": farm_db.load_triage_auto(conn), "triage_error": triage_error,
            **settings_context(conn, now, **kw)}, status_code=status_code)

    @router.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: Optional[str] = None,
                 cancelled: Optional[int] = None, reset: Optional[str] = None,
                 unmuted: Optional[str] = None, auto_triage: Optional[int] = None):
        notice = None
        if auto_triage is not None:
            notice = (f"A console scan now sends its top {auto_triage} new candidates to Claude."
                      if auto_triage else "A console scan sends nothing to Claude.")
        elif saved:
            notice = f"Saved {saved}. The farmer applies it on its next cycle."
        elif reset:
            notice = f"Reset {reset} to the farmer's own value."
        elif unmuted:
            notice = f"Unmuted {unmuted}. Its candidates are back in the review queue."
        elif cancelled is not None:
            notice = (f"Paused {cancelled} approved market{'s' if cancelled != 1 else ''}. "
                      f"The farmer cancels the orders on its next cycle.")
        with closing(ctx.connect()) as conn:
            return settings_response(request, conn, notice=notice)

    @router.post("/settings/group/{group}")
    async def save_settings(request: Request, group: str):
        if group not in forms.SETTING_GROUPS:
            raise HTTPException(404, f"no settings group {group!r}")
        form = await form_of(request)
        values, errors = forms.parse_settings(form, group)
        with closing(ctx.connect()) as conn:
            if errors:
                return settings_response(request, conn, 422, group=group, submitted=form,
                                         errors=errors)
            overrides, _ = farm_db.load_settings(conn)
            status = farm_db.read_farmer_status(conn)
            farm_db.put_settings(conn, {
                key: value for key, value in values.items()
                if value != views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]
            }, ctx.clock())
        return see_other(f"/settings?saved={group}")

    @router.post("/settings/triage")
    async def save_triage_auto(request: Request):
        n, errors = forms.parse_triage_auto(await form_of(request))
        with closing(ctx.connect()) as conn:
            if errors:
                return settings_response(request, conn, 422, triage_error=errors["auto_top_n"])
            farm_db.put_triage_auto(conn, n, ctx.clock())
        return see_other(f"/settings?auto_triage={n}")

    @router.post("/settings/reset/{key}")
    async def reset_setting(request: Request, key: str):
        if key not in farm_db.SETTINGS:
            raise HTTPException(404, f"no setting {key!r}")
        with closing(ctx.connect()) as conn:
            farm_db.delete_setting(conn, key)
        return see_other(f"/settings?reset={key}")

    @router.post("/settings/unmute")
    async def unmute(request: Request):
        form = await form_of(request)
        try:
            kind = farm_db.MuteKind(form.get("kind"))
        except ValueError:
            raise HTTPException(400, "kind must be event or tag") from None
        value = form.get("value") or ""
        with closing(ctx.connect()) as conn:
            farm_db.unmute(conn, kind, value)
        return see_other("/settings?" + urlencode({"unmuted": form.get("label") or value}))

    @router.get("/partials/settings/farmer", response_class=HTMLResponse)
    def farmer_partial(request: Request):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            return ctx.templates.TemplateResponse(request, "partials/settings_farmer.html", {
                "now": now, **settings_context(conn, now)})

    @router.post("/farmer/stop", response_class=HTMLResponse)
    async def stop_farmer(request: Request):
        keep = (await form_of(request)).get("keep_orders") == "1"
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            farm_db.send_command(conn, CommandKind.STOP, now,
                                 {"keep_orders": True} if keep else None)
            return ctx.templates.TemplateResponse(request, "partials/settings_farmer.html", {
                "now": now, **settings_context(conn, now)})

    @router.post("/farmer/cancel-all")
    async def cancel_all(request: Request):
        errors = forms.parse_cancel_all(await form_of(request))
        with closing(ctx.connect()) as conn:
            if errors:
                return settings_response(request, conn, 422, cancel_error=errors["confirm"])
            paused = farm_db.cancel_all(conn)
        return see_other(f"/settings?cancelled={paused}")

    return router
