from investos.main import app


def test_api_routes_do_not_require_trailing_slash_redirects():
    trailing_slash_routes = sorted(
        {
            route.path
            for route in app.routes
            if getattr(route, "path", "/") != "/" and route.path.endswith("/")
        }
    )

    assert trailing_slash_routes == [], (
        "API routes behind the Next.js proxy must use canonical paths without "
        "trailing slashes. Redirects expose the backend's loopback URL to remote "
        f"clients: {trailing_slash_routes}"
    )
