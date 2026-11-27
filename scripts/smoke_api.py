#!/usr/bin/env python3
"""End-to-end API smoke test over real HTTP. Stdlib only.

    docker compose exec backend python manage.py seed_demo --password 'pick-a-password'
    python3 scripts/smoke_api.py --password 'pick-a-password'
"""
import argparse
import http.cookiejar
import json
import urllib.error
import urllib.request
import uuid


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "csrftoken"), "")

    def call(self, method, path, body=None):
        data = None if method == "GET" else json.dumps(body or {}).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-CSRFToken": self.csrf()},
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)


def expect(result, wanted, what):
    status, body = result
    assert status == wanted, f"{what}: expected {wanted}, got {status}: {body}"
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--password", required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    api = Api(args.base)

    api.call("GET", "/api/auth/csrf/")
    me = expect(
        api.call("POST", "/api/auth/login/", {"username": "manager", "password": args.password}),
        200,
        "login",
    )
    print("logged in as", me["username"], "(manager)" if me["is_manager"] else "")

    if expect(api.call("GET", "/api/shifts/current/"), 200, "current shift")["shift"] is None:
        expect(api.call("POST", "/api/shifts/open/", {"opening_float": "1000.00"}), 201, "open shift")
        print("opened a shift")

    menu = expect(api.call("GET", "/api/menu/"), 200, "menu")
    item = next(
        i for c in menu["categories"] for i in c["items"] if i["is_available"] and not i["modifier_groups"]
    )

    def charged_order():
        order_id = str(uuid.uuid4())
        expect(api.call("POST", "/api/orders/", {"id": order_id}), 201, "create order")
        expect(
            api.call("POST", f"/api/orders/{order_id}/items/", {"menu_item": item["id"], "quantity": 2}),
            201,
            "add item",
        )
        return expect(api.call("POST", f"/api/orders/{order_id}/charge/"), 200, "charge")

    order = charged_order()
    key = str(uuid.uuid4())
    path = f"/api/orders/{order['id']}/payments/cash/"
    body = {"tendered": "1000.00", "idempotency_key": key}
    first = expect(api.call("POST", path, body), 201, "cash payment")
    again = expect(api.call("POST", path, body), 200, "cash replay")
    assert first["payment"]["id"] == again["payment"]["id"], "replay created a second payment"
    print(f"{order['order_label']}: cash payment confirmed, change {first['payment']['change_given']}; replay safe")

    order = charged_order()
    code = uuid.uuid4().hex[:10].upper()
    manual = expect(
        api.call(
            "POST",
            f"/api/orders/{order['id']}/payments/manual-mpesa/",
            {"receipt_code": code, "amount": order["total"], "idempotency_key": str(uuid.uuid4())},
        ),
        201,
        "manual M-PESA",
    )
    assert manual["order"]["status"] == "paid_pending_verification"
    print(f"{order['order_label']}: manual M-PESA recorded as {manual['payment']['status']} (code {code})")

    print("sync status:", expect(api.call("GET", "/api/sync/status/"), 200, "sync status"))
    print("\nTo see reconciliation confirm it, record this code in the M-PESA records:")
    print(f'  curl -X POST http://localhost:8001/cloud/mpesa/transactions/ -H "Authorization: Bearer $KEY" \\')
    print(f"    -H 'Content-Type: application/json' -d '{{\"receipt_code\":\"{code}\",\"amount\":\"{order['total']}\"}}'")


if __name__ == "__main__":
    main()