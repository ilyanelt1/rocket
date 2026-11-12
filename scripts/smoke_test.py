import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8005").rstrip("/")
USERNAME = os.environ["ADMIN_USERNAME"]
PASSWORD = os.environ["ADMIN_PASSWORD"]


def request(opener, path, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(BASE_URL + path, data=body)
    return opener.open(req, timeout=10)


def main():
    anonymous = urllib.request.build_opener()
    response = request(anonymous, "/")
    if not response.geturl().endswith("/login"):
        raise RuntimeError("Anonymous dashboard request did not redirect to login")
    print("PASS: anonymous users are redirected to login")

    cookies = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    response = request(
        client,
        "/login",
        {"username": USERNAME, "password": PASSWORD},
    )
    if response.status != 200 or not response.geturl().endswith("/"):
        raise RuntimeError("Administrator login did not open the dashboard")
    dashboard = response.read().decode()
    if "SKU-101" not in dashboard:
        raise RuntimeError("Seeded product SKU-101 is missing from dashboard")
    print("PASS: administrator can log in and see seeded stock")

    response = request(client, "/api/reconcile/1")
    payload = json.loads(response.read())
    required = {"physical", "in_transit", "total"}
    if not required.issubset(payload):
        raise RuntimeError(f"Reconciliation response misses fields: {required - payload.keys()}")
    print("PASS: authenticated stock reconciliation API responds")

    response = request(client, "/logout", {})
    if not response.geturl().endswith("/login"):
        raise RuntimeError("Logout did not redirect to login")
    response = request(client, "/")
    if not response.geturl().endswith("/login"):
        raise RuntimeError("Logged-out user can still access dashboard")
    print("PASS: logout clears the authenticated session")


if __name__ == "__main__":
    try:
        main()
    except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
