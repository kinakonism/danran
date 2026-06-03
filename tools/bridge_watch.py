#!/usr/bin/env python3
"""
danran bridge 外部死活監視 — GitHub Actions の cron から定期実行する。

mini の bridge が heartbeat（public.ai_status.updated_at）を更新しているので、
それが一定時間より古ければ「bridge 停止（mini 電源オフ/ネット断/プロセス死）」と判断し、
まさとに Web Push を送る。連投を防ぐため ai_status.alerted で1停止につき1回だけ通知し、
復活したらフラグを戻して「復活」通知を送る。

env（GitHub Actions secrets で渡す）:
  SUPABASE_URL           例: https://xxxx.supabase.co （非機密）
  SUPABASE_ANON_KEY      anon キー
  VAPID_PRIVATE_KEY      Web Push 秘密鍵（RAW base64url）
  VAPID_SUBJECT          例: mailto:you@example.com
  OWNER_NAME             既定 "まさと"（users.name から uid を引く）
  STALE_MIN              既定 10（分）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

URL  = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
KEY  = os.environ.get("SUPABASE_ANON_KEY") or ""
PRIV = os.environ.get("VAPID_PRIVATE_KEY") or ""
SUBJ = os.environ.get("VAPID_SUBJECT") or ""
OWNER_NAME = os.environ.get("OWNER_NAME") or "まさと"
STALE_MIN  = int(os.environ.get("STALE_MIN") or "10")
HDR = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + "/rest/v1/" + path, data=data, headers=HDR, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def owner_uid():
    import urllib.parse
    rows = api("GET", "users?select=id&name=eq." + urllib.parse.quote(OWNER_NAME) + "&limit=1") or []
    return rows[0]["id"] if rows else ""


def push_owner(title, body):
    from pywebpush import webpush
    uid = owner_uid()
    if not uid:
        print("owner uid not found"); return
    subs = api("GET", "push_subscriptions?select=endpoint,p256dh,auth&user_id=eq." + uid) or []
    payload = json.dumps({"title": title, "body": body, "url": "/"}, ensure_ascii=False)
    sent = 0
    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"],
                                       "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    data=payload, vapid_private_key=PRIV, vapid_claims={"sub": SUBJ})
            sent += 1
        except Exception as e:
            print("push err:", e)
    print(f"pushed to {sent} device(s)")


def main():
    if not (URL and KEY and PRIV and SUBJ):
        print("env 不足（SUPABASE_URL/ANON_KEY/VAPID_*）。何もせず終了。")
        return
    rows = api("GET", "ai_status?select=updated_at,alerted&order=id&limit=1") or []
    if not rows:
        print("ai_status 行なし"); return
    row = rows[0]
    ts = (row.get("updated_at") or "").replace("Z", "+00:00")
    try:
        last = datetime.fromisoformat(ts)
    except Exception:
        print("updated_at パース不可:", ts); return
    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    alerted = bool(row.get("alerted"))
    print(f"heartbeat age = {age_min:.1f} min / alerted={alerted} / threshold={STALE_MIN}")

    if age_min > STALE_MIN:
        if not alerted:
            push_owner("⚠️ danran AI が停止中",
                       f"bridge の応答が {int(age_min)} 分ありません。Mac mini と bridge を確認してください。")
            api("PATCH", "ai_status?id=eq.1", {"alerted": True})
            print("→ 停止アラート送信＆alerted=true")
        else:
            print("→ 停止中だが通知済み（スキップ）")
    else:
        if alerted:
            push_owner("✅ danran AI が復活", "bridge の応答が戻りました。")
            api("PATCH", "ai_status?id=eq.1", {"alerted": False})
            print("→ 復活通知＆alerted=false")
        else:
            print("→ 正常")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("bridge_watch err:", e)
        sys.exit(0)   # 監視失敗でワークフローを赤くしない
