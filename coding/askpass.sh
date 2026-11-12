#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) printf '%s\n' "$AOF_GIT_TOKEN" ;;
  *) exit 1 ;;
esac
