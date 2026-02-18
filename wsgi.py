"""
WSGI entry point for production (e.g. Gunicorn on Render).
Run: gunicorn --bind 0.0.0.0:$PORT wsgi:app
"""
import os
from pathlib import Path

# Ensure we run from the directory containing config and data files
wsgi_dir = Path(__file__).resolve().parent
os.chdir(wsgi_dir)

from indian_stock_monitor import load_config, create_app

load_config()
app = create_app()
