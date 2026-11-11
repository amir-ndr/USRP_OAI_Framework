#!/usr/bin/env bash
# Emergency stop for any experiment: ask every oaiho session/runner to stop OAI cleanly (UE, DUs, CU),
# then force remaining OAI groups. A session may stop its own core containers during cleanup;
# this broad emergency script itself issues no Docker command.
for p in $(pgrep -f '\.venv/bin/oaiho (session|run-node|probe|dashboar[d])'); do kill -TERM "$p" 2>/dev/null; done
sleep 10
for pg in $(ps -o pgid= -C nr-softmodem,nr-uesoftmodem 2>/dev/null | sort -u); do
  sudo -n /usr/bin/kill -TERM -- "-$pg" 2>/dev/null
done
sleep 5
for pg in $(ps -o pgid= -C nr-softmodem,nr-uesoftmodem 2>/dev/null | sort -u); do
  sudo -n /usr/bin/kill -KILL -- "-$pg" 2>/dev/null
done
pgrep -a -x 'nr-softmodem|nr-uesoftmodem' || echo "no OAI process running"
echo "If anything still transmits: switch the X310 off. Inspect docker ps separately for retained core containers."
