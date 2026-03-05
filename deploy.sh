#!/bin/bash
set -e

echo "Pulling latest changes to /opt/bt-monitor..."
sudo git -C /opt/bt-monitor pull

echo "Restarting bt-scanner service..."
sudo systemctl restart bt-scanner.service

echo "Done. Service status:"
sudo systemctl status bt-scanner.service --no-pager -l
