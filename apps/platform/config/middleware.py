from django.conf import settings


class ResponseSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = (
            "default-src 'none'; script-src 'self'; script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if settings.SECURE_SSL_REDIRECT:
            policy += "; upgrade-insecure-requests"
        response.headers["Content-Security-Policy"] = policy
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # Withdrawal must take effect on every request, including browser history.
        response.headers["Cache-Control"] = "no-store"
        return response
