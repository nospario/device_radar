#!/bin/bash
set -e

echo "Pulling latest changes to /opt/bt-monitor..."
sudo git -C /opt/bt-monitor pull

echo "Installing dependencies..."
pip install -r /opt/bt-monitor/requirements.txt --break-system-packages -q

echo "Restarting services..."
sudo systemctl restart bt-scanner.service
sudo systemctl restart bt-web.service
sudo systemctl restart bt-telegram.service

# Kitkat indexer (only restart if service is installed)
if systemctl list-unit-files bt-kitkat-indexer.service | grep -q bt-kitkat-indexer; then
    sudo systemctl restart bt-kitkat-indexer.service
    echo "Kitkat indexer restarted."
fi

echo "Done. Service status:"
sudo systemctl status bt-scanner.service bt-web.service bt-telegram.service --no-pager -l
