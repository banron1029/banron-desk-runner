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
# 키 종류에 따라 호출 주소와 인증 헤더가 다르다.
#  - hub: 네이버 클라우드 NAVER API HUB에서 발급한 키(Secret 40자)
#  - legacy: 네이버 개발자센터(openapi.naver.com)에서 발급한 키
API = {
    "hub": {
        "news": "https://naverapihub.apigw.ntruss.com/search/v1/news",
        "trend": "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
        "id": "X-NCP-APIGW-API-KEY-ID", "secret": "X-NCP-APIGW-API-KEY",
    },
    "legacy": {
        "news": "https://openapi.naver.com/v1/search/news.json",
        "trend": "https://openapi.naver.com/v1/datalab/search",
        "id": "X-Naver-Client-Id", "secret": "X-Naver-Client-Secret",
    },
}


def kind_of(secret):
    return "hub" if len(secret) >= 30 else "legacy"
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
    api = API[kind_of(secret)]
    headers = {api["id"]: cid, api["secret"]: secret}
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
                kws.append(parse_news(call(API[kind_of(secret)]["news"] + "?" + q, cid, secret), kw, since, per))
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


def chain_trends(groups, fetch, anchor_name=None):
    """주제어를 5개씩 나눠 조회하되, 묶음마다 같은 기준 주제어를 넣어 값의 눈금을 하나로 맞춘다.

    네이버는 한 번에 5개까지만 비교해 주고 값은 그 5개 안의 상대값이다.
    기준 주제어가 매 조회에 들어가므로 그 비율로 나머지를 환산할 수 있다.
    fetch(groups) -> {이름: {날짜: 값}}
    """
    order = list(groups)
    if anchor_name:  # 지정한 기준 주제어를 맨 앞으로
        order.sort(key=lambda g: g["name"] != anchor_name)
    first = order[:5]
    res = fetch(first)
    total = lambda d: sum(d.values())
    anchor = max(first, key=lambda g: total(res.get(g["name"], {})))
    if anchor_name:
        anchor = first[0]
    base = total(res.get(anchor["name"], {}))
    series = {g["name"]: res.get(g["name"], {}) for g in first}
    low = []
    rest = order[5:]
    for i in range(0, len(rest), 4):
        part = rest[i:i + 4]
        r = fetch([anchor] + part)
        a = total(r.get(anchor["name"], {}))
        if a <= 0 or base <= 0:
            low.extend(g["name"] for g in part)
            for g in part:
                series[g["name"]] = {}
            continue
        f = base / a
        if a < 30:  # 기준 주제어가 너무 작게 잡혀 환산 오차가 커질 수 있음
            low.extend(g["name"] for g in part)
        for g in part:
            series[g["name"]] = {k: v * f for k, v in r.get(g["name"], {}).items()}
    top = max([v for d in series.values() for v in d.values()] or [0])
    k = 100.0 / top if top > 0 else 1.0
    periods = sorted({p for d in series.values() for p in d})
    out = [{
        "name": g["name"], "keywords": g["keywords"],
        "values": [round(series[g["name"]].get(p, 0.0) * k, 2) for p in periods],
    } for g in groups]
    out.sort(key=lambda x: sum(x["values"][-7:]), reverse=True)
    return periods, out, anchor["name"], low


def collect_trends(cfg, cid, secret, now):
    end = (now - timedelta(days=1)).date()  # 당일 값은 미완성이라 전날까지
    start = end - timedelta(days=cfg.get("days", 30) - 1)
    sets, errors = [], []

    def fetch(groups):
        body = {
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "timeUnit": cfg.get("timeUnit", "date"),
            "keywordGroups": [{"groupName": g["name"], "keywords": g["keywords"][:20]} for g in groups],
        }
        raw = call(API[kind_of(secret)]["trend"], cid, secret, body)
        time.sleep(0.15)
        return {r.get("title", ""): {d["period"]: float(d["ratio"]) for d in r.get("data", [])}
                for r in raw.get("results", [])}

    for s in cfg.get("sets", []):
        try:
            gs = s.get("groups", [])[:20]
            periods, series, anchor, low = chain_trends(gs, fetch)
            # 1위가 기준 주제어가 아니면 1위를 기준으로 다시 조회해 오차를 줄인다
            if series and series[0]["name"] != anchor and len(gs) > 5:
                periods, series, anchor, low = chain_trends(gs, fetch, series[0]["name"])
            sets.append({"name": s.get("name", ""), "periods": periods, "series": series,
                         "anchor": anchor, "lowPrecision": low})
        except Exception as e:
            errors.append({"set": s.get("name", ""), "error": str(e)})
            sets.append({"name": s.get("name", ""), "periods": [], "series": [], "failed": True})
    return {
        "collectedAt": now.isoformat(timespec="seconds"),
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "timeUnit": cfg.get("timeUnit", "date"),
        "sets": sets, "errors": errors,
    }


def merge_days(news, cfg, now):
    """이번 수집 결과를 게시일(한국시간)별 파일 data/naver/days/날짜.json 에 누적한다."""
    ddir = os.path.join(ROOT, "data", "naver", "days")
    os.makedirs(ddir, exist_ok=True)
    cap = cfg.get("perKeywordPerDay", 20)
    days = {}

    def load(date):
        if date not in days:
            path = os.path.join(ddir, date + ".json")
            try:
                with open(path, encoding="utf-8") as f:
                    days[date] = json.load(f)
            except Exception:
                days[date] = {"date": date, "groups": []}
            days[date]["_changed"] = False
        return days[date]

    def slot(day, category, keyword):
        g = next((x for x in day["groups"] if x["category"] == category), None)
        if g is None:
            g = {"category": category, "keywords": []}
            day["groups"].append(g)
        k = next((x for x in g["keywords"] if x["keyword"] == keyword), None)
        if k is None:
            k = {"keyword": keyword, "items": []}
            g["keywords"].append(k)
        return k

    added = 0
    for g in news["groups"]:
        for k in g["keywords"]:
            for it in k["items"]:
                day = load(it["publishedAt"][:10])
                tgt = slot(day, g["category"], k["keyword"])
                if any(x["url"] == it["url"] for x in tgt["items"]):
                    continue
                tgt["items"].append(it)
                tgt["items"].sort(key=lambda x: x["publishedAt"], reverse=True)
                del tgt["items"][cap:]
                day["_changed"] = True
                added += 1
    for date, day in days.items():
        if day.pop("_changed"):
            day["updatedAt"] = now.isoformat(timespec="seconds")
            day["count"] = sum(len(k["items"]) for g in day["groups"] for k in g["keywords"])
            write(os.path.join("days", date + ".json"), day)
    # 보관 기간이 지난 날짜 파일 정리
    limit = (now - timedelta(days=cfg.get("keepDays", 30))).date().isoformat()
    for name in os.listdir(ddir):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name) and name[:10] < limit:
            os.remove(os.path.join(ddir, name))
    return added


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
    # 값 자체는 찍지 않고 글자 수만 남긴다(개발자센터 키는 ID 20자·Secret 10자, API HUB 키는 ID 10자·Secret 40자)
    print("키 종류 — 검색 %s, 데이터랩 %s" % (kind_of(secret), kind_of(os.environ.get("NAVER_DATALAB_CLIENT_SECRET", "").strip() or secret)))
    print("키 길이 — 검색 ID %d자 / Secret %d자, 데이터랩 ID %d자 / Secret %d자" % (
        len(cid), len(secret),
        len(os.environ.get("NAVER_DATALAB_CLIENT_ID", "").strip()),
        len(os.environ.get("NAVER_DATALAB_CLIENT_SECRET", "").strip())))
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
        print("날짜별 묶음에 새로 추가: %d건" % merge_days(news, cfg.get("news", {}), now))
    if trends_ok:
        write("trends.json", trends)
    return 0 if (news_ok and trends_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
