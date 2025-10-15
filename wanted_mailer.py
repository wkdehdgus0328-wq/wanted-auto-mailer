def build_params(cfg: Dict[str, Any], page: int) -> List[tuple]:
    """site_v4 안전 파라미터만 구성"""
    filters = cfg.get("filters", {})
    limit = int(cfg.get("paging", {}).get("limit", 50))
    offset = page * limit

    params: List[tuple] = []

    # 키워드 (하나만)
    q = (filters.get("query") or "").strip()
    if q:
        params.append(("query", q))

    # locations: 숫자만 허용
    locs = filters.get("locations", [])
    if isinstance(locs, str):
        locs = [v.strip() for v in locs.split(",") if v.strip()]
    for v in locs:
        try:
            params.append(("locations", str(int(v))))
        except ValueError:
            pass  # 잘못된 값은 버림

    # country
    params.append(("country", (filters.get("country") or "kr").lower()))

    # 정렬 (job.latest_order 형식만 허용)
    js = (filters.get("job_sort") or "").strip()
    if js.startswith("job."):
        params.append(("job_sort", js))

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
        r = requests.get(url, params=params, headers=headers, timeout=25)
        if r.status_code == 422:
            # ⚠️ 파라미터가 과하거나 형식 오류 → 최소셋으로 다시 한 번 시도
            print("⚠️ 422 from API → retry with minimal params")
            minimal = [("country", "kr"), ("limit", str(limit)), ("offset", str(page * limit))]
            q = next((v for (k, v) in params if k == "query"), "")
            if q:
                minimal.append(("query", q))
            r = requests.get(url, params=minimal, headers=headers, timeout=25)

        r.raise_for_status()
        data = r.json()
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
