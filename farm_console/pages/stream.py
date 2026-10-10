"""The event stream that tells an open page when to refresh its regions."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import farm_db

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
        `farm-event` when a farm_events row appears, `status` when the farmer
        starts or stops running."""
        conn = ctx.connect()
        try:
            def read():
                status = farm_db.read_farmer_status(conn)
                return (status and status.last_cycle_at, farm_db.last_event_id(conn),
                        views.liveness(status, ctx.clock()).running)

            cycle, event_id, running = read()
            yield ": connected\n\n"
            polls, quiet = 0, 0.0
            while ctx.max_polls is None or polls < ctx.max_polls:
                await asyncio.sleep(ctx.poll_s)
                polls += 1
                if await request.is_disconnected():
                    break
                new_cycle, new_event_id, new_running = read()
                out = []
                if new_cycle != cycle:
                    out.append(f"event: cycle\ndata: {farm_db.stamp(new_cycle)}\n\n")
                if new_event_id != event_id:
                    out.append(f"event: farm-event\ndata: {new_event_id}\n\n")
                if new_running != running:
                    out.append(f"event: status\ndata: {'running' if new_running else 'stopped'}"
                               f"\n\n")
                cycle, event_id, running = new_cycle, new_event_id, new_running
                quiet = 0.0 if out else quiet + ctx.poll_s
                if quiet >= KEEPALIVE_S:
                    out.append(": keepalive\n\n")
                    quiet = 0.0
                for message in out:
                    yield message
        finally:
            conn.close()

    return router
