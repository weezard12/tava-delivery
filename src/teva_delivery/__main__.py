"""``python -m teva_delivery``: serve the API with Uvicorn.

One process only: the background worker and the live-update bus live in it. The access log
is the service's own (method, path, status, duration), so Uvicorn's is turned off.
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="teva-delivery")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - inside the container
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "teva_delivery.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        ws_ping_interval=None,
    )


if __name__ == "__main__":
    main()
