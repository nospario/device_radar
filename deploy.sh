#!/bin/bash
set -e

echo "Pulling latest changes to /opt/bt-monitor..."
sudo git -C /opt/bt-monitor pull

echo "Restarting services..."
sudo systemctl restart bt-scanner.service
sudo systemctl restart bt-telegram.service

echo "Done. Service status:"
sudo systemctl status bt-scanner.service bt-telegram.service --no-pager -l
