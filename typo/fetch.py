#!/usr/bin/env python3
"""오타 체크용 원문 수집: 반론보도닷컴에서 시간 범위 안 기사의 제목·부제·본문·사진 설명을 받는다.

환경 변수:
  TYPO_ID     요청 id (예: r_20261127_1). 결과 파일 이름에 쓴다.
  TYPO_START  범위 시작, ISO 8601 (+09:00)
  TYPO_END    범위 끝(미포함), ISO 8601 (+09:00)
  TYPO_EXTRA  범위와 상관없이 다시 받을 기사 번호, 쉼표로 구분 (선택, 최대 5개)
결과: data/typo/<TYPO_ID>.json

사이트가 빠른 조회를 차단하므로 모든 요청은 하나씩, 2초 이상 간격으로 보낸다.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

BASE = "https://www.banronbodo.com"
KST = timezone(timedelta(hours=9))
GAP = 2.2            # 요청 사이 최소 간격(초)
MAX_LIST_PAGES = 6
MAX_ARTICLES = 40
KEEP_DAYS = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_last = [0.0]
requests_made = [0]


class Blocked(Exception):
    pass


def get(url):
    """2초 간격을 지켜 한 페이지를 받는다. 403·429·연결 오류면 60초 쉬고 한 번만 다시 시도."""
    for attempt in (1, 2):
        wait = GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        requests_made[0] += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                cs = r.headers.get_content_charset() or "utf-8"
                return raw.decode(cs, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503) and attempt == 1:
                time.sleep(60)
                continue
            if e.code in (403, 429, 503):
                raise Blocked(f"HTTP {e.code}")
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 1:
                time.sleep(60)
                continue
            raise Blocked(str(e))


def parse_iso(s):
    return datetime.fromisoformat(s).astimezone(KST)


class ArticleParser(HTMLParser):
    """본문 영역(article-view-content-div) 안의 문단과 사진 설명, 부제를 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.stack = []          # (tag, role)
        self.body_depth = None
        self.paras, self.captions, self.subs, self.heads = [], [], [], []
        self.buf = None
        self.buf_kind = None
        self.crumbs = []
        self.in_crumb = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = a.get("property") or a.get("name")
            if k and "content" in a:
                self.meta.setdefault(k, a["content"])
            return
        if tag in ("br", "img", "hr", "input", "link"):
            if tag == "br" and self.buf is not None:
                self.buf.append("\n")
            return
        cls = a.get("class", "") or ""
        idv = a.get("id", "") or ""
        role = None
        if idv == "article-view-content-div" or "article-body" in cls.split():
            if self.body_depth is None:
                self.body_depth = len(self.stack) + 1
                role = "body"
        if self.buf is None:
            if "subheading" in cls or "sub-title" in cls or "subtitle" in cls:
                self.buf, self.buf_kind, role = [], "sub", "cap"
            elif "heading" in cls.split() and tag in ("h1", "h2", "h3"):
                self.buf, self.buf_kind, role = [], "head", "cap"
            elif self.body_depth is not None and tag == "figcaption":
                self.buf, self.buf_kind, role = [], "caption", "cap"
            elif self.body_depth is not None and tag == "p":
                self.buf, self.buf_kind, role = [], "para", "cap"
        if "breadcrumb" in cls or "location" in cls.split() or "article-head-path" in cls:
            if self.in_crumb is None:
                self.in_crumb = len(self.stack) + 1
        self.stack.append((tag, role))

    def handle_endtag(self, tag):
        if tag in ("br", "img", "hr", "input", "link", "meta"):
            return
        # 닫는 태그와 맞는 위치까지 되감는다(잘못 닫힌 HTML 대비)
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                idx = i
                break
        if idx is None:
            return
        while len(self.stack) > idx:
            t, role = self.stack.pop()
            depth = len(self.stack) + 1
            if role == "cap" and self.buf is not None:
                text = re.sub(r"[ \t ]+", " ", "".join(self.buf)).strip()
                text = re.sub(r"\s*\n\s*", "\n", text)
                if text:
                    {"sub": self.subs, "head": self.heads, "caption": self.captions, "para": self.paras}[self.buf_kind].append(text)
                self.buf, self.buf_kind = None, None
            if role == "body" and depth == self.body_depth:
                self.body_depth = None
            if self.in_crumb is not None and depth == self.in_crumb:
                self.in_crumb = None

    def handle_data(self, data):
        if self.buf is not None:
            self.buf.append(data)
        elif self.in_crumb is not None:
            t = data.strip()
            if t and t not in (">", "›", "HOME", "홈"):
                self.crumbs.append(t)
        elif self.body_depth is not None and data.strip():
            # p 바깥에 바로 들어 있는 본문 글
            self.paras.append(re.sub(r"\s+", " ", data).strip())


JUNK = re.compile(r"\$\(function|addEventListener|copyToClipboard|ImageViewer|^다른기사 보기$")
NOISE = re.compile(r"(무단전재|재배포 금지|저작권자|Copyright|@[A-Za-z0-9.-]+\.(?:com|kr|net))")


def extract(idxno, page):
    p = ArticleParser()
    p.feed(page)
    m = p.meta
    title = html.unescape(m.get("og:title", "")).strip()
    title = re.sub(r"\s*-\s*반론보도닷컴\s*$", "", title)
    if p.heads:
        title = p.heads[0]
    pub = m.get("article:published_time") or ""
    mod = m.get("article:modified_time") or ""
    if not pub:
        mm = re.search(r"입력\s*(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})", page)
        if mm:
            pub = "%s-%s-%sT%s:%s:00+09:00" % mm.groups()
    if not mod:
        mm = re.search(r"수정\s*(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})", page)
        if mm:
            mod = "%s-%s-%sT%s:%s:00+09:00" % mm.groups()
    paras = [t for t in p.paras if (not NOISE.search(t) or len(t) > 80) and not JUNK.search(t)]
    # 본문 끝의 '관련기사' 이후(관련기사 목록·기자 이름·다른기사 보기)는 버린다
    for i, t in enumerate(paras):
        if t in ("관련기사", "다른기사 보기"):
            paras = paras[:i]
            break
    if paras and re.fullmatch(r"[가-힣]{2,4}\s?(기자|특파원|객원기자)", paras[-1]):
        paras = paras[:-1]
    caps = [t for t in p.captions]
    # 본문 안 △ 문단은 사진 설명으로 옮긴다
    for t in list(paras):
        if t.startswith("△") and t not in caps:
            caps.append(t)
            paras.remove(t)
    section = m.get("article:section", "") or (p.crumbs[0] if p.crumbs else "")
    sub = "\n".join(p.subs)
    joined = "\n".join([title, sub, "\n".join(paras), "\n".join(caps)])
    return {
        "idxno": idxno,
        "url": f"{BASE}/news/articleView.html?idxno={idxno}",
        "title": title,
        "subtitle": sub,
        "section": section,
        "crumbs": p.crumbs[:6],
        "publishedAt": parse_iso(pub).isoformat() if pub else None,
        "modifiedAt": parse_iso(mod).isoformat() if mod else None,
        "paragraphs": paras,
        "captions": caps,
        "bodyHash": hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12],
        "ok": bool(paras),
        "diag": {"paragraphs": len(paras), "captions": len(caps), "subtitle": bool(sub),
                 "bodyFound": bool(p.paras), "chars": sum(len(x) for x in paras)},
    }


ITEM = re.compile(r"articleView\.html\?idxno=(\d+)")
TIME = re.compile(r"(?:(\d{4})[-.])?(\d{2})[-.](\d{2})\s+(\d{2}):(\d{2})")


def list_candidates(start, end):
    """날짜 지정 목록에서 기사 번호와 목록에 보이는 시각을 모은다."""
    found = {}
    d0, d1 = start.date(), (end - timedelta(seconds=1)).date()
    url0 = f"{BASE}/news/articleList.html?sc_sdate={d0}&sc_edate={d1}&view_type=sm"
    seen_pages = set()
    for page_no in range(1, MAX_LIST_PAGES + 1):
        url = url0 + (f"&page={page_no}" if page_no > 1 else "")
        page = get(url)
        ids = ITEM.findall(page)
        key = tuple(dict.fromkeys(ids))
        if not key or key in seen_pages:
            break
        seen_pages.add(key)
        new = 0
        for m in ITEM.finditer(page):
            idx = int(m.group(1))
            if idx in found and found[idx] is not None:
                continue
            tail = page[m.end(): m.end() + 2500]
            t = TIME.search(tail)
            when = None
            if t:
                y = int(t.group(1)) if t.group(1) else start.year
                try:
                    when = datetime(y, int(t.group(2)), int(t.group(3)), int(t.group(4)), int(t.group(5)), tzinfo=KST)
                    if when - end > timedelta(days=200):   # 연도 넘어갈 때
                        when = when.replace(year=y - 1)
                except ValueError:
                    when = None
            if idx not in found:
                new += 1
            found[idx] = when
        total = re.search(r"총\s*:?\s*([\d,]+)\s*건", page)
        if total and len(found) >= int(total.group(1).replace(",", "")):
            break
        if not new:
            break
    return found


def auto_window(now):
    """예약 실행: 실행 시각으로 회차와 범위를 정한다. 주말이면 None."""
    if now.weekday() >= 5:
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 12:
        back = 3 if now.weekday() == 0 else 1
        slot, start, end = 1, day - timedelta(days=back) + timedelta(hours=17), day + timedelta(hours=10)
    elif now.hour < 15:
        slot, start, end = 2, day + timedelta(hours=10), day + timedelta(hours=13)
    else:
        slot, start, end = 3, day + timedelta(hours=13), day + timedelta(hours=17)
    return f"r_{day:%Y%m%d}_{slot}", start, end


def previous_articles(rid, limit=5):
    """앞선 두 회차 결과 파일에서 다시 확인할 기사(번호와 bodyHash)를 고른다."""
    if not os.path.isdir("data/typo"):
        return {}
    names = sorted(n for n in os.listdir("data/typo") if re.fullmatch(r"r_\d{8}_\d\.json", n) and n < f"{rid}.json")[-2:]
    picked = {}
    for name in reversed(names):
        try:
            with open(os.path.join("data/typo", name), encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            continue
        for a in sorted(prev.get("articles", []), key=lambda x: x.get("publishedAt") or "", reverse=True):
            if a.get("ok") and a["idxno"] not in picked and len(picked) < limit:
                picked[a["idxno"]] = a.get("bodyHash")
    return picked


def main():
    rid = os.environ.get("TYPO_ID", "").strip()
    prev_hash = {}
    if not rid:
        w = auto_window(datetime.now(KST))
        if w is None:
            print("주말이라 실행하지 않습니다")
            return
        rid, start, end = w
        prev_hash = previous_articles(rid)
        extra = list(prev_hash)
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", rid):
            sys.exit("TYPO_ID 형식이 올바르지 않습니다")
        start = parse_iso(os.environ["TYPO_START"])
        end = parse_iso(os.environ["TYPO_END"])
        extra = [int(x) for x in re.findall(r"\d+", os.environ.get("TYPO_EXTRA", ""))][:5]
    out = {"id": rid, "windowStart": start.isoformat(), "windowEnd": end.isoformat(),
           "collectedAt": datetime.now(KST).isoformat(timespec="seconds"),
           "articles": [], "skipped": [], "errors": [], "blocked": False}
    try:
        cands = list_candidates(start, end)
        out["listed"] = len(cands)
        todo = []
        for idx, when in sorted(cands.items()):
            # 목록 시각이 범위 밖이 분명하면 열지 않는다(앞뒤 1분 여유)
            if when is not None and not (start - timedelta(minutes=1) <= when < end + timedelta(minutes=1)):
                continue
            todo.append(idx)
        for idx in extra:
            if idx not in todo:
                todo.append(idx)
        for idx in todo[:MAX_ARTICLES]:
            try:
                a = extract(idx, get(f"{BASE}/news/articleView.html?idxno={idx}"))
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                out["errors"].append({"idxno": idx, "error": str(e)[:200]})
                continue
            pub = parse_iso(a["publishedAt"]) if a["publishedAt"] else None
            in_window = pub is not None and start <= pub < end
            if pub and pub > datetime.now(KST):
                out["skipped"].append({"idxno": idx, "reason": "예약(미래 시각)"})
                continue
            if not in_window and idx not in extra:
                out["skipped"].append({"idxno": idx, "reason": "범위 밖", "publishedAt": a["publishedAt"]})
                continue
            a["inWindow"] = in_window
            if not in_window and idx in prev_hash:
                if prev_hash[idx] == a["bodyHash"]:
                    out["skipped"].append({"idxno": idx, "reason": "앞 회차와 같음(수정 없음)"})
                    continue
                a["recheck"] = True
            out["articles"].append(a)
    except Blocked as e:
        out["blocked"] = True
        out["errors"].append({"error": f"사이트 접속 제한으로 중단: {e}"})
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"error": str(e)[:300]})
    out["requests"] = requests_made[0]
    # 팩트 체크용: 같은 소식을 다룬 다른 매체 기사(네이버 뉴스 검색, 반론보도닷컴 사이트 접속 아님)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import related
        out["naverCalls"] = related.attach_related(out["articles"], out["errors"])
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"error": "다른 매체 검색 실패: " + str(e)[:200]})

    os.makedirs("data/typo", exist_ok=True)
    with open(f"data/typo/{rid}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    # 오래된 결과 정리
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for name in os.listdir("data/typo"):
        m = re.search(r"_(\d{8})_", name)
        if name.endswith(".json") and m and m.group(1) < cutoff:
            os.remove(os.path.join("data/typo", name))
    print(f"기사 {len(out['articles'])}건 · 목록 {out.get('listed', 0)}건 · 접속 {out['requests']}회 · 오류 {len(out['errors'])}건")


if __name__ == "__main__":
    main()
