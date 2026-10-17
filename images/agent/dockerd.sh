#!/bin/sh
# The main process of a nested-Docker container (Sysbox), run as the container's root.
# Usage: boss-dockerd GID
#
# The daemon pulls through the gate by name. Containers it starts can't resolve "gate", so
# the client config gives them the gate's address instead. Only HTTPS is proxied, so plain
# HTTP between services on a compose network goes direct. The socket belongs to GID, so the
# agent (the host user's uid and gid) can use it.
set -eu
gate=$(getent hosts gate | cut -d' ' -f1)
mkdir -p /etc/boss-docker
cat > /etc/boss-docker/config.json <<JSON
{"proxies": {"default": {"httpsProxy": "http://$gate:3128", "noProxy": "localhost,127.0.0.1"}}}
JSON
exec dockerd --group "$1"
