#!/bin/sh
# Install the systemd user units. Usage:
#   deploy/install.sh            install units, start the dashboard
#   deploy/install.sh --timer    also enable the scheduled pipeline
# Undo: systemctl --user disable --now resume-tailor.timer resume-tailor-dashboard.service
set -eu
cd "$(dirname "$0")"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
for u in resume-tailor.service resume-tailor.timer resume-tailor-dashboard.service; do
    ln -sf "$PWD/$u" "$UNITS/$u"
done

# Encrypted Gmail credential for runs when the keyring is locked (optional).
CRED="$HOME/.config/resume-tailor/gmail-app-password.cred"
if [ -f "$CRED" ]; then
    mkdir -p "$UNITS/resume-tailor.service.d"
    printf '[Service]\nLoadCredentialEncrypted=gmail-app-password:%s\n' "$CRED" \
        > "$UNITS/resume-tailor.service.d/credentials.conf"
fi

systemctl --user daemon-reload
systemctl --user enable --now resume-tailor-dashboard.service
if [ "${1:-}" = "--timer" ]; then
    systemctl --user enable --now resume-tailor.timer
    systemctl --user list-timers resume-tailor.timer
fi
echo "dashboard: http://localhost:8501"
