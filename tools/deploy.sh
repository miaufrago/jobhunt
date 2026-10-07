#!/bin/sh
# Deploy one instance. Every instance-specific value is an environment variable,
# so a second board for a different person is the same script with a different
# env file -- nothing in here is specific to one candidate.
#
#   ./tools/deploy.sh                      code only, ~3s restart
#   ./tools/deploy.sh --rebuild            full image rebuild, ~10s down
#   INSTANCE=jobs-b ./tools/deploy.sh      deploy a different instance
#   ./tools/deploy.sh --env instances/b.env
#
# A full rebuild is needed only when requirements.txt, the Dockerfile, or the
# container's own config (network, IP, volumes) changes. Code alone does not.
set -e

# --env <file> loads instance settings before anything else.
if [ "$1" = "--env" ]; then
  . "$2"
  shift 2
fi

HOST="${JOBHUNT_HOST:-user@your-docker-host}"
INSTANCE="${INSTANCE:-jobhunt}"
IP="${JOBHUNT_IP:-192.168.1.10}"
NETWORK="${JOBHUNT_NETWORK:-br0}"
DATA="${JOBHUNT_DATA_DIR:-/srv/appdata/$INSTANCE}"
SRC="${JOBHUNT_SRC_DIR:-/srv/appdata/$INSTANCE-src}"
TZ_NAME="${JOBHUNT_TZ:-Etc/UTC}"
IMAGE="${JOBHUNT_IMAGE:-jobhunt:latest}"
PROXY="${JOBHUNT_PROXY_CONTAINER:-your-reverse-proxy}"
SCALE="${POLITENESS_SCALE:-1.0}"

# Pin the MAC, derived from the IP. Docker randomises a container's MAC whenever
# the container is recreated, and a DHCP server that holds reservations tracks
# devices by MAC. An unpinned container therefore becomes a new device on every
# rebuild, and any reservation held against its old MAC stops matching.
#
# The scheme is Docker's own: the 02:42 locally-administered prefix followed by
# the four address octets in hex. 192.168.1.10 becomes 02:42:0a:00:00:0a, and
# 192.168.1.50 becomes 02:42:c0:a8:01:32. Any subnet works, and the MAC is
# readable straight back to the address.
MAC="${JOBHUNT_MAC:-$(printf '02:42:%02x:%02x:%02x:%02x' \
  "$(echo "$IP" | cut -d. -f1)" "$(echo "$IP" | cut -d. -f2)" \
  "$(echo "$IP" | cut -d. -f3)" "$(echo "$IP" | cut -d. -f4)")}"

echo "instance=$INSTANCE ip=$IP data=$DATA"

tar --exclude=.venv --exclude=data --exclude=out --exclude=__pycache__ \
    --exclude=.git --exclude='*.pyc' --exclude=docs --exclude=tools \
    -czf /tmp/$INSTANCE.tgz jobhunt config.example.yaml requirements.txt Dockerfile \
    docker-entrypoint.sh docker-compose.yml README.md .env.example profile .claude

ssh "$HOST" "mkdir -p $SRC $DATA"
scp -q /tmp/$INSTANCE.tgz "$HOST:$SRC/src.tgz"

if [ "$1" = "--rebuild" ]; then
  echo "full rebuild -- the container will be down for about 10 seconds"
  ssh "$HOST" "cd $SRC && tar xzf src.tgz && rm src.tgz \
    && docker build -q -t $IMAGE . \
    && docker rm -f $INSTANCE >/dev/null 2>&1; \
    docker run -d --name $INSTANCE --restart unless-stopped \
      --network $NETWORK --ip $IP --mac-address $MAC -v $DATA:/data \
      -e TZ=$TZ_NAME -e INSTANCE_NAME=$INSTANCE -e POLITENESS_SCALE=$SCALE \
      ${APP_PASSWORD:+-e APP_PASSWORD=$APP_PASSWORD} \
      ${API_TOKEN:+-e API_TOKEN=$API_TOKEN} \
      $IMAGE >/dev/null"
else
  echo "code deploy -- about 3 seconds of downtime"
  ssh "$HOST" "cd $SRC && tar xzf src.tgz && rm src.tgz \
    && docker cp $SRC/jobhunt $INSTANCE:/app/ \
    && docker cp $SRC/config.example.yaml $INSTANCE:/app/config.default.yaml \
    && docker restart $INSTANCE >/dev/null"
fi

echo "waiting for health..."
i=0
while [ $i -lt 12 ]; do
  if ssh "$HOST" "docker exec $PROXY curl -sf -m 5 http://$IP:8080/healthz" >/dev/null 2>&1; then
    echo "up"
    ssh "$HOST" "docker exec $PROXY curl -s -m 5 http://$IP:8080/api/stats"
    echo
    exit 0
  fi
  i=$((i + 1))
  sleep 2
done
echo "not healthy after 24s -- check: ssh $HOST docker logs $INSTANCE --tail 30"
exit 1
