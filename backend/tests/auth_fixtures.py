"""服务器隔离认证夹具；供应商替身不证明真实收信。"""

from test_authentication import BASE, sms_login


def login_response(client):
    current = client.get(BASE + "/session")
    return current if current.status_code == 200 else sms_login(client)


def login(client):
    return {"X-CSRF-Token": login_response(client).json()["csrf"]}
