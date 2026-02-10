#!/bin/bash
# P4 Integration Tool - Quick Connect
#
# Usage: /proj/gfx_gct_lec_user0/nanyang2/p4-integration/connect.sh
#
# Automatically detects whether you're on the server or a remote machine,
# sets up SSH tunnel if needed, and opens the web UI in Firefox.

SERVER="atletx8-neu006"
PORT=5000
URL="http://localhost:$PORT"
CURRENT_HOST=$(hostname -s)

echo ""
echo "  P4 Integration Tool"
echo "  ==================="
echo ""

# Check if we're on the server itself
if [ "$CURRENT_HOST" = "$SERVER" ]; then
    # On the server — direct access
    if ! ss -tlnp 2>/dev/null | grep -q ":$PORT " && ! lsof -i :$PORT >/dev/null 2>&1; then
        echo "  [ERROR] Server is not running on port $PORT."
        echo ""
        echo "  Start it with:"
        echo "    cd /proj/gfx_gct_lec_user0/nanyang2/p4-integration"
        echo "    source venv/bin/activate"
        echo "    nohup python wsgi.py > /tmp/p4_integ_server.log 2>&1 &"
        echo ""
        exit 1
    fi

    echo "  Running on server ($SERVER). Opening browser..."
    firefox --new-tab "$URL" >/dev/null 2>&1 &

else
    # On a remote machine — need SSH tunnel
    echo "  Remote machine detected ($CURRENT_HOST)."
    echo "  Setting up SSH tunnel to $SERVER..."
    echo ""

    # Check if tunnel is already running
    if ss -tlnp 2>/dev/null | grep -q ":$PORT " || lsof -i :$PORT >/dev/null 2>&1; then
        echo "  Tunnel already active. Opening browser..."
        firefox --new-tab "$URL" >/dev/null 2>&1 &
    else
        USER=$(whoami)
        echo "  (Enter your password if prompted)"
        echo ""

        # -f: authenticate in foreground, then fork to background
        # -N: no remote command, just tunnel
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
        echo "  Opening browser..."
        firefox --new-tab "$URL" >/dev/null 2>&1 &
        echo ""
        echo "  To disconnect later:"
        echo "    kill \$(lsof -t -i :$PORT) 2>/dev/null"
    fi
fi

echo ""
echo "  ==========================================="
echo "  URL: $URL"
echo "  Log in with your P4 username & password."
echo "  ==========================================="
echo ""
