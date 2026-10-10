#!/bin/sh
# Containers run with a read-only root filesystem and an empty tmpfs at /tmp, as the host
# user, who has no home in the image. Make the home directory, then run the command.
set -eu
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
exec "$@"
