#!/bin/bash
set +H  # Disable bash history expansion (! in passwords)

# P4 Integration Tool - Quick Connect
#
# Usage: /proj/gfx_gct_lec_user0/nanyang2/p4-integration/connect.sh
#
# On the server: opens browser directly
# On other machines: sets up SSH tunnel, then opens browser

PORT=5000
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
MASTER_FILE="$SCRIPT_DIR/data/master_host"
CURRENT_HOST=$(hostname | cut -d. -f1)

# Read master hostname from file (written by wsgi.py on startup)
if [ ! -f "$MASTER_FILE" ]; then
    echo ""
    echo "  [ERROR] Server not started yet."
    echo "  Please start the server first on the target machine:"
    echo "    cd $SCRIPT_DIR"
    echo "    source venv/bin/activate"
    echo "    python wsgi.py"
    echo ""
    exit 1
fi
SERVER=$(cat "$MASTER_FILE")

echo ""
echo "  P4 Integration Tool"
echo "  ==================="
echo ""

open_browser() {
    local url="$1"
    if [ -n "$DISPLAY" ]; then
        firefox --new-tab "$url" >/dev/null 2>&1 &
        disown
        echo "  Opening browser..."
    else
        echo "  No graphical display detected."
        echo "  Please open the URL in your browser manually."
    fi
}

if [ "$CURRENT_HOST" = "$SERVER" ]; then
    # On the server — direct access
    URL="http://$SERVER:$PORT"
    echo "  On server ($SERVER). Direct access."
    open_browser "$URL"

else
    # On a remote machine — use SSH tunnel with localhost
    URL="http://localhost:$PORT"
    echo "  Remote machine ($CURRENT_HOST)."
    echo ""

    # Check if tunnel is already running
    if ss -tlnp 2>/dev/null | grep -q ":$PORT " || lsof -i :$PORT >/dev/null 2>&1; then
        echo "  SSH tunnel already active."
        open_browser "$URL"
    else
        USER=$(whoami)
        echo "  Setting up SSH tunnel to $SERVER..."
        echo "  (Enter your password if prompted)"
        echo ""

        # -f: authenticate in foreground, then fork to background
        ssh -f -N \
            -L $PORT:localhost:$PORT \
            -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=60 \
            "$USER@$SERVER"

        if [ $? -ne 0 ]; then
            echo "  [ERROR] SSH connection failed."
            echo ""
            echo "  Make sure you can SSH to $SERVER:"
            echo "    ssh $USER@$SERVER"
            echo ""
            exit 1
        fi

        echo ""
        echo "  SSH tunnel established."
        open_browser "$URL"
        echo ""
        echo "  To disconnect later:"
        echo "    kill \`lsof -t -i :$PORT\` 2>/dev/null"
    fi
fi

echo ""
echo "  ==========================================="
echo "  URL: $URL"
echo "  Log in with your P4 username & password."
echo "  ==========================================="
echo ""
