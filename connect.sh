#!/bin/bash
# P4 Integration Tool - Quick Connect
#
# Usage: /proj/gfx_gct_lec_user0/nanyang2/p4-integration/connect.sh
#
# Sets up SSH port forwarding and opens the web UI in Firefox.

SERVER="atletx8-neu006"
PORT=5000
URL="http://localhost:$PORT"

echo ""
echo "  P4 Integration Tool"
echo "  ==================="
echo ""

# Check if tunnel is already running
if ss -tlnp 2>/dev/null | grep -q ":$PORT " || lsof -i :$PORT >/dev/null 2>&1; then
    echo "  Already connected! Opening browser..."
    firefox --new-tab "$URL" >/dev/null 2>&1 &
    echo ""
    echo "  URL: $URL"
    echo ""
    exit 0
fi

USER=$(whoami)

echo "  Connecting to $SERVER..."
echo "  (Enter your password if prompted)"
echo ""

# Start SSH tunnel
# Using -o ExitOnForwardFailure=yes so SSH exits if port forwarding fails
# Using -o ServerAliveInterval=60 to keep the tunnel alive
ssh -N \
    -L $PORT:localhost:$PORT \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=60 \
    "$USER@$SERVER" &

SSH_PID=$!

# Wait a moment for SSH to establish the tunnel
sleep 2

# Check if SSH is still running (it would have died if auth failed)
if ! kill -0 $SSH_PID 2>/dev/null; then
    echo "  [ERROR] SSH connection failed."
    echo ""
    echo "  Make sure you can SSH to $SERVER:"
    echo "    ssh $USER@$SERVER"
    echo ""
    exit 1
fi

echo ""
echo "  Connected! Opening browser..."

# Open in new tab (works whether Firefox is already running or not)
firefox --new-tab "$URL" >/dev/null 2>&1 &

echo ""
echo "  ==========================================="
echo "  URL: $URL"
echo "  Log in with your P4 username & password."
echo "  ==========================================="
echo ""
echo "  SSH tunnel running (PID: $SSH_PID)"
echo "  To disconnect: kill $SSH_PID"
echo ""
