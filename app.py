import os
import io
import re
import html
import json
import shutil
import zipfile
import tempfile
import datetime as dt
import pandas as pd
import anthropic
import streamlit as st
from openpyxl import load_workbook
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "단순경비율.xlsx")
ACCOUNT_PATH = os.path.join(BASE_DIR, "계정과목.xlsx")

PURCHASE_CATEGORIES = {"도매 및 상품중개업", "소매업", "음식점"}

EXCLUDED_ACCOUNTS = {
    "인적용역": {"감가상각비"},
}

# 종합소득세율 구간 (과세표준, 세율, 누진공제)
TAX_BRACKETS = [
    (14_000_000,   0.06,         0),
    (50_000_000,   0.15, 1_260_000),
    (88_000_000,   0.24, 5_760_000),
    (150_000_000,  0.35, 15_440_000),
    (300_000_000,  0.38, 19_940_000),
    (500_000_000,  0.40, 25_940_000),
    (1_000_000_000,0.42, 35_940_000),
    (float("inf"), 0.45, 65_940_000),
]


@st.cache_data
def load_data():
    df = pd.read_excel(EXCEL_PATH, header=None)
    df.columns = ["업종코드", "중분류", "소분류", "세분류", "세세분류",
                  "단순일반율", "단순자가율", "기준일반율", "기준자가율", "기준및적용범위"]
    df["업종코드"] = df["업종코드"].astype(str).str.strip()
    return df


@st.cache_data
def load_accounts():
    df = pd.read_excel(ACCOUNT_PATH, header=None)
    accounts = {"매출원가": [], "판관비": []}
    current = None
    for _, row in df.iterrows():
        if pd.notna(row[0]):
            current = str(row[0]).strip()
        if current and pd.notna(row[1]):
            accounts[current].append(str(row[1]).strip())
    return accounts


def find_business(df, code):
    row = df[df["업종코드"] == code]
    if row.empty:
        return None
    return row.iloc[0]


def has_product_purchase(중분류):
    return any(cat in str(중분류) for cat in PURCHASE_CATEGORIES)


def get_excluded_accounts(중분류):
    excluded = set()
    for key, accs in EXCLUDED_ACCOUNTS.items():
        if key in str(중분류):
            excluded |= accs
    return excluded


def get_expense_distribution(business_info, accounts, use_purchase, 적용경비율=None):
    api_key = st.secrets.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    excluded = get_excluded_accounts(business_info["중분류"])
    account_list = [a for a in accounts["판관비"] if a not in excluded]

    if use_purchase:
        all_accounts = ["당기상품매입액"] + account_list
    else:
        all_accounts = account_list

    expense_rate = 적용경비율 if 적용경비율 is not None else float(business_info["단순일반율"])

    prompt = f"""당신은 세무 전문가입니다. 아래 업종의 경비율을 계정과목별로 배분해주세요.

업종 정보:
- 업종코드: {business_info['업종코드']}
- 중분류: {business_info['중분류']}
- 소분류: {business_info['소분류']}
- 세분류: {business_info['세분류']}
- 세세분류: {business_info['세세분류']}
- 단순경비율(일반): {expense_rate}%
- 업종 설명: {business_info['기준및적용범위']}

배분할 계정과목:
{json.dumps(all_accounts, ensure_ascii=False)}

조건:
1. 위 계정과목들의 비율 합계가 정확히 {expense_rate}%가 되어야 합니다
2. 해당 업종의 특성에 맞게 현실적으로 배분해주세요
3. 해당 업종에서 발생하지 않는 비용은 0%로 설정하세요
4. 소수점 첫째 자리까지만 표시하세요

반드시 아래 JSON 형식으로만 응답하세요 (설명 없이):
{{
  "계정과목명": 비율숫자,
  ...
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]

    return json.loads(response_text.strip())


def calc_income_tax(과세표준):
    """과세표준으로 산출세액 계산"""
    for limit, rate, deduction in TAX_BRACKETS:
        if 과세표준 <= limit:
            return round(과세표준 * rate - deduction)
    return 0


# ── 페이지 설정 ──────────────────────────────────────
st.set_page_config(page_title="단순경비율 계산기", page_icon="📊", layout="centered")

# ── 비밀번호 인증 ──────────────────────────────────────
def check_password():
    correct_pw = st.secrets.get("APP_PASSWORD") or os.getenv("APP_PASSWORD", "tax1234")

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    st.markdown("---")
    st.markdown("<h2 style='text-align:center'>🔐 경비율 계산기</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center; color:gray'>이용 문의: 이재원 세무사</p>", unsafe_allow_html=True)
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        pw = st.text_input("비밀번호를 입력하세요", type="password", placeholder="비밀번호")
        if st.button("입력", use_container_width=True, type="primary"):
            if pw == correct_pw:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")
    return False

if not check_password():
    st.stop()

# ══════════════════════════════════════════════════════
# 카드사 변환 관련 함수
# ══════════════════════════════════════════════════════
TEMPLATE_PATH = os.path.join(BASE_DIR, "신용카드매입자료_업로드_기본서식.xlsx")

ACCOUNT_CODE_MAP = {
    "복리후생비": "811", "여비교통비": "812", "접대비": "813",
    "통신비": "814", "수도광열비": "815", "전력비": "816",
    "세금과공과금": "817", "임차료": "819", "수선비": "820",
    "보험료": "821", "차량유지비": "822", "운반비": "824",
    "교육훈련비": "825", "도서인쇄비": "826", "회의비": "827",
    "포장비": "828", "사무용품비": "829", "소모품비": "830",
    "수수료비용": "831", "보관료": "832", "광고선전비": "833",
    "건물관리비": "837", "미분류": "",
}

DEFAULT_RULES = {
    "keyword": [
        {"match": ["주유소","SK에너지","GS칼텍스","현대오일뱅크","S-OIL"], "account": "차량유지비", "vat": "공제"},
        {"match": ["정비","카센터","타이어","세차"], "account": "차량유지비", "vat": "공제"},
        {"match": ["통행료","하이패스","주차","한국도로공사"], "account": "차량유지비", "vat": "공제"},
        {"match": ["충전","일렉링크","전기차충전"], "account": "차량유지비", "vat": "공제"},
        {"match": ["GS25","CU","세븐일레븐","이마트24","미니스톱","씨유"], "account": "복리후생비", "vat": "공제"},
        {"match": ["이마트","홈플러스","롯데마트"], "account": "복리후생비", "vat": "공제"},
        {"match": ["맥도날드","버거킹","롯데리아","KFC","한솥도시락"], "account": "복리후생비", "vat": "공제"},
        {"match": ["스타벅스","커피","카페","빽다방","투썸"], "account": "접대비", "vat": "불공제"},
        {"match": ["SK텔레콤","KT","LG유플러스","SKT","LGU+"], "account": "통신비", "vat": "공제"},
        {"match": ["호텔","모텔","펜션","아난티"], "account": "여비교통비", "vat": "공제"},
        {"match": ["택시","카카오T"], "account": "여비교통비", "vat": "공제"},
        {"match": ["병원","의원","한의원","치과"], "account": "복리후생비", "vat": "불공제"},
        {"match": ["약국"], "account": "복리후생비", "vat": "불공제"},
        {"match": ["우체국"], "account": "통신비", "vat": "불공제"},
    ],
    "industry": [
        {"match": ["주유소"], "account": "차량유지비", "vat": "공제"},
        {"match": ["편의점"], "account": "복리후생비", "vat": "공제"},
        {"match": ["음식점","한식","중식","일식","양식","부페"], "account": "접대비", "vat": "불공제"},
        {"match": ["커피전문점"], "account": "접대비", "vat": "불공제"},
        {"match": ["숙박업","호텔"], "account": "여비교통비", "vat": "공제"},
        {"match": ["통신사"], "account": "통신비", "vat": "공제"},
        {"match": ["통행료"], "account": "차량유지비", "vat": "공제"},
    ]
}


def parse_filename_card(filename):
    # 형식: 상호명_사업자번호_카드사_카드번호_직원유무_차량유무.xlsx
    base = os.path.splitext(filename)[0]
    parts = base.split("_")
    result = {"업체명": "", "사업자번호": "", "신용카드사명": "", "신용카드번호": "", "직원유무": "", "차량유무": ""}
    if len(parts) >= 1: result["업체명"] = parts[0]
    if len(parts) >= 2: result["사업자번호"] = parts[1]
    if len(parts) >= 3: result["신용카드사명"] = parts[2]
    if len(parts) >= 4: result["신용카드번호"] = parts[3]
    if len(parts) >= 5: result["직원유무"] = parts[4]
    if len(parts) >= 6: result["차량유무"] = parts[5]
    return result


def classify_transaction(vendor, industry, rules):
    vendor = str(vendor) if vendor else ""
    industry = str(industry) if industry else ""
    for rule in rules["keyword"]:
        for kw in rule["match"]:
            if kw in vendor:
                return rule["account"], rule["vat"], "키워드"
    if industry and industry not in ("nan", ""):
        for rule in rules["industry"]:
            for kw in rule["match"]:
                if kw in industry:
                    return rule["account"], rule["vat"], "업종"
    return "미분류", "공제", "미분류"


def process_card_data(vendor, date, total, bizno, upjong, card_company, card_number):
    supply = (total / 1.1).astype(int)
    vat = total - supply
    accounts, vat_deds, methods = [], [], []
    for v, u in zip(vendor, upjong):
        acc, vd, m = classify_transaction(v, u, DEFAULT_RULES)
        accounts.append(acc); vat_deds.append(vd); methods.append(m)
    vat_types = [57 if v == "공제" else "" for v in vat_deds]
    account_codes = [ACCOUNT_CODE_MAP.get(a, "") for a in accounts]
    result = pd.DataFrame({
        "카드종류": 1, "신용카드사명": card_company, "신용카드번호": card_number,
        "승인일자": date.dt.strftime("%Y%m%d"), "사업자등록번호": bizno,
        "거래처명": vendor.str.slice(0, 15), "거래처유형": "",
        "공급가액": supply, "세액": vat, "봉사료": 0, "합계금액": total,
        "부가세공제여부": vat_deds, "부가세유형": vat_types,
        "계정과목": account_codes, "품목(적요)": upjong.str.slice(0, 30) if hasattr(upjong, "str") else "",
    })
    stats = pd.DataFrame({
        "가맹점명": vendor, "업종": upjong, "계정과목": accounts,
        "부가세": vat_deds, "분류방법": methods, "금액": total
    })
    return result, stats


def parse_samsung_card(file_bytes, card_company, card_number):
    df_raw = pd.read_excel(io.BytesIO(file_bytes), header=None)

    # 헤더 행 탐색
    header_row = None
    for i in range(min(100, len(df_raw))):
        row_join = " ".join("" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x) for x in df_raw.iloc[i].tolist())
        if ("이용일" in row_join or "승인일자" in row_join) and ("가맹점명" in row_join or "가맹점" in row_join) and "금액" in row_join:
            header_row = i; break
    if header_row is None: header_row = 19

    df = pd.read_excel(io.BytesIO(file_bytes), header=header_row)
    df.columns = [str(c).strip() for c in df.columns]

    def pick(cands):
        for c in cands:
            if c in df.columns: return c
        return None

    col_v = pick(["가맹점명", "가맹점", "상호"])
    col_d = pick(["이용일", "승인일자", "거래일"])
    col_t = pick(["이용금액(원)", "이용금액", "금액"])
    col_b = pick(["사업자번호", "사업자등록번호"])
    col_u = pick(["업종", "업태"])

    # 컬럼명 감지 실패 시 위치(index) 기반으로 폴백
    if col_v is None and len(df.columns) >= 7:
        df.columns = list(df.columns)
        # 삼성카드 표준 12컬럼: 상품 매출 성명 카드구분 카드번호 이용일 가맹점명 이용금액(원) 개월수 승인번호 사업자번호 업종
        col_map = {0:"상품", 1:"매출", 2:"성명", 3:"카드구분", 4:"카드번호",
                   5:"이용일", 6:"가맹점명", 7:"이용금액(원)", 8:"개월수",
                   9:"승인번호", 10:"사업자번호", 11:"업종"}
        df.columns = [col_map.get(i, f"col{i}") for i in range(len(df.columns))]
        col_v = "가맹점명"; col_d = "이용일"; col_t = "이용금액(원)"
        col_b = "사업자번호"; col_u = "업종"

    if col_v is None or col_d is None or col_t is None:
        raise ValueError(f"삼성카드 컬럼 감지 실패. 발견된 컬럼: {list(df.columns)}")

    vendor = df[col_v].astype(str).str.replace(r"_x000D_", "", regex=True).str.strip()
    date = pd.to_datetime(df[col_d], errors="coerce")
    total = pd.to_numeric(df[col_t], errors="coerce").fillna(0).astype(int)
    bizno = df[col_b].apply(lambda x: re.sub(r"\D", "", str(x))[:10]) if col_b else pd.Series([""] * len(df))
    upjong = df[col_u].astype(str) if col_u else pd.Series([""] * len(df))

    mask = date.notna() & (total != 0)
    return process_card_data(vendor[mask].reset_index(drop=True), date[mask].reset_index(drop=True),
                             total[mask].reset_index(drop=True), bizno[mask].reset_index(drop=True),
                             upjong[mask].reset_index(drop=True), card_company, card_number)


def parse_hana_card(file_bytes, card_company, card_number):
    df = pd.read_excel(io.BytesIO(file_bytes), header=12)
    df = df[df['취소일자'].isna() | (df['취소일자'] == '취소일자')]
    vendor = df['가맹점명'].astype(str).str.strip()
    date = pd.to_datetime(df['매출일자'], errors="coerce")
    total = pd.to_numeric(df['원화\n사용금액'], errors="coerce").fillna(0).astype(int)
    bizno = df['가맹점\n사업자번호'].astype(str).str.replace("-", "").str[:10]
    upjong = df['업종명'].astype(str) if '업종명' in df.columns else pd.Series([""] * len(df))
    mask = date.notna() & (total != 0)
    return process_card_data(vendor[mask], date[mask], total[mask], bizno[mask], upjong[mask], card_company, card_number)


def parse_shinhan_card(file_bytes, card_company, card_number):
    """신한카드 파싱 - header=4, 컬럼 위치 기반"""
    df = pd.read_excel(io.BytesIO(file_bytes), header=4)
    if len(df.columns) >= 11:
        vendor = df.iloc[:, 5].astype(str).str.strip()
        date = pd.to_datetime(df.iloc[:, 0], errors="coerce")
        total = pd.to_numeric(df.iloc[:, 6], errors="coerce").fillna(0).astype(int)
        bizno = df.iloc[:, 10].astype(str).str.replace("-", "").str[:10]
        upjong = df.iloc[:, 4].astype(str)
    else:
        vendor = df['가맹점명'].astype(str).str.strip() if '가맹점명' in df.columns else df.iloc[:, 5].astype(str).str.strip()
        date = pd.to_datetime(df['거래일자'] if '거래일자' in df.columns else df.iloc[:, 0], errors="coerce")
        total = pd.to_numeric(df['이용금액'] if '이용금액' in df.columns else df.iloc[:, 6], errors="coerce").fillna(0).astype(int)
        bizno = (df['사업자등록번호'] if '사업자등록번호' in df.columns else df.iloc[:, 10]).astype(str).str.replace("-", "").str[:10]
        upjong = (df['상품유형'] if '상품유형' in df.columns else df.iloc[:, 4]).astype(str)
    mask = date.notna() & (total != 0)
    return process_card_data(vendor[mask].reset_index(drop=True), date[mask].reset_index(drop=True),
                             total[mask].reset_index(drop=True), bizno[mask].reset_index(drop=True),
                             upjong[mask].reset_index(drop=True), card_company, card_number)


def parse_bc_card(file_bytes, card_company, card_number):
    """비씨카드 파싱 - 날짜 YYYY/MM/DD 패턴으로 데이터행 식별"""
    from openpyxl import load_workbook as _load_wb
    wb = _load_wb(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    all_rows = list(ws.iter_rows(values_only=True))

    # 헤더행 탐색: 날짜 패턴(YYYY/MM/DD) 있는 행 직전
    header_idx = 9
    for i, row in enumerate(all_rows):
        non_none = [c for c in row if c is not None]
        if non_none and re.match(r'\d{4}/\d{2}/\d{2}', str(non_none[0])):
            for j in range(i - 1, -1, -1):
                if any(c is not None for c in all_rows[j]):
                    header_idx = j
                    break
            break

    vendors, dates, totals, biznos = [], [], [], []
    for row in all_rows[header_idx + 1:]:
        date_val = row[2] if len(row) > 2 else None
        if date_val is None: continue
        date_str = str(date_val).strip()
        if not re.match(r'\d{4}/\d{2}/\d{2}', date_str): continue
        vendor = str(row[9]).strip() if len(row) > 9 and row[9] is not None else ''
        amount_raw = row[19] if len(row) > 19 else None
        try:
            amount = int(str(amount_raw).replace(',', '').strip())
        except (ValueError, TypeError):
            amount = 0
        if amount <= 0: continue
        bizno_raw = str(row[12]).replace('-', '') if len(row) > 12 and row[12] is not None else ''
        biznos.append(re.sub(r'\D', '', bizno_raw)[:10])
        vendors.append(vendor); dates.append(date_str); totals.append(amount)

    if not vendors:
        return pd.DataFrame(), pd.DataFrame()

    vendor_s = pd.Series(vendors)
    date_s = pd.to_datetime(pd.Series(dates), format='%Y/%m/%d', errors='coerce')
    total_s = pd.Series(totals, dtype=int)
    bizno_s = pd.Series(biznos)
    upjong_s = pd.Series([""] * len(vendors))
    return process_card_data(vendor_s, date_s, total_s, bizno_s, upjong_s, card_company, card_number)


def fix_html_entities(xlsx_path):
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(xlsx_path, 'r') as z: z.extractall(tmp)
        for sub in ['xl/worksheets', 'xl']:
            d = os.path.join(tmp, sub)
            if not os.path.exists(d): continue
            for fn in os.listdir(d):
                if fn.endswith('.xml'):
                    fp = os.path.join(d, fn)
                    content = open(fp, encoding='utf-8').read()
                    open(fp, 'w', encoding='utf-8').write(html.unescape(content))
        with zipfile.ZipFile(xlsx_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(tmp):
                for f in files: z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), tmp))
    finally:
        shutil.rmtree(tmp)


def write_to_template(rows_df, company_name, business_number):
    tmp_path = tempfile.mktemp(suffix=".xlsx")
    shutil.copy2(TEMPLATE_PATH, tmp_path)
    wb = load_workbook(tmp_path)
    ws = wb.active
    ws['A4'] = company_name; ws['C4'] = business_number
    for r in range(10, 10 + len(rows_df) + 10):
        for c in range(1, 16): ws.cell(r, c).value = None
    for idx, row in rows_df.iterrows():
        r = 10 + list(rows_df.index).index(idx)
        ws.cell(r, 1).value = int(row['카드종류']); ws.cell(r, 2).value = row['신용카드사명']
        ws.cell(r, 3).value = row['신용카드번호']; ws.cell(r, 4).value = row['승인일자']
        ws.cell(r, 5).value = row['사업자등록번호']; ws.cell(r, 6).value = row['거래처명']
        ws.cell(r, 7).value = row['거래처유형'] or None
        ws.cell(r, 8).value = int(row['공급가액']); ws.cell(r, 9).value = int(row['세액'])
        ws.cell(r, 10).value = int(row['봉사료']); ws.cell(r, 11).value = int(row['합계금액'])
        ws.cell(r, 12).value = row['부가세공제여부']
        ws.cell(r, 13).value = int(row['부가세유형']) if row['부가세유형'] != "" else None
        ws.cell(r, 14).value = row['계정과목']
        ws.cell(r, 15).value = row['품목(적요)'] or None
    ws.cell(9, 8).value = int(rows_df['공급가액'].sum())
    ws.cell(9, 9).value = int(rows_df['세액'].sum())
    ws.cell(9, 10).value = int(rows_df['봉사료'].sum())
    ws.cell(9, 11).value = int(rows_df['합계금액'].sum())
    wb.save(tmp_path); fix_html_entities(tmp_path)
    with open(tmp_path, 'rb') as f: data = f.read()
    os.remove(tmp_path)
    return data


# ── 탭 구성 ──────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["📊 경비율 계산기", "🧾 종합소득세 계산기", "💳 카드사 변환기"])


# ════════════════════════════════════════════════════
# TAB 1 : 경비율 계산기
# ════════════════════════════════════════════════════
with tab1:
    st.title("📊 단순경비율 계정과목 배분 계산기")
    st.caption("업종코드와 매출액을 입력하면 Claude AI가 계정과목별 경비를 산정합니다.")

    with st.form("calc_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            업종코드 = st.text_input("업종코드", placeholder="예) 552101")
        with col2:
            매출액_input = st.text_input("매출액 (원)", placeholder="예) 150,000,000")
        with col3:
            경비율_input = st.text_input("경비율 (%)", placeholder="미입력 시 단순경비율 자동적용",
                                         help="직접 입력하면 해당 경비율로 계정과목 배분. 비워두면 단순경비율 자동 사용.")

        submitted = st.form_submit_button("계산하기", use_container_width=True, type="primary")

    if submitted:
        if not 업종코드 or not 매출액_input:
            st.warning("업종코드와 매출액을 모두 입력해주세요.")
        else:
            try:
                매출액 = int(매출액_input.replace(",", "").replace(" ", ""))
            except ValueError:
                st.error("매출액은 숫자로 입력해주세요.")
                st.stop()

            # 경비율 처리 (입력 없으면 None → 단순경비율 사용)
            커스텀경비율 = None
            if 경비율_input.strip():
                try:
                    커스텀경비율 = float(경비율_input.replace("%", "").strip())
                    if not (0 < 커스텀경비율 <= 100):
                        st.error("경비율은 0~100 사이 값을 입력해주세요.")
                        st.stop()
                except ValueError:
                    st.error("경비율은 숫자로 입력해주세요. 예) 64.1")
                    st.stop()

            df = load_data()
            accounts = load_accounts()
            business = find_business(df, 업종코드.strip())

            if business is None:
                st.error(f"업종코드 **{업종코드}** 를 찾을 수 없습니다.")
                st.stop()

            use_purchase = has_product_purchase(business["중분류"])
            적용경비율 = 커스텀경비율 if 커스텀경비율 else float(business["단순일반율"])

            st.divider()
            st.subheader("📌 업종 정보")
            c1, c2, c3 = st.columns(3)
            c1.metric("업종명", business["세세분류"])
            c2.metric("단순경비율(일반)", f"{business['단순일반율']}%")
            c3.metric("적용 경비율", f"{적용경비율}%",
                      delta="직접 입력" if 커스텀경비율 else "단순경비율 자동적용",
                      delta_color="normal" if 커스텀경비율 else "off")
            st.caption(f"중분류: {business['중분류']} / 소분류: {business['소분류']} / 세분류: {business['세분류']}")

            st.divider()
            with st.spinner("Claude AI가 계정과목별 비율을 산정하고 있습니다..."):
                try:
                    distribution = get_expense_distribution(business, accounts, use_purchase, 적용경비율)
                except Exception as e:
                    st.error(f"API 오류: {e}")
                    st.stop()

            st.subheader("📋 계정과목별 경비 내역")
            rows = []
            total_rate = 0
            total_amount = 0
            for account, rate in distribution.items():
                if rate > 0:
                    amount = 매출액 * rate / 100
                    rows.append({"계정과목": account, "비율": f"{rate:.1f}%", "금액 (원)": f"{amount:,.0f}"})
                    total_rate += rate
                    total_amount += amount

            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.divider()
            c1, c2, c3 = st.columns(3)
            c1.metric("매출액", f"{매출액:,.0f}원")
            c2.metric("총 경비", f"{total_amount:,.0f}원", f"{total_rate:.1f}%")
            소득금액 = 매출액 - total_amount
            c3.metric("소득금액 (매출 - 경비)", f"{소득금액:,.0f}원", f"{100 - total_rate:.1f}%")
            st.caption(f"※ 단순경비율 {business['단순일반율']}% 기준 / Claude AI 추정값으로 실제 신고와 다를 수 있습니다.")

            # 종합소득세 탭으로 넘길 소득금액 저장
            st.session_state["소득금액"] = 소득금액
            st.info("💡 종합소득세를 계산하려면 상단 **'🧾 종합소득세 계산기'** 탭을 클릭하세요.")


# ════════════════════════════════════════════════════
# TAB 2 : 종합소득세 계산기
# ════════════════════════════════════════════════════
with tab2:
    st.title("🧾 종합소득세 계산기")
    st.caption("소득금액과 공제항목을 입력하면 종합소득세를 자동 계산합니다.")

    # 소득금액 (경비율 탭에서 넘어온 값 or 직접 입력)
    default_소득 = st.session_state.get("소득금액", 0)

    st.subheader("① 소득금액")
    소득금액_input = st.number_input(
        "소득금액 (원)",
        min_value=0,
        value=int(default_소득),
        step=100000,
        format="%d",
        help="경비율 계산기에서 자동으로 불러오거나 직접 입력하세요."
    )

    st.divider()

    # ── 소득공제 ──
    st.subheader("② 소득공제")
    dc1, dc2, dc3 = st.columns(3)
    with dc1:
        국민연금 = st.number_input("국민연금보험료 (원)", min_value=0, value=0, step=10000, format="%d")
    with dc2:
        소상공인공제 = st.number_input("소상공인공제부금 (원)", min_value=0, value=0, step=10000, format="%d",
                                       help="노란우산공제 납입액")
    with dc3:
        인적공제 = st.number_input("인적공제 (원)", min_value=0, value=1500000, step=500000, format="%d",
                                    help="본인 150만원 기본, 부양가족 1인당 150만원 추가")

    총소득공제 = 국민연금 + 소상공인공제 + 인적공제

    st.divider()

    # ── 세액공제 ──
    st.subheader("③ 세액공제")
    tc1, tc2, tc3 = st.columns(3)
    with tc1:
        퇴직연금공제 = st.number_input("퇴직연금세액공제 (원)", min_value=0, value=0, step=10000, format="%d")
    with tc2:
        연금계좌공제 = st.number_input("연금계좌세액공제 (원)", min_value=0, value=0, step=10000, format="%d")
    with tc3:
        표준세액공제 = 70000
        st.text_input("표준세액공제 (원)", value="70,000", disabled=True, help="고정값 70,000원")

    총세액공제 = 퇴직연금공제 + 연금계좌공제 + 표준세액공제

    st.divider()

    # ── 기납부세액 ──
    st.subheader("④ 기납부세액")
    pc1, pc2 = st.columns(2)
    with pc1:
        중간예납 = st.number_input("중간예납세액 (원)", min_value=0, value=0, step=10000, format="%d")
    with pc2:
        원천징수 = st.number_input("원천징수세액 (원)", min_value=0, value=0, step=10000, format="%d")

    총기납부 = 중간예납 + 원천징수

    st.divider()

    # ── 계산 버튼 ──
    if st.button("종합소득세 계산", use_container_width=True, type="primary"):

        과세표준 = max(소득금액_input - 총소득공제, 0)
        산출세액 = calc_income_tax(과세표준)
        납부세액 = max(산출세액 - 총세액공제, 0)
        최종납부 = max(납부세액 - 총기납부, 0)
        환급세액 = max(총기납부 - 납부세액, 0)
        지방소득세 = round(납부세액 * 0.1)

        # 해당 세율 구간 찾기
        for limit, rate, deduction in TAX_BRACKETS:
            if 과세표준 <= limit:
                적용세율 = rate
                누진공제액 = deduction
                break

        st.divider()
        st.subheader("📊 종합소득세 계산 결과")

        # 계산 구조 표
        steps = [
            ("소득금액",           f"{소득금액_input:>20,.0f} 원", ""),
            ("(-) 소득공제",       f"{총소득공제:>20,.0f} 원",
             f"국민연금 {국민연금:,.0f} + 소상공인 {소상공인공제:,.0f} + 인적공제 {인적공제:,.0f}"),
            ("= 과세표준",         f"{과세표준:>20,.0f} 원", ""),
            (f"× 세율 ({적용세율*100:.0f}%)", f"{round(과세표준 * 적용세율):>20,.0f} 원", ""),
            (f"(-) 누진공제",      f"{누진공제액:>20,.0f} 원", ""),
            ("= 산출세액",         f"{산출세액:>20,.0f} 원", ""),
            ("(-) 세액공제",       f"{총세액공제:>20,.0f} 원",
             f"퇴직연금 {퇴직연금공제:,.0f} + 연금계좌 {연금계좌공제:,.0f} + 표준 {표준세액공제:,.0f}"),
            ("= 납부세액",         f"{납부세액:>20,.0f} 원", ""),
            ("(-) 기납부세액",     f"{총기납부:>20,.0f} 원",
             f"중간예납 {중간예납:,.0f} + 원천징수 {원천징수:,.0f}"),
        ]

        for label, value, note in steps:
            r1, r2, r3 = st.columns([3, 3, 4])
            r1.write(f"**{label}**")
            r2.write(value)
            if note:
                r3.caption(note)

        st.divider()

        # 최종 결과 카드
        res1, res2, res3 = st.columns(3)
        if 환급세액 > 0:
            res1.metric("🔴 종합소득세 납부세액", f"{최종납부:,.0f}원")
            res2.metric("💚 환급세액", f"{환급세액:,.0f}원")
        else:
            res1.metric("🔴 종합소득세 납부세액", f"{최종납부:,.0f}원")
            res2.metric("🏙️ 지방소득세 (10%)", f"{지방소득세:,.0f}원")

        res3.metric("💳 총 세금 부담", f"{최종납부 + 지방소득세:,.0f}원",
                    help="종합소득세 + 지방소득세 합계")

        st.caption(f"※ 적용 세율: {적용세율*100:.0f}% / 누진공제: {누진공제액:,.0f}원 / 지방소득세는 납부세액의 10%")
        st.caption("※ 본 계산은 추정값이며 실제 세액은 세무사 확인 후 신고하시기 바랍니다.")


# ════════════════════════════════════════════════════
# TAB 3 : 카드사 변환기
# ════════════════════════════════════════════════════
with tab3:
    st.title("💳 카드사 통합 변환기")
    st.caption("카드사 엑셀 파일을 업로드하면 세무사랑 업로드용 파일로 자동 변환합니다.")

    st.info("📌 파일명 형식: **상호명_사업자번호_카드사_카드번호_직원유무_차량유무.xlsx**\n\n예) 용은물류_3020895715_삼성카드_5120-2800-0000-5697_직원없음_차량있음.xlsx")

    st.subheader("① 카드사 파일 업로드")
    st.caption("삼성카드, 하나카드, 신한카드, 비씨카드 지원 / 여러 파일 동시 업로드 가능")
    uploaded_cards = st.file_uploader(
        "카드사 엑셀 파일 선택",
        type=["xlsx"],
        accept_multiple_files=True,
        key="card_files"
    )

    if uploaded_cards:
        st.divider()
        if st.button("🔄 변환 시작", use_container_width=True, type="primary"):
            all_rows, all_stats = [], []
            errors = []

            with st.spinner("변환 중..."):
                for uf in uploaded_cards:
                    info = parse_filename_card(uf.name)
                    card_co = info["신용카드사명"]
                    card_no = info["신용카드번호"]
                    file_bytes = uf.read()

                    try:
                        if "삼성" in card_co:
                            rows, stats = parse_samsung_card(file_bytes, card_co, card_no)
                        elif "하나" in card_co:
                            rows, stats = parse_hana_card(file_bytes, card_co, card_no)
                        elif "신한" in card_co:
                            rows, stats = parse_shinhan_card(file_bytes, card_co, card_no)
                        elif "비씨" in card_co or "BC" in card_co.upper():
                            rows, stats = parse_bc_card(file_bytes, card_co, card_no)
                        else:
                            errors.append(f"⚠️ {uf.name}: 지원하지 않는 카드사 ({card_co})\n지원: 삼성/하나/신한/비씨카드")
                            continue
                        all_rows.append(rows)
                        all_stats.append(stats)
                    except Exception as e:
                        errors.append(f"❌ {uf.name}: {e}")

            if errors:
                for e in errors: st.error(e)

            if all_rows:
                combined = pd.concat(all_rows, ignore_index=True)
                stats_df = pd.concat(all_stats, ignore_index=True)

                total_cnt = len(combined)
                classified = len(stats_df[stats_df['계정과목'] != '미분류'])
                unclassified = total_cnt - classified
                first_info = parse_filename_card(uploaded_cards[0].name)

                st.divider()
                st.subheader("📊 변환 결과")
                c1, c2, c3 = st.columns(3)
                c1.metric("총 거래건수", f"{total_cnt:,}건")
                c2.metric("자동 분류", f"{classified:,}건", f"{classified/total_cnt*100:.1f}%")
                c3.metric("미분류", f"{unclassified:,}건", delta_color="inverse")

                amt_col = st.columns(3)
                amt_col[0].metric("공급가액 합계", f"{combined['공급가액'].sum():,.0f}원")
                amt_col[1].metric("세액 합계", f"{combined['세액'].sum():,.0f}원")
                amt_col[2].metric("합계금액", f"{combined['합계금액'].sum():,.0f}원")

                # 계정과목별 집계
                st.divider()
                st.subheader("📋 계정과목별 집계")
                summary = stats_df.groupby('계정과목')['금액'].agg(['count','sum']).reset_index()
                summary.columns = ['계정과목', '건수', '금액합계']
                summary['금액합계'] = summary['금액합계'].apply(lambda x: f"{x:,.0f}원")
                st.dataframe(summary, use_container_width=True, hide_index=True)

                if unclassified > 0:
                    st.warning(f"⚠️ 미분류 {unclassified}건은 리포트 파일에서 확인 후 수동 수정하세요.")

                st.divider()
                st.subheader("⬇️ 파일 다운로드")

                # 세무사랑 파일 생성
                try:
                    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    세무사랑_data = write_to_template(
                        combined,
                        first_info["업체명"],
                        first_info["사업자번호"]
                    )
                    dl1, dl2 = st.columns(2)
                    with dl1:
                        st.download_button(
                            label="📥 세무사랑 업로드 파일",
                            data=세무사랑_data,
                            file_name=f"세무사랑_통합_{ts}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            type="primary"
                        )
                    with dl2:
                        buf = io.BytesIO()
                        stats_df.to_excel(buf, index=False, engine='openpyxl')
                        st.download_button(
                            label="📊 분류 리포트",
                            data=buf.getvalue(),
                            file_name=f"분류리포트_{ts}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True
                        )
                except Exception as e:
                    st.error(f"파일 생성 오류: {e}")
                    st.info("세무사랑 템플릿 없이 리포트만 다운로드 가능합니다.")
                    buf = io.BytesIO()
                    stats_df.to_excel(buf, index=False, engine='openpyxl')
                    st.download_button("📊 분류 리포트 다운로드", buf.getvalue(), f"분류리포트_{ts}.xlsx")
