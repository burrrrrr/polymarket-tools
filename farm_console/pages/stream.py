"""The event stream that tells an open page when to refresh its regions."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import farm_db
from tools import db as tools_db

from .. import views

KEEPALIVE_S = 15


def routes(ctx):
    router = APIRouter()

    @router.get("/stream")
    async def stream(request: Request):
        return StreamingResponse(signals(request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def signals(request):
        """Named events only, no HTML: `cycle` when the farmer finishes a cycle,
        `farm-event` when a farm_events row appears, `status` when the farmer or
        the worker starts or stops running, `job` when a worker job is queued,
        changes state or logs a line, `watch` when a sniper watch is armed,
        changes, is sampled or logs a line."""
        conn = ctx.connect()
        tools = ctx.connect_tools() if ctx.tools_db is not None else None
        try:
            def read():
                status = farm_db.read_farmer_status(conn)
                return (status and status.last_cycle_at, farm_db.last_event_id(conn),
                        views.liveness(status, ctx.clock()).running)

            def read_tools():
                if tools is None:
                    return None, False, None
                return (tools_db.last_job_change(tools),
                        tools_db.is_running(tools_db.read_worker_status(tools), ctx.clock()),
                        tools_db.watch_version(tools))

            cycle, event_id, running = read()
            job, working, watch = read_tools()
            yield ": connected\n\n"
            polls, quiet = 0, 0.0
            while ctx.max_polls is None or polls < ctx.max_polls:
                await asyncio.sleep(ctx.poll_s)
                polls += 1
                if await request.is_disconnected():
                    break
                new_cycle, new_event_id, new_running = read()
                new_job, new_working, new_watch = read_tools()
                out = []
                if new_cycle != cycle:
                    out.append(f"event: cycle\ndata: {farm_db.stamp(new_cycle)}\n\n")
                if new_event_id != event_id:
                    out.append(f"event: farm-event\ndata: {new_event_id}\n\n")
                if new_running != running:
                    out.append(f"event: status\ndata: {'running' if new_running else 'stopped'}"
                               f"\n\n")
                if new_working != working:
                    out.append(f"event: status\ndata: worker "
                               f"{'running' if new_working else 'stopped'}\n\n")
                if new_job != job:
                    out.append(f"event: job\ndata: {new_job[0]}.{new_job[1]}\n\n")
                if new_watch != watch:
                    out.append(f"event: watch\ndata: {'.'.join(map(str, new_watch))}\n\n")
                cycle, event_id, running = new_cycle, new_event_id, new_running
                job, working, watch = new_job, new_working, new_watch
                quiet = 0.0 if out else quiet + ctx.poll_s
                if quiet >= KEEPALIVE_S:
                    out.append(": keepalive\n\n")
                    quiet = 0.0
                for message in out:
                    yield message
        finally:
            conn.close()
            if tools is not None:
                tools.close()

    return router
