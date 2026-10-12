"""La Goyniela — FastAPI application entrypoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.admin import router as admin_jornadas_router
from app.api.v1.admin_uploads import router as admin_uploads_router
from app.api.v1.auth import router as auth_router
from app.api.v1.competitions import router as competition_router
from app.api.v1.groups import router as groups_router
from app.api.v1.health import router as health_router
from app.api.v1.jornadas import read_router as jornadas_router
from app.api.v1.losilla import _close_client, router as losilla_router
from app.api.v1.porras import router as porras_router
from app.api.v1.teams import router as teams_router
from app.api.v1.user_uploads import router as user_uploads_router
from app.core.config import settings
from app.scheduler.lifespan import lifespan as scheduler_lifespan


async def lifespan(app: FastAPI):
    """Startup/shutdown hooks. Drives the scheduler and closes Losilla client.

    We compose the scheduler lifespan with the Losilla teardown. The
    scheduler's startup runs first; on shutdown we let APScheduler drain
    first and then close the Losilla HTTP client.
    """
    async with scheduler_lifespan(app):
        yield
    await _close_client()


app = FastAPI(
    title="La Goyniela API",
    version="0.1.0",
    description=(
        "Backend API for the La Goyniela peña quiniela organizer. "
        "Serves competition data, jornada composition, team metadata, "
        "and (later) group voting. See docs/proyecto/arquitectura.md."
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness/readiness probes"},
        {"name": "competitions", "description": "Competitions / leagues / cups"},
        {"name": "teams", "description": "Team identities (clubs + national teams)"},
        {"name": "auth", "description": "User authentication (local / Google / X)"},
        {"name": "groups", "description": "Peña groups with admin/member roles"},
        {"name": "jornadas",
         "description": "Quiniela jornadas (boleto, schedule, escrutinio)"},
        {"name": "losilla",
         "description": "Eduardo Losilla read-only mirror (escrutinio + comentarios)"},
        {"name": "porras",
         "description": "MVP-05 per-(group, jornada) porras + free-form voting + media + boleto"},
        {"name": "admin-uploads",
         "description": "Image uploads (team logos) — system-admin only"},
        {"name": "admin-jornadas",
         "description": "Jornada configuration + superadmin panel — superadmin only"},
        {"name": "admin-users",
         "description": "Superadmin user-management endpoints"},
        {"name": "admin-teams",
         "description": "Superadmin team-management endpoints (CRUD + soft delete)"},
        {"name": "users",
         "description": "Per-user actions (avatar upload)"},
    ],
)

app.include_router(health_router)
app.include_router(competition_router)
app.include_router(teams_router)
app.include_router(auth_router)
app.include_router(groups_router)
app.include_router(jornadas_router)
app.include_router(losilla_router)
# MVP-05: the per-(group, jornada) porra endpoints replace the old
# ``/groups/{gid}/jornadas/{jid}/picks`` + ``/open`` + ``/optimize`` +
# ``/cost`` + ``/consensus`` routes. Picks are now scoped to a porra, and
# the format lives on the porra (not on the jornada).
app.include_router(porras_router)
app.include_router(admin_uploads_router)
app.include_router(admin_jornadas_router)
app.include_router(user_uploads_router)

# Slice 2 of the superadmin-panel sprint: rate-limit middleware + the 4
# superadmin-only routers (users, teams, jornadas, composicion). The
# middleware is registered BEFORE the routers so it sees the path and
# can short-circuit with 429 before any of the handlers run. The
# routers are imported + included in the order they were written; the
# 4 new ones share the ``/api/v1/admin/...`` prefix so the rate
# limiter matches them via ``ADMIN_PATH_PREFIX``.
from app.middleware.rate_limit import AdminRateLimitMiddleware  # noqa: E402

app.add_middleware(AdminRateLimitMiddleware)  # noqa: E402

from app.api.v1.admin_composicion import (  # noqa: E402
    router as admin_composicion_router,
)
from app.api.v1.admin_ingest import (  # noqa: E402
    router as admin_ingest_router,
)
from app.api.v1.admin_jornadas import (  # noqa: E402
    router as admin_jornadas_superadmin_router,
)
from app.api.v1.admin_teams import (  # noqa: E402
    router as admin_teams_superadmin_router,
)
from app.api.v1.admin_users import (  # noqa: E402
    router as admin_users_superadmin_router,
)

app.include_router(admin_users_superadmin_router)
app.include_router(admin_teams_superadmin_router)
app.include_router(admin_jornadas_superadmin_router)
app.include_router(admin_composicion_router)
app.include_router(admin_ingest_router)

# Static mount — used only by the local storage backend. When
# STORAGE_BACKEND=s3 the files live in a bucket and don't need a mount,
# but the mount is still cheap and harmless (the directory might not
# even exist for s3 deployments). The test fixture in
# tests/test_image_uploads.py rebinds the route's StaticFiles to a tmp
# path during tests, so this mount MUST always be created at startup.
Path(settings.storage_local_path).mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/uploads",
    StaticFiles(directory=settings.storage_local_path),
    name="uploads",
)