#!/bin/sh
# Containers run with a read-only root filesystem and an empty tmpfs at /tmp, as the host
# user, who has no home in the image. Make the home directory, then run the command.
set -eu
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
# In a nested-Docker box, boss-dockerd wrote the proxy settings for the Docker client. The
# client keeps state beside its config, so it gets a copy in the home directory.
if [ -f /etc/boss-docker/config.json ] && [ ! -f "$HOME/.docker/config.json" ]; then
  mkdir -p "$HOME/.docker"
  cp /etc/boss-docker/config.json "$HOME/.docker/config.json"
fi
exec "$@"
