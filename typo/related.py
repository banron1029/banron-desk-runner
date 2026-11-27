"""팩트 체크용: 반론보도닷컴 기사마다 같은 소식을 다룬 다른 매체 기사를 네이버 뉴스 검색으로 모은다.

네이버 검색 결과의 제목·요약(description)만 쓴다. 다른 매체 사이트에는 접속하지 않는다.
인증키는 환경 변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 로만 받는다.
"""
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "naver"))
import collect  # noqa: E402

# 의견·연재 성격이라 다른 매체와 사실을 견주기 어려운 섹션
SKIP_SECTIONS = ("오피니언", "전문가칼럼", "전문가필진", "문화休", "만평")
PER_ARTICLE = 12
WINDOW_DAYS = 3


def core_phrase(text, limit=38):
    """제목·부제에서 검색어로 쓸 핵심 구절을 뽑는다."""
    t = re.sub(r"\[[^\]]{1,20}\]|\([^)]{1,20}\)|【[^】]{1,20}】", " ", text or "")
    t = re.sub(r"[\"'“”‘’「」『』]", " ", t)
    t = re.split(r"\.{2,}|…|·{3,}| - |\n", t)[0]
    t = re.sub(r"\s+(外|등)$", "", t.strip())
    t = re.sub(r"[^0-9A-Za-z가-힣%.\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    return t


def queries_for(a):
    qs = []
    for src in [a.get("title", "")] + (a.get("subtitle", "") or "").split("\n")[:2]:
        q = core_phrase(src)
        if len(q) >= 6 and q not in qs:
            qs.append(q)
    return qs[:3]


def search(q, cid, secret):
    api = collect.API[collect.kind_of(secret)]
    url = api["news"] + "?" + urllib.parse.urlencode({"query": q, "display": 30, "sort": "sim"})
    return collect.call(url, cid, secret)


def attach_related(articles, log):
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        log.append("네이버 인증키가 없어 다른 매체 검색을 건너뜀")
        return 0
    calls = 0
    for a in articles:
        sec = (a.get("section") or "") + " " + " ".join(a.get("crumbs") or [])
        if any(s in sec for s in SKIP_SECTIONS):
            a["related"] = {"skipped": "의견·연재 섹션", "queries": [], "items": []}
            continue
        pub = datetime.fromisoformat(a["publishedAt"]) if a.get("publishedAt") else None
        items, seen, qs, errs = [], set(), queries_for(a), []
        for q in qs:
            try:
                raw = search(q, cid, secret)
                calls += 1
            except Exception as e:  # noqa: BLE001
                errs.append(f"{q}: {str(e)[:120]}")
                continue
            time.sleep(0.3)
            for it in raw.get("items", []):
                link = it.get("originallink") or it.get("link") or ""
                host = collect.host_of(link)
                if not link or "banronbodo" in host:
                    continue
                key = re.sub(r"[#?].*$", "", link).rstrip("/").lower()
                if key in seen:
                    continue
                try:
                    when = parsedate_to_datetime(it.get("pubDate", ""))
                except Exception:  # noqa: BLE001
                    continue
                if pub and abs(when - pub) > timedelta(days=WINDOW_DAYS):
                    continue
                seen.add(key)
                items.append({
                    "query": q,
                    "outlet": host,
                    "title": collect.clean(it.get("title")),
                    "desc": collect.clean(it.get("description"), 220),
                    "url": link,
                    "naverUrl": it.get("link") if "news.naver.com" in (it.get("link") or "") else "",
                    "publishedAt": when.astimezone(collect.KST).isoformat(timespec="seconds"),
                })
        a["related"] = {"queries": qs, "items": items[:PER_ARTICLE], "errors": errs}
    return calls
