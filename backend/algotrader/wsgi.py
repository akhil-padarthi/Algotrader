"""Production entry point: gunicorn "algotrader.wsgi:app"."""
import logging

from .api import create_app
from .config import load_settings, setup_logging

setup_logging()
logging.getLogger("werkzeug").setLevel(logging.WARNING)
settings = load_settings()
settings.validate()
settings = settings.guard_exposed_dashboard(exposed=True)   # gunicorn serves the network (no automatic admin)
if settings.manual_trading_note:
    logging.getLogger("algotrader").warning(settings.manual_trading_note)
app = create_app(settings)
