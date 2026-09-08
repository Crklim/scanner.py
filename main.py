import os
import requests
import yfinance as yf
import numpy as np
import pandas as pd

# GitHub Actions Secrets에서 로드
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("토큰 또는 Chat ID 미설정")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)

US_TICKERS = ["AMD", "MU", "GOOG", "TSLA", "PLTR", "VST", "SPCX", "NVDA", "AMZN", "CRWD", "BMNR", "MSFT", "CRCL"]

KR_TICKERS = {
    "한솔케미칼": "014680.KS", "한미반도체": "042700.KS", "삼성전자": "005930.KS",
    "알테오젠": "196170.KQ", "삼성전기": "009150.KS", "에코프로": "086520.KQ",
    "삼성바이오로직스": "207940.KS", "SK하이닉스": "000660.KS", "삼성SDI": "006400.KS",
    "주성엔지니어링": "036930.KQ", "후성": "093370.KS"
}

def scan_us():
    alerts = []
    for sym in US_TICKERS:
        try:
            t = yf.Ticker(sym)
            h = t.history(period="60d")
            if h.empty: continue
            price = h['Close'].iloc[-1]
            ma = h['Close'].rolling(20).mean().iloc[-1]
            std = h['Close'].rolling(20).std().iloc[-1]
            z = (price - ma) / std if std > 0 else 0
            
            cw, pw = None, None
            if t.options:
                chain = t.option_chain(t.options[0])
                if not chain.calls.empty and chain.calls['openInterest'].sum() > 0:
                    cw = chain.calls.loc[chain.calls['openInterest'].idxmax()]['strike']
                if not chain.puts.empty and chain.puts['openInterest'].sum() > 0:
                    pw = chain.puts.loc[chain.puts['openInterest'].idxmax()]['strike']

            sym_alert = []
            if z >= 2.0: sym_alert.append(f"과열(+{z:.2f}σ)")
            elif z <= -2.0: sym_alert.append(f"침체({z:.2f}σ)")
            if cw and abs(price - cw) / cw <= 0.015: sym_alert.append(f"Call Wall(${cw}) 저항")
            if pw and abs(price - pw) / pw <= 0.015: sym_alert.append(f"Put Wall(${pw}) 지지")
            
            if sym_alert:
                alerts.append(f"• *{sym}* (${price:.2f}): " + ", ".join(sym_alert))
        except Exception as e:
            print(f"{sym} 오류: {e}")
    return alerts

def scan_kr():
    alerts = []
    for name, code in KR_TICKERS.items():
        try:
            t = yf.Ticker(code)
            h = t.history(period="90d")
            if h.empty: continue
            price = h['Close'].iloc[-1]
            ma = h['Close'].rolling(20).mean().iloc[-1]
            std = h['Close'].rolling(20).std().iloc[-1]
            z = (price - ma) / std if std > 0 else 0
            
            sym_alert = []
            if z >= 2.0: sym_alert.append(f"과열(+{z:.2f}σ)")
            elif z <= -2.0: sym_alert.append(f"침체({z:.2f}σ)")
            
            if sym_alert:
                alerts.append(f"• *{name}* ({int(price):,}원): " + ", ".join(sym_alert))
        except Exception as e:
            print(f"{name} 오류: {e}")
    return alerts

if __name__ == "__main__":
    us_signals = scan_us()
    kr_signals = scan_kr()
    
    msg_lines = ["📡 *[GitHub 퀀트 레이더 정기 스캔]*\n"]
    if us_signals:
        msg_lines.append("🇺🇸 *미국 주식 수급 시그널:*")
        msg_lines.extend(us_signals)
        msg_lines.append("")
    else:
        msg_lines.append("🇺🇸 *미국 주식:* 특이 과열/침체/Wall 종목 없음 (중립)")
        msg_lines.append("")

    if kr_signals:
        msg_lines.append("🇰🇷 *국내 주식 수급 시그널:*")
        msg_lines.extend(kr_signals)
        msg_lines.append("")
    else:
        msg_lines.append("🇰🇷 *국내 주식:* 특이 과열/침체 종목 없음 (중립)")
        msg_lines.append("")
        
    # 특이 종목이 없더라도 정상 스캔 완료 브리핑을 항상 발송
    send_telegram("\n".join(msg_lines))
    print("텔레그램 브리핑 전송 완료!")
