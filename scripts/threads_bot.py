"""株式会社ES Threads 自動投稿ボット（Threads公式API）

使い方:
  python scripts/threads_bot.py post      # 予定時刻を過ぎた投稿を1本だけ投稿
  python scripts/threads_bot.py insights  # 投稿済みの数値を取得して data/insights.csv に保存
  python scripts/threads_bot.py report    # 週次レポートを reports/ に作成
  python scripts/threads_bot.py refresh   # アクセストークンの期限を延長

環境変数:
  THREADS_TOKEN      Threadsの長期アクセストークン（GitHubのSecretsに保存）
  GITHUB_REPOSITORY  画像URLを作るためのリポジトリ名（GitHub Actionsが自動で設定）
  DRY_RUN=1          実際には投稿せず、内容だけ表示
"""
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "posts" / "schedule.json"
HISTORY = ROOT / "data" / "history.json"
INSIGHTS = ROOT / "data" / "insights.csv"
RESULTS = ROOT / "data" / "results.csv"
REPORTS = ROOT / "reports"
PAUSE = ROOT / "PAUSE"
API = "https://graph.threads.net/v1.0"
JST = dt.timezone(dt.timedelta(hours=9))
MAX_CHARS = 500
METRICS = ["views", "likes", "replies", "reposts", "quotes", "shares"]


def now():
    return dt.datetime.now(JST)


def load_json(path, default):
    # utf-8-sig: Windowsで保存したファイル先頭のBOMがあっても読めるように
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def text_hash(text):
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def api(method, path, params):
    url = path if path.startswith("http") else f"{API}/{path}"
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Threads APIエラー {e.code}: {body}") from None


def token():
    t = os.environ.get("THREADS_TOKEN", "").strip()
    if not t and not os.environ.get("DRY_RUN"):
        sys.exit("THREADS_TOKEN が設定されていません（GitHubのSecretsを確認してください）")
    return t


def image_url(image):
    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    return f"https://raw.githubusercontent.com/{repo}/main/{urllib.parse.quote(image)}"


def validate(post):
    errors = []
    for key in ("id", "at", "text"):
        if not post.get(key):
            errors.append(f"{key} がありません")
    if len(post.get("text", "")) > MAX_CHARS:
        errors.append(f"本文が{len(post['text'])}文字です（上限{MAX_CHARS}文字）")
    if post.get("image") and not (ROOT / post["image"]).exists():
        errors.append(f"画像が見つかりません: {post['image']}")
    return errors


def cmd_post():
    if PAUSE.exists():
        print("一時停止中です（PAUSEファイルがあります）。投稿しません。")
        return
    schedule = load_json(SCHEDULE, [])
    history = load_json(HISTORY, [])
    done_ids = {h["id"] for h in history}
    done_hashes = {h["hash"] for h in history}

    # 設定ミスは投稿前にまとめて止める
    problems = [f"{p.get('id')}: {e}" for p in schedule for e in validate(p)]
    ids = [p.get("id") for p in schedule]
    problems += [f"{i}: IDが重複しています" for i in set(ids) if ids.count(i) > 1]
    if problems:
        sys.exit("投稿予定に問題があります:\n" + "\n".join(problems))

    due = [p for p in schedule
           if p["id"] not in done_ids and not p.get("skip")
           and dt.datetime.fromisoformat(p["at"]) <= now()]
    if not due:
        print("投稿する予定はありません。")
        return
    post = sorted(due, key=lambda p: p["at"])[0]
    h = text_hash(post["text"])
    if h in done_hashes:
        # 同じ本文は二度投稿しない。記録だけ残して次へ
        history.append({"id": post["id"], "hash": h, "status": "skipped_duplicate",
                        "at": now().isoformat(timespec="seconds")})
        save_json(HISTORY, history)
        print(f"{post['id']}: 過去と同じ本文なのでスキップしました。")
        return

    print(f"投稿します: {post['id']}（予定 {post['at']}）")
    if os.environ.get("DRY_RUN"):
        print(post["text"])
        if post.get("image"):
            print("画像:", image_url(post["image"]))
        return

    t = token()
    params = {"text": post["text"], "access_token": t}
    if post.get("image"):
        params.update(media_type="IMAGE", image_url=image_url(post["image"]))
    else:
        params["media_type"] = "TEXT"
    container = api("POST", "me/threads", params)["id"]

    for _ in range(20):  # 画像の処理が終わるまで待つ（最大約5分）
        status = api("GET", container, {"fields": "status,error_message", "access_token": t})
        if status.get("status") == "FINISHED":
            break
        if status.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"投稿の準備に失敗: {status}")
        time.sleep(15)
    else:
        raise RuntimeError("投稿の準備が時間内に終わりませんでした")

    media_id = api("POST", "me/threads_publish", {"creation_id": container, "access_token": t})["id"]
    link = api("GET", media_id, {"fields": "permalink", "access_token": t}).get("permalink", "")
    history.append({"id": post["id"], "hash": h, "status": "posted", "media_id": media_id,
                    "permalink": link, "at": now().isoformat(timespec="seconds")})
    save_json(HISTORY, history)
    print(f"投稿しました: {link}")


def cmd_insights():
    t = token()
    history = [h for h in load_json(HISTORY, []) if h.get("status") == "posted"]
    rows = []
    for h in history:
        res = api("GET", f"{h['media_id']}/insights",
                  {"metric": ",".join(METRICS), "access_token": t})
        vals = {}
        for m in res.get("data", []):
            v = m.get("total_value", {}).get("value")
            if v is None and m.get("values"):
                v = m["values"][0].get("value")
            vals[m["name"]] = v
        rows.append({"取得日時": now().strftime("%Y-%m-%d %H:%M"), "投稿ID": h["id"],
                     "投稿日時": h["at"], "URL": h.get("permalink", ""),
                     **{m: vals.get(m, "") for m in METRICS}})
    new = not INSIGHTS.exists()
    INSIGHTS.parent.mkdir(parents=True, exist_ok=True)
    with INSIGHTS.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["取得日時", "投稿ID", "投稿日時", "URL", *METRICS])
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)}件の数値を保存しました。")


def cmd_report():
    latest = {}
    if INSIGHTS.exists():
        with INSIGHTS.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                latest[r["投稿ID"]] = r  # 最後に取得した値が残る
    inquiries, contracts = {}, {}
    if RESULTS.exists():
        with RESULTS.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                pid = (r.get("きっかけの投稿ID") or "不明").strip() or "不明"
                inquiries[pid] = inquiries.get(pid, 0) + 1
                if (r.get("契約") or "").strip() in ("はい", "yes", "1", "○"):
                    contracts[pid] = contracts.get(pid, 0) + 1

    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    ids = sorted(set(latest) | set(inquiries) - {"不明"},
                 key=lambda i: (-contracts.get(i, 0), -inquiries.get(i, 0),
                                -num(latest.get(i, {}).get("views"))))
    lines = [f"# 週次レポート（{now():%Y-%m-%d}作成）", "",
             "並び順：契約 → 問い合わせ → 閲覧数", "",
             "| 投稿ID | 閲覧 | いいね | 返信 | 再投稿 | シェア | 問い合わせ | 契約 |",
             "|---|---|---|---|---|---|---|---|"]
    for i in ids:
        r = latest.get(i, {})
        lines.append(f"| {i} | {r.get('views', '')} | {r.get('likes', '')} | {r.get('replies', '')} | "
                     f"{r.get('reposts', '')} | {r.get('shares', '')} | {inquiries.get(i, 0)} | {contracts.get(i, 0)} |")
    lines += ["", f"きっかけ不明の問い合わせ：{inquiries.get('不明', 0)}件（契約 {contracts.get('不明', 0)}件）",
              "", "## 良かった点・改善点・翌週の企画", "", "（Claudeがこの表をもとに記入します）", ""]
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{now():%Y-%m-%d}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"レポートを作成しました: {out.name}")


def cmd_refresh():
    t = token()
    res = api("GET", "https://graph.threads.net/refresh_access_token",
              {"grant_type": "th_refresh_token", "access_token": t})
    days = int(res.get("expires_in", 0)) // 86400
    if res.get("access_token") and res["access_token"] != t:
        sys.exit("新しいトークンが発行されました。GitHubのSecrets（THREADS_TOKEN）を更新してください。"
                 "（トークンはログに出していません）")
    print(f"トークンを延長しました（残り約{days}日）")


if __name__ == "__main__":
    commands = {"post": cmd_post, "insights": cmd_insights, "report": cmd_report, "refresh": cmd_refresh}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()
