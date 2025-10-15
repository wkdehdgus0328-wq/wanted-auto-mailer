# -*- coding: utf-8 -*-
import os, json, smtplib, ssl, requests
from typing import Any, Dict, List, Set
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import urlencode

CONFIG_PATH = os.getenv("WM_CONFIG_PATH", "config.json")
STATE_PATH  = os.getenv("WM_STATE_PATH",  "last_ids.txt")

def load_config(p:str)->Dict[str,Any]:
    with open(p,"r",encoding="utf-8") as f: return json.load(f)

def load_sent_ids(p:str)->Set[str]:
    if not os.path.exists(p): return set()
    with open(p,"r",encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip() and not l.startswith("#")}

def save_sent_ids(p:str, ids:Set[str], keep:int=5000):
    arr=list(ids)[-keep:]
    with open(p,"w",encoding="utf-8") as f: f.write("\n".join(arr))

def pick_source(cfg:Dict[str,Any]):
    url = cfg.get("site_v4",{}).get("url") or "https://www.wanted.co.kr/api/v4/jobs"
    headers = {"Accept":"application/json","User-Agent":"Mozilla/5.0"}
    return "site_v4", url, headers

# v4에서 안전한 쿼리 파라미터만 사용
def build_params(cfg: Dict[str, Any], page: int) -> List[tuple]:
    fs = cfg.get("filters", {})
    limit  = int(cfg.get("paging", {}).get("limit", 30))
    offset = page * limit
    params: List[tuple] = []

    q = (fs.get("query") or "").strip()
    if q: params.append(("query", q))

    locs = fs.get("locations", [])
    if isinstance(locs, str):
        locs = [v.strip() for v in locs.split(",") if v.strip()]
    for v in locs:
        try: params.append(("locations", str(int(v))))
        except ValueError: pass

    params.append(("country", (fs.get("country") or "kr").lower()))

    js = (fs.get("job_sort") or "").strip()
    if js.startswith("job."): params.append(("job_sort", js))

    params += [("limit", str(limit)), ("offset", str(offset))]
    return params

def fetch_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    _, url, headers = pick_source(cfg)
    max_pages = int(cfg.get("paging", {}).get("max_pages", 1))
    limit     = int(cfg.get("paging", {}).get("limit", 30))
    results: List[Dict[str, Any]] = []

    for page in range(max_pages):
        params = build_params(cfg, page)
        print("GET URL:", url + "?" + urlencode(params, doseq=True))

        resp = requests.get(url, params=params, headers=headers, timeout=25)
        if resp.status_code == 422:
            print("⚠️ 422 → retry with minimal params")
            minimal = [("country","kr"),("limit",str(limit)),("offset",str(page*limit))]
            q = next((v for (k,v) in params if k=="query"), "")
            if q: minimal.append(("query", q))
            print("GET URL (minimal):", url + "?" + urlencode(minimal, doseq=True))
            resp = requests.get(url, params=minimal, headers=headers, timeout=25)

        resp.raise_for_status()
        data = resp.json()
        items = (
            data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else
            data.get("results") if isinstance(data, dict) and isinstance(data.get("results"), list) else
            data.get("jobs") if isinstance(data, dict) and isinstance(data.get("jobs"), list) else
            data if isinstance(data, list) else []
        )
        if not items: break
        results.extend(items)
        if len(items) < limit: break
    return results

def norm(raw:Dict[str,Any])->Dict[str,str]:
    jid=str(raw.get("id") or raw.get("position_id") or raw.get("job_id") or "")
    title=str(raw.get("title") or raw.get("position") or raw.get("name") or "제목 없음")
    comp=""
    if isinstance(raw.get("company"),dict):
        comp=str(raw["company"].get("name_ko") or raw["company"].get("name") or raw["company"].get("title") or "")
    comp=comp or str(raw.get("company_name") or "")
    loc=str(raw.get("location") or raw.get("city") or raw.get("workplace") or "")
    link=f"https://www.wanted.co.kr/wd/{jid}" if jid else ""
    pub=str(raw.get("published_at") or raw.get("created_at") or raw.get("posting_created_at") or "")
    return {"id":jid,"title":title,"company":comp,"location":loc,"link":link,"published_at":pub}

def filter_new(items:List[Dict[str,Any]], sent:Set[str])->List[Dict[str,str]]:
    out=[]
    for r in items:
        it=norm(r)
        if it["id"] and it["id"] not in sent: out.append(it)
    return out

def esc(s:str)->str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
              .replace('"',"&quot;").replace("'","&#39;"))

def build_html(items:List[Dict[str,str]], cfg:Dict[str,Any])->str:
    now=datetime.now().strftime("%Y-%m-%d %H:%M")
    q=(cfg.get("filters",{}).get("query") or "").strip()
    head=f"<h2>원티드 신규 공고 알림</h2><p>{now} 기준"
    if q: head+=f' | 키워드: <b>{esc(q)}</b>'
    head+=f" | 건수: <b>{len(items)}</b></p>"
    lis=[]
    for it in items:
        meta=" · ".join([x for x in [esc(it['company']), esc(it['location']), esc(it['published_at'])] if x])
        lis.append(f'<li style="margin:8px 0;"><a href="{it["link"] or "#"}" target="_blank">{esc(it["title"])}</a><br><span style="color:#666;">{meta}</span></li>')
    return head + "<ol>" + "".join(lis) + "</ol><p style='color:#888;font-size:12px;'>※ GitHub Actions로 자동 발송</p>"

def send_mail(html:str, cfg:Dict[str,Any], n:int):
    mail=cfg.get("email",{}); auth=mail.get("auth",{}); send=mail.get("send",{})
    host=mail.get("smtp_host"); port=int(mail.get("smtp_port",465)); use_tls=bool(auth.get("use_tls",False))
    user_email=auth.get("user_email","")
    # 우선 시크릿, 없으면 config의 password 사용
    user_pass=os.getenv("SMTP_PASS", auth.get("password",""))
    from_name=send.get("from_name", auth.get("user_name","")); from_email=send.get("from_email", user_email)
    to_name=send.get("to_name",""); to_email=send.get("to_email")
    subject=f"{send.get('subject_prefix','[Wanted]')} 신규 공고 {n}건"

    if not (host and port and from_email and to_email):
        raise RuntimeError("메일 설정(email.smtp_host/port, send.from_email, send.to_email) 확인 필요.")
    if not (user_email and user_pass):
        raise RuntimeError("SMTP 비밀번호가 없습니다. Secrets(SMTP_PASS) 또는 config.email.auth.password 설정하세요.")

    print(f"SMTP host={host}:{port} tls={use_tls} from={from_email} to={to_email}")

    msg=MIMEMultipart("alternative")
    msg["Subject"]=subject
    msg["From"]=f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"]=f"{to_name} <{to_email}>" if to_name else to_email
    msg.attach(MIMEText(html,"html","utf-8"))

    ctx=ssl.create_default_context()
    if use_tls:
        srv=smtplib.SMTP(host,port); srv.starttls(context=ctx)
    else:
        srv=smtplib.SMTP_SSL(host,port,context=ctx)
    with srv:
        srv.login(user_email,user_pass)
        srv.sendmail(from_email,[to_email],msg.as_string())

def main():
    cfg=load_config(CONFIG_PATH)
    sent=load_sent_ids(STATE_PATH)
    force_test = os.getenv("FORCE_TEST", "0") == "1"

    try:
        items_raw = fetch_jobs(cfg)
    except Exception as e:
        print("채용공고 조회 실패:", repr(e))
        items_raw = []

    only_new=bool(cfg.get("only_new",True))
    items=filter_new(items_raw,sent) if only_new else [norm(x) for x in items_raw]

    if force_test and not items:
        html = f"""
        <h2>✅ Wanted Mailer 테스트</h2>
        <p>신규 공고는 없지만 테스트 발송입니다.</p>
        <p>실행 시각: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
        """
        send_mail(html, cfg, 0)
        print("신규 없음 → 테스트 메일 발송 완료.")
        return

    if not items:
        print("신규 공고 없음. 메일 미발송.")
        return

    html=build_html(items,cfg)
    send_mail(html,cfg,len(items))
    for it in items: sent.add(it["id"])
    save_sent_ids(STATE_PATH, sent, keep=int(cfg.get("state_keep_last",5000)))
    print(f"메일 발송 완료: {len(items)}건")

if __name__=="__main__":
    main()
