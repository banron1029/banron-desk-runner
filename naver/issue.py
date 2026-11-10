#!/usr/bin/env python3
"""이슈 검색: 검색어 하나로 최근 이틀 치 네이버 뉴스를 받아 비슷한 기사끼리 묶는다.

환경 변수: ISSUE_QUERY(검색어), ISSUE_ID(요청 id), NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
결과: data/naver/issues/<ISSUE_ID>.json
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402

WINDOW_HOURS = 48
MAX_FETCH = 1000      # 네이버가 한 검색어에 내주는 최대 범위
KEEP_DAYS = 14
TOP_CLUSTERS = 30
ITEMS_PER_CLUSTER = 8
SIM = 0.34


def bigrams(title, query):
    t = re.sub(r"\[[^\]]{1,12}\]|\([^)]{1,12}\)", " ", title)       # [단독] (종합) 같은 머리말 제거
    t = t.replace(query, " ")
    t = re.sub(r"[^0-9A-Za-z가-힣]+", " ", t).lower()
    out = set()
    for w in t.split():
        if len(w) == 1:
            continue
        for i in range(len(w) - 1):
            out.add(w[i:i + 2])
    return out


def cluster(items, query):
    """제목의 글자 2개 묶음이 많이 겹치는 기사끼리 한 덩어리로 묶는다."""
    groups = []  # [{"members": [idx], "grams": [set,...]}]
    for i, it in enumerate(items):
        g = bigrams(it["title"], query)
        best, best_sim = None, 0.0
        if len(g) >= 3:
            for grp in groups:
                for other in grp["grams"][:6]:
                    inter = len(g & other)
                    if not inter:
                        continue
                    s = inter / float(len(g | other))
                    if s > best_sim:
                        best, best_sim = grp, s
        if best is not None and best_sim >= SIM:
            best["members"].append(i)
            best["grams"].append(g)
        else:
            groups.append({"members": [i], "grams": [g]})
    return [grp["members"] for grp in groups]


def build(query, raw_items, total, now):
    since = now - timedelta(hours=WINDOW_HOURS)
    items, seen = [], set()
    for it in raw_items:
        try:
            pub = parsedate_to_datetime(it.get("pubDate", ""))
        except Exception:
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=collect.KST)
        if pub < since:
            continue
        url = it.get("originallink") or it.get("link") or ""
        key = re.sub(r"[#?].*$", "", url).rstrip("/").lower()
        if not url or key in seen:
            continue
        seen.add(key)
        items.append({
            "title": collect.clean(it.get("title")),
            "desc": collect.clean(it.get("description"), 140),
            "url": url,
            "outlet": collect.host_of(url),
            "publishedAt": pub.astimezone(collect.KST).isoformat(timespec="seconds"),
        })
    clusters = []
    for members in cluster(items, query):
        ms = [items[i] for i in members]
        ms.sort(key=lambda x: x["publishedAt"])
        clusters.append({
            "size": len(ms),
            "outlets": len({m["outlet"] for m in ms}),
            "firstAt": ms[0]["publishedAt"], "lastAt": ms[-1]["publishedAt"],
            "items": ms[:ITEMS_PER_CLUSTER],
        })
    clusters.sort(key=lambda c: (c["size"], c["lastAt"]), reverse=True)
    big = [c for c in clusters if c["size"] >= 2][:TOP_CLUSTERS]
    singles = [c["items"][0] for c in clusters if c["size"] == 1]
    return {
        "query": query,
        "collectedAt": now.isoformat(timespec="seconds"),
        "windowStart": since.isoformat(timespec="seconds"),
        "naverTotal": total,
        "fetched": len(raw_items),
        "inWindow": len(items),
        "clusters": big,
        "singleCount": len(singles),
        "singleSamples": [{"title": s["title"], "outlet": s["outlet"], "url": s["url"], "publishedAt": s["publishedAt"]} for s in singles[:40]],
    }


def main():
    query = re.sub(r"\s+", " ", os.environ.get("ISSUE_QUERY", "")).strip()
    rid = os.environ.get("ISSUE_ID", "").strip()
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
        print("요청 id 형식이 올바르지 않습니다.", file=sys.stderr)
        return 2
    if not (1 <= len(query) <= 40):
        print("검색어는 1~40자여야 합니다.", file=sys.stderr)
        return 2
    if not cid or not secret:
        print("네이버 인증키가 설정되지 않았습니다.", file=sys.stderr)
        return 2
    now = datetime.now(collect.KST)
    since = now - timedelta(hours=WINDOW_HOURS)
    base = collect.API[collect.kind_of(secret)]["news"]
    raw, total, start, reached_old, error = [], 0, 1, False, ""
    while start <= MAX_FETCH and not reached_old:
        q = urllib.parse.urlencode({"query": query, "display": 100, "start": start, "sort": "date"})
        try:
            page = collect.call(base + "?" + q, cid, secret)
        except Exception as e:
            error = str(e)
            break
        total = int(page.get("total") or 0)
        got = page.get("items", [])
        raw.extend(got)
        if len(got) < 100:
            break
        try:
            last = parsedate_to_datetime(got[-1].get("pubDate", ""))
            reached_old = last < since
        except Exception:
            pass
        start += 100
        time.sleep(0.12)
    if not raw and error:
        out = {"query": query, "collectedAt": now.isoformat(timespec="seconds"), "error": error, "clusters": []}
    else:
        out = build(query, raw, total, now)
        # 1,000건을 다 받았는데도 이틀 전까지 닿지 못했으면 일부만 본 것이다
        out["truncated"] = (not reached_old) and len(raw) >= MAX_FETCH
        if error:
            out["partialError"] = error
    out["id"] = rid
    collect.write(os.path.join("issues", rid + ".json"), out)
    # 오래된 결과 정리
    idir = os.path.join(collect.ROOT, "data", "naver", "issues")
    limit = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(idir):
        path = os.path.join(idir, name)
        if name.endswith(".json") and os.path.getmtime(path) < limit and name != rid + ".json":
            os.remove(path)
    print("검색어 처리 완료: 받은 기사 %d건 · 기간 안 %d건 · 묶음 %d개" % (
        out.get("fetched", 0), out.get("inWindow", 0), len(out.get("clusters", []))))
    return 0 if not out.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
