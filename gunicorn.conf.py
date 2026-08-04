# https://docs.gunicorn.org/en/stable/settings.html

import multiprocessing

print(f"CPU Count: {multiprocessing.cpu_count()}")

# bind = "0.0.0.0:8080"
forwarded_allow_ips = "*"
# workers = multiprocessing.cpu_count()*(2 + 1)
workers = 1
# threads = multiprocessing.cpu_count()*(2 + 1)
threads = 3 # each of the worker could handle 3 requests at a time.
worker_class = 'gthread'
worker_connections = 1000
timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = 'info'
capture_output = True
# reload = True
reload_engine = 'auto'