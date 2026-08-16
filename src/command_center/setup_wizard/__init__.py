"""First-run setup wizard. Self-contained — remove this package and the
single `setup_wizard.register(app)` call in app.py to remove the wizard
entirely; nothing else in the app depends on it.
"""

from command_center.setup_wizard.register import register

__all__ = ["register"]
