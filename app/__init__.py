"""
EMS Analytics Dashboard - Application Factory
Flask application with Blueprint architecture for scalability.
"""
import os
import secrets
import warnings

from flask import Flask

try:
    from flask_cors import CORS
    _HAS_CORS = True
except ImportError:
    _HAS_CORS = False


def create_app(config_name=None):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static"
    )

    secret_key = os.environ.get("SECRET_KEY")
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    if not secret_key:
        if debug:
            secret_key = secrets.token_hex(32)
            warnings.warn(
                "SECRET_KEY not set; using a random ephemeral key for this "
                "debug run. Set SECRET_KEY in the environment for anything "
                "beyond local development."
            )
        else:
            raise RuntimeError(
                "SECRET_KEY environment variable must be set (see .env.example)."
            )
    app.config["SECRET_KEY"] = secret_key
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "uploads"
    )
    app.config["DATA_FOLDER"] = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data"
    )
    app.config["ALLOWED_EXTENSIONS"] = {"xlsx", "xls", "csv"}
    app.config["DEBUG"] = debug

    if _HAS_CORS:
        CORS(app, resources={r"/api/*": {"origins": "*"}})

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["DATA_FOLDER"], exist_ok=True)

    from app.routes.main import main_bp
    from app.routes.api import api_bp
    from app.routes.upload import upload_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(upload_bp, url_prefix="/upload")

    return app
