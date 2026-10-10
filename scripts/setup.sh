#!/usr/bin/env bash

# Symlink each of this repo's skills into ~/.claude/skills/
# Safe to re-run; idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="${HOME}/.claude/skills"
mkdir -p "${SKILLS_DIR}"

# Helper: idempotently symlink ${1} (source) to ${2} (target).
link() {
    local source="${1}"
    local target="${2}"
    if [ ! -e "${source}" ]; then
        echo "skip:   ${source} does not exist in repo"
        return
    fi
    if [ -L "${target}" ]; then
        local existing
        existing="$(readlink "${target}")"
        if [ "${existing}" = "${source}" ]; then
            echo "ok:     ${target} -> ${source} (already linked)"
            return
        fi
        echo "relink: ${target} -> ${existing}  =>  ${source}"
        rm "${target}"
    elif [ -e "${target}" ]; then
        echo "ERROR:  ${target} exists and is not a symlink."
        echo "        Move or remove it before running this script."
        exit 1
    fi
    ln -s "${source}" "${target}"
    echo "linked: ${target} -> ${source}"
}

# One link per skill directory, so skills from other sources can coexist.
for skill in "${REPO}"/skills/*/; do
    skill="${skill%/}"
    link "${skill}" "${SKILLS_DIR}/$(basename "${skill}")"
done
echo
echo "Done. Restart Claude Code (or open a new session) to pick up new skills."
