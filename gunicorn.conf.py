# https://docs.gunicorn.org/en/stable/settings.html

import multiprocessing

print(f"CPU Count: {multiprocessing.cpu_count()}")

# bind = "0.0.0.0:8080"
forwarded_allow_ips = "*"
# workers = multiprocessing.cpu_count()*(2 + 1)
workers = 1
# threads = multiprocessing.cpu_count()*(2 + 1)
threads = 3 # each of the worker could handle 3 requests at a time.
# Overridden on the command line by -k uvicorn.workers.UvicornWorker, which is
# what serves the WebSocket. Left here so a WSGI run still has a sane default.
worker_class = 'uvicorn_worker.UvicornWorker'
worker_connections = 1000
# A WebSocket is a long-lived connection by definition; the old 30s would cut
# a student off mid-verification. Uvicorn does not apply this to sockets, but
# face inference on a slow request should not be reaped either.
timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = 'info'
capture_output = True
# reload = True
reload_engine = 'auto'