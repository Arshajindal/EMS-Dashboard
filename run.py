"""
Entry point – run with:
    python run.py
or for production:
    gunicorn "run:app" -w 1 -b 0.0.0.0:5000
"""
import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"\n{'='*55}")
    print(f"  EMS Analytics Dashboard")
    print(f"  Running on http://0.0.0.0:{port}")
    print(f"  Debug mode: {debug}")
    print(f"{'='*55}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
