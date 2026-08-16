"""Single entry point — `setup_wizard.register(app)` adds the wizard's
routes and the gating middleware to an existing FastAPI app. To remove
the wizard entirely: delete this call from app.py, delete this package.
Nothing else references it.
"""

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from command_center.setup_wizard import invites, status
from command_center.setup_wizard import state as state_module
from command_center.setup_wizard.router import router, settings_router, templates


def register(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(settings_router)

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static"):
            return await call_next(request)

        # Called through the module, not a frozen `from...import` name, so
        # monkeypatching status.is_setup_complete in tests actually takes
        # effect here.
        complete = status.is_setup_complete()
        if not complete:
            if not path.startswith("/setup"):
                return RedirectResponse(url="/setup", status_code=303)
            # Every /setup* request needs a still-valid invite, not just
            # the first — see invites.request_is_invited's docstring for
            # why this re-checks on every request instead of trusting a
            # one-time check. /settings/invites (the admin page that
            # creates these) never reaches here: it's outside /setup, so
            # it's already covered by the redirect above while
            # incomplete, and reachable normally once setup is done.
            state = state_module.load()
            if not invites.request_is_invited(state, request.query_params.get("invite")):
                return templates.TemplateResponse(
                    request, "setup/invalid_invite.html", {}, status_code=403
                )

        # Deliberately no "complete -> bounce /setup back to /" rule: once
        # set up, /setup stays reachable so you can go back in and change
        # a value (profile, Groq key, etc). Only an incomplete instance
        # gets forced there.
        return await call_next(request)
