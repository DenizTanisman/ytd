#!/bin/zsh
# Lets launchd hold 127.0.0.1:8765. Nothing runs in the background: opening the page starts
# server.py, and it exits by itself after 5 idle minutes.
# Remove with:  launchctl bootout gui/$(id -u)/com.ismaildeniz.ytd && rm ~/Library/LaunchAgents/com.ismaildeniz.ytd.plist
set -e
dir="${0:A:h}"
plist="$HOME/Library/LaunchAgents/com.ismaildeniz.ytd.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ismaildeniz.ytd</string>
  <key>ProgramArguments</key>
  <array><string>$dir/.venv/bin/python</string><string>$dir/server.py</string></array>
  <key>WorkingDirectory</key><string>$dir</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>Sockets</key>
  <dict>
    <key>Listeners</key>
    <dict>
      <key>SockNodeName</key><string>127.0.0.1</string>
      <key>SockServiceName</key><string>8765</string>
      <key>SockFamily</key><string>IPv4</string>
      <key>SockType</key><string>stream</string>
    </dict>
  </dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/ytd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ytd.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/com.ismaildeniz.ytd" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "ytd hazır: http://127.0.0.1:8765 (sayfa açılınca başlar, 5 dk boşta kalınca kapanır)"
