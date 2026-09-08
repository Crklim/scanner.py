import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

st.set_page_config(layout="wide", page_title="US/KR Quant & Supply Dashboard")

# 0. 텔레그램 설정 & 세션 상태 관리 (secrets.toml에서 안전하게 로드)
DEFAULT_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

if "sent_alerts" not in st.session_state:
    st.session_state["sent_alerts"] = {}

def send_telegram_alert(bot_token, chat_id, message, alert_key=None, force=False):
    now = datetime.now()
    if not force and alert_key:
        last_sent = st.session_state["sent_alerts"].get(alert_key)
        if last_sent and (now - last_sent) < timedelta(hours=1):
            return False, "1시간 이내 발송 이력이 있어 스킵했습니다."

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            if alert_key:
                st.session_state["sent_alerts"][alert_key] = now
            return True, "전송 성공"
        else:
            return False, f"전송 실패 (HTTP {res.status_code})"
    except Exception as e:
        return False, str(e)

# 사이드바 설정
st.sidebar.header("⚙️ 시스템 및 알림 설정")
telegram_token = st.sidebar.text_input("Telegram Bot Token", value=DEFAULT_BOT_TOKEN, type="password")
telegram_chat_id = st.sidebar.text_input("Telegram Chat ID", value=DEFAULT_CHAT_ID)
auto_alert_enabled = st.sidebar.checkbox("임계치 도달 시 자동 알림 발송", value=True)

if st.sidebar.button("🔔 텔레그램 연결 테스트"):
    success, log = send_telegram_alert(
        telegram_token, 
        telegram_chat_id, 
        "✅ *[Quant Radar]* 텔레그램 봇 연동 테스트 메시지입니다.", 
        force=True
    )
    if success:
        st.sidebar.success("텔레그램 발송 성공!")
    else:
        st.sidebar.error(f"발송 오류: {log}")
# 1. 미국 주식 연산 (Yahoo v7 옵션 API 직접 호출 방식)
def get_us_stock_data(ticker_symbol):
    # 가격 및 기술적 지표 산출
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="60d")
    if hist.empty:
        return None
    
    current_price = hist['Close'].iloc[-1]
    ma20 = hist['Close'].rolling(window=20).mean().iloc[-1]
    std20 = hist['Close'].rolling(window=20).std().iloc[-1]
    z_score = (current_price - ma20) / std20 if std20 > 0 else 0
    
    max_pain, call_wall, put_wall = None, None, None
    calls_df, puts_df, selected_exp = None, None, None
    
    # Yahoo Finance v7 API 직접 호출 (클라우드 환경 차단 우회)
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker_symbol}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get('optionChain', {}).get('result', [])
            if data:
                opt_data = data[0]
                expirations = opt_data.get('expirationDates', [])
                options_list = opt_data.get('options', [])
                
                if expirations and options_list:
                    # 첫 번째 만기일 포맷 변환 (YYYY-MM-DD)
                    selected_exp = datetime.fromtimestamp(expirations[0]).strftime('%Y-%m-%d')
                    first_opt = options_list[0]
                    raw_calls = first_opt.get('calls', [])
                    raw_puts = first_opt.get('puts', [])
                    
                    if raw_calls or raw_puts:
                        calls_df = pd.DataFrame(raw_calls) if raw_calls else pd.DataFrame(columns=['strike', 'openInterest'])
                        puts_df = pd.DataFrame(raw_puts) if raw_puts else pd.DataFrame(columns=['strike', 'openInterest'])
                        
                        for df in [calls_df, puts_df]:
                            if 'openInterest' not in df.columns:
                                df['openInterest'] = 0
                            else:
                                df['openInterest'] = df['openInterest'].fillna(0)
                        
                        # Max Pain 연산
                        all_strikes = sorted(list(set(calls_df['strike']).union(set(puts_df['strike']))))
                        total_loss = {}
                        for s in all_strikes:
                            call_loss = np.maximum(0, s - calls_df['strike']) * calls_df['openInterest']
                            put_loss = np.maximum(0, puts_df['strike'] - s) * puts_df['openInterest']
                            total_loss[s] = call_loss.sum() + put_loss.sum()
                        
                        if total_loss:
                            max_pain = min(total_loss, key=total_loss.get)
                            
                        if not calls_df.empty and calls_df['openInterest'].sum() > 0:
                            call_wall = calls_df.loc[calls_df['openInterest'].idxmax()]['strike']
                        if not puts_df.empty and puts_df['openInterest'].sum() > 0:
                            put_wall = puts_df.loc[puts_df['openInterest'].idxmax()]['strike']
    except Exception:
        pass
            
    return {
        'price': current_price,
        'ma20': ma20,
        'z_score': z_score,
        'exp_date': selected_exp,
        'max_pain': max_pain,
        'call_wall': call_wall,
        'put_wall': put_wall,
        'calls': calls_df,
        'puts': puts_df
    }

# 2. 한국 주식 연산
def get_kr_stock_data(full_ticker):
    ticker = yf.Ticker(full_ticker)
    df_ohlcv = ticker.history(period="90d")
    if df_ohlcv.empty:
        return None
        
    current_price = df_ohlcv['Close'].iloc[-1]
    ma20 = df_ohlcv['Close'].rolling(window=20).mean().iloc[-1]
    std20 = df_ohlcv['Close'].rolling(window=20).std().iloc[-1]
    z_score = (current_price - ma20) / std20 if std20 > 0 else 0
    
    bins = np.linspace(df_ohlcv['Low'].min(), df_ohlcv['High'].max(), 15)
    volume_profile, bin_edges = np.histogram(df_ohlcv['Close'], bins=bins, weights=df_ohlcv['Volume'])
    
    upper_bins = [i for i, edge in enumerate(bin_edges[:-1]) if edge > current_price]
    lower_bins = [i for i, edge in enumerate(bin_edges[1:]) if edge < current_price]
    
    kr_resistance = bin_edges[upper_bins[np.argmax(volume_profile[upper_bins])]] if upper_bins else current_price * 1.05
    kr_support = bin_edges[lower_bins[np.argmax(volume_profile[lower_bins])]] if lower_bins else current_price * 0.95
    
    return {
        'price': current_price,
        'ma20': ma20,
        'z_score': z_score,
        'resistance': kr_resistance,
        'support': kr_support,
        'ohlcv': df_ohlcv
    }

# UI
st.title("📈 수급 퀀트 레이더 대시보드")
tab1, tab2 = st.tabs(["🇺🇸 미국 관심 종목", "🇰🇷 국내 관심 종목"])

US_TICKERS = ["AMD", "MU", "GOOG", "TSLA", "PLTR", "VST", "SPCX", "NVDA", "AMZN", "CRWD", "BMNR", "MSFT", "CRCL"]
KR_TICKERS = {
    "한솔케미칼 (014680)": "014680.KS", "한미반도체 (042700)": "042700.KS", "삼성전자 (005930)": "005930.KS",
    "알테오젠 (196170)": "196170.KQ", "삼성전기 (009150)": "009150.KS", "에코프로 (086520)": "086520.KQ",
    "삼성바이오로직스 (207940)": "207940.KS", "SK하이닉스 (000660)": "000660.KS", "삼성SDI (006400)": "006400.KS",
    "주성엔지니어링 (036930)": "036930.KQ", "후성 (093370)": "093370.KS"
}

with tab1:
    col_u1, col_u2 = st.columns([1, 4])
    with col_u1:
        us_ticker = st.selectbox("종목 선택", US_TICKERS)
    with st.spinner(f"{us_ticker} 데이터 수집 중..."):
        data_us = get_us_stock_data(us_ticker)
        
    if data_us:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("현재가", f"${data_us['price']:.2f}")
        m2.metric("Max Pain", f"${data_us['max_pain']:.2f}" if data_us['max_pain'] else "N/A")
        m3.metric("Call Wall (저항)", f"${data_us['call_wall']:.2f}" if data_us['call_wall'] else "N/A")
        m4.metric("Put Wall (지지)", f"${data_us['put_wall']:.2f}" if data_us['put_wall'] else "N/A")
        m5.metric("가격 Z-Score (20D)", f"{data_us['z_score']:+.2f} σ")
        
        st.caption(f"기준 옵션 만기일: {data_us['exp_date'] if data_us['exp_date'] else '옵션 없음'}")
        
        if data_us['calls'] is not None and data_us['puts'] is not None:
            p = data_us['price']
            c_fil = data_us['calls'][(data_us['calls']['strike'] >= p * 0.8) & (data_us['calls']['strike'] <= p * 1.2)]
            p_fil = data_us['puts'][(data_us['puts']['strike'] >= p * 0.8) & (data_us['puts']['strike'] <= p * 1.2)]
            
            fig = go.Figure()
            fig.add_trace(go.Bar(x=c_fil['strike'], y=c_fil['openInterest'], name='Call OI', marker_color='red', opacity=0.7))
            fig.add_trace(go.Bar(x=p_fil['strike'], y=p_fil['openInterest'], name='Put OI', marker_color='blue', opacity=0.7))
            fig.add_vline(x=p, line_dash="dash", line_color="green", annotation_text="현재가")
            if data_us['max_pain']:
                fig.add_vline(x=data_us['max_pain'], line_dash="dot", line_color="orange", annotation_text="Max Pain")
            fig.update_layout(title=f"{us_ticker} Open Interest 행사가별 분포", barmode='group')
            st.plotly_chart(fig, use_container_width=True)

with tab2:
    col_k1, col_k2 = st.columns([1, 4])
    with col_k1:
        kr_name = st.selectbox("종목 선택", list(KR_TICKERS.keys()))
        full_code = KR_TICKERS[kr_name]
    with st.spinner(f"{kr_name} 데이터 수집 중..."):
        data_kr = get_kr_stock_data(full_code)
        
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
        df_chart = data_kr['ohlcv'].tail(40)
        fig_kr = go.Figure(data=[go.Candlestick(
            x=df_chart.index, open=df_chart['Open'], high=df_chart['High'],
            low=df_chart['Low'], close=df_chart['Close'], name="주가"
        )])
        fig_kr.add_hline(y=data_kr['resistance'], line_dash="dash", line_color="red", annotation_text="저항선")
        fig_kr.add_hline(y=data_kr['support'], line_dash="dash", line_color="blue", annotation_text="지지선")
        fig_kr.update_layout(title=f"{kr_name} 캔들 차트 및 매물대", xaxis_rangeslider_visible=False)
        st.plotly_chart(fig_kr, use_container_width=True)
