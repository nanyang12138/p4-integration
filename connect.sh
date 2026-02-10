#!/bin/bash
set +H  # Disable bash history expansion (! in passwords)

# P4 Integration Tool - Quick Connect
#
# Usage: /proj/gfx_gct_lec_user0/nanyang2/p4-integration/connect.sh
#
# On the server: opens browser directly
# On other machines: sets up SSH tunnel, then opens browser

SERVER="atletx8-neu006"
PORT=5000
CURRENT_HOST=$(hostname | cut -d. -f1)

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
        echo ""

        # Start SSH in background using nohup, let user type password interactively
        ssh -N \
            -L $PORT:localhost:$PORT \
            -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=60 \
            "$USER@$SERVER" &
        SSH_PID=$!

        # Wait for tunnel to establish (give time for password entry)
        echo ""
        echo "  Waiting for tunnel to establish..."
        sleep 3

        if ! kill -0 $SSH_PID 2>/dev/null; then
            echo "  [ERROR] SSH connection failed."
            echo ""
            echo "  Make sure you can SSH to $SERVER:"
            echo "    ssh $USER@$SERVER"
            echo ""
            exit 1
        fi

        echo "  SSH tunnel established (PID: $SSH_PID)."
        open_browser "$URL"
        echo ""
        echo "  To disconnect later:"
        echo "    kill $SSH_PID"
    fi
fi

echo ""
echo "  ==========================================="
echo "  URL: $URL"
echo "  Log in with your P4 username & password."
echo "  ==========================================="
echo ""
