import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime
import plotly.graph_objects as go

# --- 페이지 기본 설정 ---
st.set_page_config(
    page_title="수급 퀀트 레이더 대시보드",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 텔레그램 발송 유틸 함수 ---
def send_telegram_alert(token, chat_id, message):
    if not token or not chat_id:
        return False, "토큰 또는 Chat ID가 설정되지 않았습니다."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            return True, "전송 성공"
        return False, f"오류 코드: {res.status_code}"
    except Exception as e:
        return False, str(e)


# ==========================================
# 1. 미국 주식 연산 (CBOE CDN + Stooq 연동)
# ==========================================
@st.cache_data(ttl=300)
def get_us_stock_data(ticker_symbol):
    ticker_symbol = ticker_symbol.upper().strip()
    current_price, ma20, std20, z_score = None, None, None, 0.0

    # 1) 주가 이력 조회 (Stooq 무료 데이터망)
    try:
        stooq_url = f"https://stooq.com/q/d/l/?s={ticker_symbol.lower()}.us&i=d"
        df_hist = pd.read_csv(stooq_url)
        df_hist.columns = [c.capitalize() for c in df_hist.columns]
        
        if not df_hist.empty and 'Close' in df_hist.columns and len(df_hist) >= 20:
            df_hist['Date'] = pd.to_datetime(df_hist['Date'])
            df_hist = df_hist.sort_values('Date').reset_index(drop=True)
            current_price = float(df_hist['Close'].iloc[-1])
            ma20 = float(df_hist['Close'].rolling(20).mean().iloc[-1])
            std20 = float(df_hist['Close'].rolling(20).std().iloc[-1])
            z_score = (current_price - ma20) / std20 if std20 > 0 else 0.0
    except Exception:
        pass

    # 2) 옵션 체인 및 Max Pain 연산 (CBOE 거래소 직결)
    max_pain, call_wall, put_wall = None, None, None
    calls_df, puts_df, selected_exp = None, None, None

    try:
        cboe_url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker_symbol}.json"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
        res = requests.get(cboe_url, headers=headers, timeout=8)

        if res.status_code == 200:
            cboe_data = res.json().get('data', {})
            if current_price is None:
                current_price = float(cboe_data.get('current_price', 0.0))

            raw_options = cboe_data.get('options', [])
            if raw_options:
                parsed_list = []
                pattern = re.compile(r'^([A-Za-z0-9]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$')

                for opt in raw_options:
                    sym = opt.get('option', '')
                    m = pattern.match(sym)
                    if m:
                        yy = int(m.group(2)) + 2000
                        mm = int(m.group(3))
                        dd = int(m.group(4))
                        exp_str = f"{yy:04d}-{mm:02d}-{dd:02d}"
                        opt_type = 'call' if m.group(5) == 'C' else 'put'
                        strike = int(m.group(6)) / 1000.0
                        oi = float(opt.get('open_interest', 0) or 0)

                        parsed_list.append({
                            'exp_date': exp_str,
                            'type': opt_type,
                            'strike': strike,
                            'openInterest': oi
                        })

                if parsed_list:
                    df_all = pd.DataFrame(parsed_list)
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    future_exps = sorted([d for d in df_all['exp_date'].unique() if d >= today_str])
                    selected_exp = future_exps[0] if future_exps else sorted(df_all['exp_date'].unique())[0]

                    target_df = df_all[df_all['exp_date'] == selected_exp]
                    calls_df = target_df[target_df['type'] == 'call'][['strike', 'openInterest']].reset_index(drop=True)
                    puts_df = target_df[target_df['type'] == 'put'][['strike', 'openInterest']].reset_index(drop=True)

                    # Max Pain 연산
                    all_strikes = sorted(list(set(calls_df['strike']).union(set(puts_df['strike']))))
                    total_loss = {}
                    for s in all_strikes:
                        c_loss = np.maximum(0, s - calls_df['strike']) * calls_df['openInterest']
                        p_loss = np.maximum(0, puts_df['strike'] - s) * puts_df['openInterest']
                        total_loss[s] = c_loss.sum() + p_loss.sum()

                    if total_loss:
                        max_pain = min(total_loss, key=total_loss.get)

                    if not calls_df.empty and calls_df['openInterest'].sum() > 0:
                        call_wall = calls_df.loc[calls_df['openInterest'].idxmax()]['strike']
                    if not puts_df.empty and puts_df['openInterest'].sum() > 0:
                        put_wall = puts_df.loc[puts_df['openInterest'].idxmax()]['strike']
    except Exception:
        pass

    if current_price is None:
        return None

    return {
        'price': current_price,
        'ma20': ma20 if ma20 is not None else current_price,
        'z_score': z_score,
        'exp_date': selected_exp,
        'max_pain': max_pain,
        'call_wall': call_wall,
        'put_wall': put_wall,
        'calls': calls_df,
        'puts': puts_df
    }


# ==========================================
# 2. 한국 주식 연산 (네이버 금융 직결)
# ==========================================
@st.cache_data(ttl=300)
def get_kr_stock_data(ticker_symbol):
    code = re.sub(r'[^0-9]', '', str(ticker_symbol))
    if not code:
        return None

    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=90&requestType=0"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    try:
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code != 200:
            return None

        items = re.findall(r'<item data="([^"]+)"', res.text)
        if not items:
            return None

        records = [line.split('|') for line in items]
        df = pd.DataFrame(records, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])

        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')
        df = df.dropna().sort_values('Date').reset_index(drop=True)
        df.index = df['Date']

        if len(df) < 20:
            return None

        current_price = float(df['Close'].iloc[-1])
        ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
        std20 = float(df['Close'].rolling(20).std().iloc[-1])
        z_score = (current_price - ma20) / std20 if std20 > 0 else 0.0

        # 매물대(Volume Profile) 15개 구간 연산
        counts, bin_edges = np.histogram(df['Close'], bins=15, weights=df['Volume'])
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

        above = bin_centers[bin_centers >= current_price]
        above_vol = counts[bin_centers >= current_price]
        below = bin_centers[bin_centers < current_price]
        below_vol = counts[bin_centers < current_price]

        resistance = float(above[np.argmax(above_vol)]) if len(above) > 0 and len(above_vol) > 0 else current_price * 1.05
        support = float(below[np.argmax(below_vol)]) if len(below) > 0 and len(below_vol) > 0 else current_price * 0.95

        return {
            'price': current_price,
            'resistance': resistance,
            'support': support,
            'z_score': z_score,
            'df': df,
            'ohlcv': df,
            'df_ohlcv': df
        }
    except Exception:
        return None


# ==========================================
# 3. 사이드바 및 환경 설정
# ==========================================
st.sidebar.header("⚙️ 시스템 및 알림 설정")

# Streamlit Cloud의 Secrets가 있으면 우선 불러오고, 없으면 기본 빈 값
default_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "") if hasattr(st, "secrets") else ""
default_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "") if hasattr(st, "secrets") else ""

bot_token = st.sidebar.text_input("Telegram Bot Token", value=default_token, type="password")
chat_id = st.sidebar.text_input("Telegram Chat ID", value=default_chat_id)
enable_alert = st.sidebar.checkbox("임계치 도달 시 자동 알림 발송", value=True)

if st.sidebar.button("🔔 텔레그램 연결 테스트"):
    success, msg = send_telegram_alert(bot_token, chat_id, "✅ *[수급 퀀트 레이더]* 텔레그램 알림 시스템 정상 연동 완료!")
    if success:
        st.sidebar.success("테스트 메시지 발송 완료!")
    else:
        st.sidebar.error(f"전송 실패: {msg}")


# ==========================================
# 4. 메인 대시보드 화면
# ==========================================
st.title("📈 수급 퀀트 레이더 대시보드")

tab1, tab2 = st.tabs(["🇺🇸 미국 관심 종목", "🇰🇷 국내 관심 종목"])

# --- 미국 주식 탭 ---
with tab1:
    us_tickers = ["AMD", "NVDA", "TSLA", "AAPL", "MSFT", "MU", "AMZN", "GOOGL", "SPY", "QQQ"]
    selected_us = st.selectbox("종목 선택", us_tickers, index=0, key="us_select")
    
    data_us = get_us_stock_data(selected_us)
    
    if data_us:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("현재가", f"${data_us['price']:,.2f}")
        m2.metric("Max Pain", f"${data_us['max_pain']:,.2f}" if data_us['max_pain'] else "N/A")
        m3.metric("Call Wall (저항)", f"${data_us['call_wall']:,.2f}" if data_us['call_wall'] else "N/A")
        m4.metric("Put Wall (지지)", f"${data_us['put_wall']:,.2f}" if data_us['put_wall'] else "N/A")
        m5.metric("가격 Z-Score (20D)", f"{data_us['z_score']:+.2f} σ")

        st.caption(f"기준 옵션 만기일: {data_us['exp_date'] if data_us['exp_date'] else '옵션 없음'}")

        # 옵션 미결제약정(OI) 분포 차트
        calls = data_us['calls']
        puts = data_us['puts']
        if calls is not None and puts is not None and not calls.empty and not puts.empty:
            merged = pd.merge(calls, puts, on="strike", how="outer", suffixes=('_call', '_put')).fillna(0)
            # 현재가 기준 상하 25% 구간만 필터링하여 가독성 확보
            curr_p = data_us['price']
            merged = merged[(merged['strike'] >= curr_p * 0.75) & (merged['strike'] <= curr_p * 1.25)]
            merged = merged.sort_values("strike")

            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=merged['strike'], y=merged['openInterest_call'],
                name='Call OI (저항)', marker_color='rgba(239, 83, 80, 0.75)'
            ))
            fig.add_trace(go.Bar(
                x=merged['strike'], y=merged['openInterest_put'],
                name='Put OI (지지)', marker_color='rgba(66, 165, 245, 0.75)'
            ))

            # 현재가 및 Max Pain 수직 라인
            fig.add_vline(x=curr_p, line_dash="dash", line_color="white", annotation_text=f"현재가: ${curr_p:.2f}")
            if data_us['max_pain']:
                fig.add_vline(x=data_us['max_pain'], line_dash="dot", line_color="gold", annotation_text=f"Max Pain: ${data_us['max_pain']:.2f}")

            fig.update_layout(
                title=f"{selected_us} 스트라이크별 미결제약정(OI) 프로파일 ({data_us['exp_date']})",
                barmode='group',
                xaxis_title="Strike ($)",
                yaxis_title="Open Interest",
                template="plotly_dark",
                height=420,
                margin=dict(l=20, r=20, t=40, b=20)
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")


# --- 한국 주식 탭 ---
with tab2:
    kr_tickers = {
        "한솔케미칼 (014680)": "014680",
        "삼성전자 (005930)": "005930",
        "SK하이닉스 (000660)": "000660",
        "LG에너지솔루션 (373220)": "373220",
        "에코프로비엠 (247540)": "247540",
        "포스코홀딩스 (005490)": "005490",
        "현대차 (005380)": "005380"
    }
    selected_kr_name = st.selectbox("종목 선택", list(kr_tickers.keys()), index=0, key="kr_select")
    selected_kr_code = kr_tickers[selected_kr_name]

    data_kr = get_kr_stock_data(selected_kr_code)

    if data_kr:
        k1, k2, k3, k4 = st.columns(4)
        price_val = f"{int(data_kr['price']):,}원" if pd.notnull(data_kr['price']) else "N/A"
        res_val = f"{int(data_kr['resistance']):,}원" if pd.notnull(data_kr['resistance']) else "N/A"
        sup_val = f"{int(data_kr['support']):,}원" if pd.notnull(data_kr['support']) else "N/A"
        z_val = f"{data_kr['z_score']:+.2f} σ" if pd.notnull(data_kr['z_score']) else "N/A"

        k1.metric("현재가", price_val)
        k2.metric("매물대 저항선", res_val)
        k3.metric("매물대 지지선", sup_val)
        k4.metric("가격 Z-Score (20D)", z_val)

        # 캔들스틱 및 매물대 지지/저항 차트
        df_chart = data_kr['ohlcv'].tail(45)
        fig_kr = go.Figure()

        fig_kr.add_trace(go.Candlestick(
            x=df_chart['Date'],
            open=df_chart['Open'],
            high=df_chart['High'],
            low=df_chart['Low'],
            close=df_chart['Close'],
            name='주가',
            increasing_line_color='#ef5350',
            decreasing_line_color='#42a5f5'
        ))

        # 매물대 지지/저항 가로선
        fig_kr.add_hline(y=data_kr['resistance'], line_dash="dash", line_color="rgba(239, 83, 80, 0.8)", annotation_text=f"저항선 {int(data_kr['resistance']):,}원")
        fig_kr.add_hline(y=data_kr['support'], line_dash="dash", line_color="rgba(66, 165, 245, 0.8)", annotation_text=f"지지선 {int(data_kr['support']):,}원")

        fig_kr.update_layout(
            title=f"{selected_kr_name} 주가 추이 및 주요 매물대 라인",
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
            height=430,
            margin=dict(l=20, r=20, t=40, b=20)
        )
        st.plotly_chart(fig_kr, use_container_width=True)
    else:
        st.warning("데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
