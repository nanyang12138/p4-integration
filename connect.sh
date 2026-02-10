#!/bin/bash
# P4 Integration Tool - Quick Connect
#
# Usage: /proj/gfx_gct_lec_user0/nanyang2/p4-integration/connect.sh
#
# Opens the P4 Integration web UI in Firefox.
# If direct access fails, falls back to SSH tunnel.

SERVER="atletx8-neu006"
PORT=5000
URL="http://$SERVER:$PORT"

echo ""
echo "  P4 Integration Tool"
echo "  ==================="
echo ""
echo "  URL: $URL"
echo ""

# Try to open browser if display is available
if [ -n "$DISPLAY" ]; then
    echo "  Opening browser..."
    timeout 3 firefox --new-tab "$URL" >/dev/null 2>&1 &
else
    echo "  No graphical display detected."
    echo "  Please open the URL above in your browser."
fi

echo ""
echo "  Log in with your P4 username & password."
echo ""
