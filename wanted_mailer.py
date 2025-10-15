# -*- coding: utf-8 -*-
"""
Wanted 신규 공고 메일러
- config.json의 필터(키워드/지역/직무/경력)로 공고 조회
- last_ids.txt에 없는 "신규"만 메일 발송
- 성공 시 last_ids.txt 업데이트

필요 패키지:
  pip install requests
"""

import os
import json
import smtplib
import ssl
import requests
from typing import Any, Dict, List, Set
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime


CONFIG_PATH = os.getenv("WM_CONFIG_PATH", "config.json")
STATE_PATH  = os.getenv("WM_STATE_PATH",  "last_ids.txt")


# ------------- IO -------------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_sent_ids(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip() and not line.startswith("#")}

def save_sent_ids(path: str, ids: Set[str], keep_last: int = 5000) -> None:
    arr = list(ids)[-keep_last:]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(arr))


# ------------- API -------------
def pick_source(cfg: Dict[str, Any]):
    """openapi_v2(공식) or site_v4(기본, 비공식)"""
    src = (cfg.get("source") or "site_v4").lower()
    if src == "openapi_v2":
        url = cfg.get("openapi", {}).get("url") or "https://openapi.wanted.jobs/v2/jobs"
        headers = {
            "Accept": "application/json",
            "Client-Id":  os.getenv("WANTED_CLIENT_ID", cfg.get("openapi", {}).get("client_id", "")),
            "Client-Secret": os.getenv("WANTED_CLIENT_SECRET", cfg.get("openapi", {}).get("client_secret", "")),
        }
        return src, url, headers
    else:
        url = cfg.get("site_v4", {}).get("url") or "https://www.wanted.co.kr/api/v4/jobs"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        return "site_v4", url, headers

def build_params(cfg: Dict[str, Any], page: int) -> List[tuple]:
    filters = cfg.get("filters", {})
    limit = int(cfg.get("paging", {}).get("limit", 50))
    offset = page * limit

    params: List[tuple] = []

    # 키워드는 여러 키로 동시 전송(호환성)
    q = (filters.get("query") or "").strip()
    if q:
        for k in ("query", "q", "keyword", "keywords"):
            params.append((k, q))

    # 배열 파라미터
    for key in ("locations", "tag_type_ids", "years"):
        vals = filters.get(key, [])
        if isinstance(vals, str):
            vals = [v.strip() for v in vals.split(",") if v.strip()]
        for v in vals:
            params.append((key, str(v)))

    # 사이트 v4는 country=kr 기본값 추천
    if (cfg.get("source") or "site_v4").lower() == "site_v4":
        params.append(("country", filters.get("country", "kr")))

    # 정렬
    if filters.get("job_sort"):
        params.append(("job_sort", str(filters["job_sort"])))

    # 페이징
    params.append(("limit", str(limit)))
    params.append(("offset", str(offset)))

    return params

def fetch_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    src, url, headers = pick_source(cfg)
    max_pages = int(cfg.get("paging", {}).get("max_pages", 2))
    limit = int(cfg.get("paging", {}).get("limit", 50))

    collected: List[Dict[str, Any]] = []
    for page in range(max_pages):
        params = build_params(cfg, page)
        resp = requests.get(url, params=params, headers=headers, timeout=25)
        resp.raise_for_status()
        data = resp.json()

        # 다양한 스키마 대응
        items = (
            data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else
            data.get("results") if isinstance(data, dict) and isinstance(data.get("results"), list) else
            data.get("jobs") if isinstance(data, dict) and isinstance(data.get("jobs"), list) else
            data if isinstance(data, list) else
            []
        )
        if not items:
            break

        collected.extend(items)
        if len(items) < limit:
            break

    return collected


# ------------- 정규화/필터 -------------
def normalize(raw: Dict[str, Any]) -> Dict[str, str]:
    job_id = str(raw.get("id") or raw.get("position_id") or raw.get("job_id") or "")
    title  = str(raw.get("title") or raw.get("position") or raw.get("name") or "제목 없음")
    company = ""
    if isinstance(raw.get("company"), dict):
        company = str(raw["company"].get("name_ko") or raw["company"].get("name") or raw["company"].get("title") or "")
    company = company or str(raw.get("company_name") or "")
    location = str(raw.get("location") or raw.get("city") or raw.get("workplace") or "")
    link = f"https://www.wanted.co.kr/wd/{job_id}" if job_id else ""
    published = str(raw.get("published_at") or raw.get("created_at") or raw.get("posting_created_at") or "")
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "link": link,
        "published_at": published,
    }

def filter_new(items: List[Dict[str, Any]], sent_ids: Set[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for raw in items:
        it = normalize(raw)
        if not it["id"]:
            continue
        if it["id"] in sent_ids:
            continue
        out.append(it)
    return out


# ------------- 메일 -------------
def escape_html(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))

def build_html(items: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    q = (cfg.get("filters", {}).get("query") or "").strip()
    head = f"<h2>원티드 신규 공고 알림</h2><p>{now} 기준"
    if q:
        head += f' | 키워드: <b>{escape_html(q)}</b>'
    head += f" | 건수: <b>{len(items)}</b></p>"

    lis = []
    for it in items:
        title = escape_html(it["title"])
        comp  = escape_html(it["company"])
        loc   = escape_html(it["location"])
        pub   = escape_html(it["published_at"])
        link  = it["link"] or "#"
        meta = " · ".join([x for x in [comp, loc, pub] if x])
        lis.append(f'<li style="margin:8px 0;"><a href="{link}" target="_blank">{title}</a><br><span style="color:#666;">{meta}</span></li>')
    return f"""{head}
<ol>
{''.join(lis)}
</ol>
<p style="color:#888;font-size:12px;">※ GitHub Actions/크론으로 자동 발송됨</p>"""

def send_mail(html_body: str, cfg: Dict[str, Any], count: int) -> None:
    mail = cfg.get("email", {})
    auth = mail.get("auth", {})
    send = mail.get("send", {})

    host = mail.get("smtp_host")
    port = int(mail.get("smtp_port", 465))
    use_tls = bool(auth.get("use_tls", False))

    user_name = auth.get("user_name", "")
    user_email = auth.get("user_email", "")
    # 보안상 환경변수 우선
    user_pass = os.getenv("SMTP_PASS", auth.get("user_password", ""))

    from_name = send.get("from_name", user_name or user_email)
    from_email = send.get("from_email", user_email)
    to_name = send.get("to_name", "")
    to_email = send.get("to_email")
    subject_prefix = send.get("subject_prefix", "[Wanted]")
    subject = f"{subject_prefix} 신규 공고 {count}건"

    if not (host and port and from_email and to_email):
        raise RuntimeError("메일 설정(email.smtp_host/port, send.from_email, send.to_email)을 확인하세요.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    if use_tls:
        server = smtplib.SMTP(host, port)
        server.starttls(context=context)
    else:
        server = smtplib.SMTP_SSL(host, port, context=context)

    with server:
        if user_email and user_pass:
            server.login(user_email, user_pass)
        server.sendmail(from_email, [to_email], msg.as_string())


# ------------- 메인 -------------
def main():
    cfg = load_config(CONFIG_PATH)
    sent = load_sent_ids(STATE_PATH)

    items_raw = fetch_jobs(cfg)
    only_new = bool(cfg.get("only_new", True))
    items = filter_new(items_raw, sent) if only_new else [normalize(x) for x in items_raw]

    if not items:
        print("신규 공고 없음. 메일 미발송.")
        return

    html = build_html(items, cfg)
    send_mail(html, cfg, len(items))

    # 성공 후 상태 갱신
    for it in items:
        sent.add(it["id"])
    save_sent_ids(STATE_PATH, sent, keep_last=int(cfg.get("state_keep_last", 5000)))
    print(f"메일 발송 완료: {len(items)}건")


if __name__ == "__main__":
    main()
