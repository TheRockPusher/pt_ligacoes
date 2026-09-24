import os

bind = f"0.0.0.0:{int(os.environ.get('PORT', '8000'))}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = 4
timeout = 30
graceful_timeout = 30
keepalive = 5
# Do not log URL queries, IP addresses, cookies, or editorial request content.
accesslog = None
errorlog = "-"
capture_output = True
max_requests = 1000
max_requests_jitter = 100
