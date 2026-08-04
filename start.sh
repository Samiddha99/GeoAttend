#!/usr/bin/env sh
set -e

echo "Starting the web server................."

# Migrations are NOT run here. They belong in fly.toml's release_command, so
# they run once per deploy rather than once per machine — every machine boots
# this script, and there is more than one.

# The project package is "config", not "project" — WSGI_APPLICATION says
# config.wsgi.application. Naming it wrong fails at import, so gunicorn never
# gets a worker up and the machine restart-loops.
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --config ./gunicorn.conf.py
