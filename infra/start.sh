#!/bin/sh
set -eu
# Production settings validate required environment before collecting or serving.
python apps/platform/manage.py collectstatic --noinput
exec gunicorn --config infra/gunicorn.conf.py config.wsgi:application
