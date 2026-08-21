#!/bin/bash
# The Lambda handler, which is a shell script because the Lambda Web Adapter runs the
# server as a real HTTP server rather than calling a Python handler function. That is the
# whole reason the adapter is here: server/app.py is the same ASGI app uvicorn serves
# locally, unchanged, with no handler-shaped wrapper around it.
#
# Reached because the function's handler is set to `run.sh` and AWS_LAMBDA_EXEC_WRAPPER
# points at /opt/bootstrap, which the adapter layer provides.
set -euo pipefail

# /var/task is where the zip is unpacked, and it holds both the repository code and its
# dependencies. The runtime's own PYTHONPATH points at /var/runtime, so this prepends
# rather than replaces — clobbering it would hide the runtime's boto3.
export PYTHONPATH="${LAMBDA_TASK_ROOT:-/var/task}:${PYTHONPATH:-}"

exec python3 -m uvicorn server.app:app \
    --host 0.0.0.0 \
    --port "${AWS_LWA_PORT:-8000}" \
    --log-level info
