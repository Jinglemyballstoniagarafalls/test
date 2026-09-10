"""
Main entry point for the site.

This file stays deliberately thin: it creates the Flask app, serves
the main site's own pages/static assets, and registers each service's
Blueprint. All service-specific logic lives inside services/<name>/.

To add a new service:
  1. Create services/<new_service>/__init__.py with its own Blueprint
     (copy services/acquaintance/__init__.py as a starting template).
  2. Import it below and register it with app.register_blueprint(...).
"""
import os

from flask import Flask, send_from_directory

from services.acquaintance import acquaintance_bp
from services.scrvi import scrvi_bp 

# --------------------------------------------
# CREATE FLASK APP
# --------------------------------------------
app = Flask(__name__, static_folder='static', template_folder='templates')

# --------------------------------------------
# REGISTER SERVICES
# --------------------------------------------
app.register_blueprint(acquaintance_bp)
app.register_blueprint(scrvi_bp)
# app.register_blueprint(some_other_service_bp)   # <- future services go here

# --------------------------------------------
# MAIN SITE ROUTES
# --------------------------------------------
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# --------------------------------------------
# RUN
# --------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # threaded=True lets this process handle multiple concurrent
    # requests (e.g. several people using different services, or the
    # same service, at the same time) instead of queuing them one at
    # a time. For real production traffic, run behind gunicorn with
    # multiple workers instead of this dev server, e.g.:
    #   gunicorn -w 4 --threads 2 -b 0.0.0.0:5000 app:app
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
