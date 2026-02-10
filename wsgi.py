import os
import sys

# Ensure this workspace path is first on sys.path so our local 'app' package is used
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug_env = os.environ.get("DEBUG") or os.environ.get("FLASK_DEBUG")
    debug = bool(int(debug_env)) if isinstance(debug_env, str) and debug_env.isdigit() else bool(debug_env)
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))

    # Optional TLS support:
    # - Set SSL=1 or SSL=true to use an ad-hoc self-signed cert
    # - Or set SSL="/path/cert.pem,/path/key.pem" to use real certs
    ssl_env = os.environ.get("SSL") or os.environ.get("FLASK_SSL")
    ssl_context = None
    if ssl_env:
        v = str(ssl_env).strip().lower()
        if v in ("1", "true", "yes", "adhoc"):
            ssl_context = "adhoc"
        elif "," in str(ssl_env):
            cert, key = str(ssl_env).split(",", 1)
            ssl_context = (cert.strip(), key.strip())

    app.run(host=host, port=port, debug=debug, ssl_context=ssl_context)
