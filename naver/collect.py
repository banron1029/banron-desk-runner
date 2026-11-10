#!/usr/bin/env python3
"""네이버 뉴스 검색·데이터랩(검색어 트렌드) 수집기.

인증키는 환경 변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 로만 받는다.
결과는 data/naver/news.json, data/naver/trends.json 에 쓴다.
표준 라이브러리만 사용한다.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = timezone(timedelta(hours=9))
NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
TAG = re.compile(r"<[^>]+>")


def clean(text, limit=None):
    s = html.unescape(TAG.sub("", text or "")).strip()
    s = re.sub(r"\s+", " ", s)
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def host_of(url):
    h = urllib.parse.urlparse(url or "").netloc.lower()
    return re.sub(r"^(www|m|news)\.", "", h)


def call(url, cid, secret, body=None, tries=3):
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP %s %s" % (e.code, detail)
            if e.code in (400, 401, 403, 404):  # 다시 시도해도 같은 결과
                break
        except Exception as e:  # 네트워크 오류 등
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(last)


def parse_news(raw, keyword, since, per_keyword):
    """뉴스 검색 응답에서 기간 안의 기사만 골라 정리한다."""
    items, seen = [], set()
    for it in raw.get("items", []):
        try:
            pub = parsedate_to_datetime(it.get("pubDate", ""))
        except Exception:
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=KST)
        if pub < since:
            continue
        url = it.get("originallink") or it.get("link") or ""
        key = re.sub(r"[#?].*$", "", url).rstrip("/").lower()
        if not url or key in seen:
            continue
        seen.add(key)
        items.append({
            "title": clean(it.get("title")),
            "desc": clean(it.get("description"), 110),
            "url": url,
            "naverUrl": it.get("link") if "news.naver.com" in (it.get("link") or "") else "",
            "outlet": host_of(url),
            "publishedAt": pub.astimezone(KST).isoformat(timespec="seconds"),
        })
        if len(items) >= per_keyword:
            break
    return {"keyword": keyword, "total": int(raw.get("total") or 0), "items": items}


def collect_news(cfg, cid, secret, now):
    since = now - timedelta(hours=cfg.get("windowHours", 36))
    per = cfg.get("perKeyword", 8)
    out, errors = [], []
    for g in cfg.get("groups", []):
        kws = []
        for kw in g.get("keywords", []):
            q = urllib.parse.urlencode({"query": kw, "display": 50, "start": 1, "sort": "date"})
            try:
                kws.append(parse_news(call(NEWS_URL + "?" + q, cid, secret), kw, since, per))
            except Exception as e:
                errors.append({"keyword": kw, "error": str(e)})
                kws.append({"keyword": kw, "total": 0, "items": [], "failed": True})
            time.sleep(0.12)
        out.append({"category": g.get("category", ""), "keywords": kws})
    return {
        "collectedAt": now.isoformat(timespec="seconds"),
        "windowStart": since.isoformat(timespec="seconds"),
        "windowHours": cfg.get("windowHours", 36),
        "groups": out,
        "errors": errors,
    }


def collect_trends(cfg, cid, secret, now):
    end = (now - timedelta(days=1)).date()  # 당일 값은 미완성이라 전날까지
    start = end - timedelta(days=cfg.get("days", 30) - 1)
    sets, errors = [], []
    for s in cfg.get("sets", []):
        groups = s.get("groups", [])[:5]  # 한 번에 비교할 수 있는 주제어는 최대 5개
        body = {
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "timeUnit": cfg.get("timeUnit", "date"),
            "keywordGroups": [{"groupName": g["name"], "keywords": g["keywords"][:20]} for g in groups],
        }
        try:
            raw = call(DATALAB_URL, cid, secret, body)
            series = [{
                "name": r.get("title", ""), "keywords": r.get("keywords", []),
                "data": [{"period": d["period"], "ratio": round(float(d["ratio"]), 2)} for d in r.get("data", [])],
            } for r in raw.get("results", [])]
            sets.append({"name": s.get("name", ""), "series": series})
        except Exception as e:
            errors.append({"set": s.get("name", ""), "error": str(e)})
            sets.append({"name": s.get("name", ""), "series": [], "failed": True})
        time.sleep(0.12)
    return {
        "collectedAt": now.isoformat(timespec="seconds"),
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "timeUnit": cfg.get("timeUnit", "date"),
        "sets": sets, "errors": errors,
    }


def write(name, obj):
    path = os.path.join(ROOT, "data", "naver", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 설정되지 않았습니다. "
              "저장소 Settings > Secrets and variables > Actions 에 두 값을 등록하세요.", file=sys.stderr)
        return 2
    with open(os.path.join(ROOT, "naver", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    now = datetime.now(KST)
    news = collect_news(cfg.get("news", {}), cid, secret, now)
    # 데이터랩을 다른 애플리케이션으로 등록한 경우 전용 키를 쓴다(없으면 검색 키를 같이 쓴다)
    dl_id = os.environ.get("NAVER_DATALAB_CLIENT_ID", "").strip() or cid
    dl_secret = os.environ.get("NAVER_DATALAB_CLIENT_SECRET", "").strip() or secret
    trends = collect_trends(cfg.get("trends", {}), dl_id, dl_secret, now)
    n_items = sum(len(k["items"]) for g in news["groups"] for k in g["keywords"])
    n_kw = sum(len(g["keywords"]) for g in news["groups"])
    ok_sets = sum(1 for s in trends["sets"] if not s.get("failed"))
    print("뉴스: 검색어 %d개 · 기사 %d건 · 실패 %d" % (n_kw, n_items, len(news["errors"])))
    print("트렌드: %d/%d 묶음 · 실패 %d" % (ok_sets, len(trends["sets"]), len(trends["errors"])))
    for e in news["errors"] + trends["errors"]:
        print("  실패:", json.dumps(e, ensure_ascii=False), file=sys.stderr)
    # 한쪽이 전부 실패하면 기존 파일을 덮어쓰지 않는다
    news_ok = n_kw > 0 and len(news["errors"]) < n_kw
    trends_ok = ok_sets > 0
    if news_ok:
        write("news.json", news)
    if trends_ok:
        write("trends.json", trends)
    return 0 if (news_ok and trends_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
