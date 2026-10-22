"""/research and /targets — plus the Pursuits section they belong to.

The Pursuits section is one nav-rail entry over five pages (Map,
Competitions, Research, Targets, Projects), tied together by the tab row
in partials/pursuits_tabs.html. Every feature router keeps its own
Jinja2Templates instance (see mindmap/router.py for why), so each one that
renders a Pursuits page calls install_template_globals on its own.
"""

from fastapi.templating import Jinja2Templates

from command_center import queries, timestamps


def install_template_globals(templates: Jinja2Templates) -> None:
    templates.env.globals["pursuits_tab_counts"] = queries.pursuits_tab_counts
    timestamps.install(templates)
