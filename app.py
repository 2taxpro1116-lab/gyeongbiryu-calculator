import os
import json
import pandas as pd
import anthropic
import streamlit as st
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


def get_expense_distribution(business_info, accounts, use_purchase):
    api_key = st.secrets.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    excluded = get_excluded_accounts(business_info["중분류"])
    account_list = [a for a in accounts["판관비"] if a not in excluded]

    if use_purchase:
        all_accounts = ["당기상품매입액"] + account_list
    else:
        all_accounts = account_list

    expense_rate = float(business_info["단순일반율"])

    prompt = f"""당신은 세무 전문가입니다. 아래 업종의 단순경비율을 계정과목별로 배분해주세요.

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

# ── 탭 구성 ──────────────────────────────────────────
tab1, tab2 = st.tabs(["📊 경비율 계산기", "🧾 종합소득세 계산기"])


# ════════════════════════════════════════════════════
# TAB 1 : 경비율 계산기
# ════════════════════════════════════════════════════
with tab1:
    st.title("📊 단순경비율 계정과목 배분 계산기")
    st.caption("업종코드와 매출액을 입력하면 Claude AI가 계정과목별 경비를 산정합니다.")

    with st.form("calc_form"):
        col1, col2 = st.columns(2)
        with col1:
            업종코드 = st.text_input("업종코드", placeholder="예) 552101")
        with col2:
            매출액_input = st.text_input("매출액 (원)", placeholder="예) 150,000,000")

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

            df = load_data()
            accounts = load_accounts()
            business = find_business(df, 업종코드.strip())

            if business is None:
                st.error(f"업종코드 **{업종코드}** 를 찾을 수 없습니다.")
                st.stop()

            use_purchase = has_product_purchase(business["중분류"])

            st.divider()
            st.subheader("📌 업종 정보")
            c1, c2, c3 = st.columns(3)
            c1.metric("업종명", business["세세분류"])
            c2.metric("단순경비율(일반)", f"{business['단순일반율']}%")
            c3.metric("상품매입 계정", "사용" if use_purchase else "미사용")
            st.caption(f"중분류: {business['중분류']} / 소분류: {business['소분류']} / 세분류: {business['세분류']}")

            st.divider()
            with st.spinner("Claude AI가 계정과목별 비율을 산정하고 있습니다..."):
                try:
                    distribution = get_expense_distribution(business, accounts, use_purchase)
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
