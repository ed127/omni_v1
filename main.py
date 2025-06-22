import os
import pandas as pd
import time
import logging
import numpy as np
import requests
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from xgboost import XGBClassifier
from sklearn.preprocessing import StandardScaler
from telegram.ext import Application
from dotenv import load_dotenv
import asyncio
import warnings
from flask import Flask
from threading import Thread
from datetime import datetime
from apexomni.constants import APEX_OMNI_HTTP_TEST
from apexomni.http_public import HttpPublic
from apexomni.http_private import HttpPrivate

warnings.filterwarnings("ignore", category=UserWarning)
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CONFIDENCE_THRESHOLD = 99
LIMIT = 500

application = Application.builder().token(TELEGRAM_TOKEN).build()
telegram_bot = application.bot

open_positions = {}
TP_SL_UPDATE_THRESHOLD = 0.01
client = HttpPublic(APEX_OMNI_HTTP_TEST)
client_private = HttpPrivate(
    base_url=APEX_OMNI_HTTP_TEST,
    api_key=os.getenv("APEX_API_KEY"),
    api_secret=os.getenv("APEX_API_SECRET"),
    api_passphrase=os.getenv("APEX_API_PASSPHRASE")
)

async def send_alert(message):
    await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

def create_targets(df):
    df = df.copy()
    df.loc[:, 'target'] = (df['close'].shift(-3) / df['close'] - 1 > 0.015).astype(int)
    return df

def fetch_all_futures_symbols():
    configs = client.configs_v3()
    perp = configs.get('data', {}).get('perpetualContract', [])
    return [item['symbol'] for item in perp if item.get('status') == 'TRADING']

def fetch_klines(symbol, interval=15, limit=500):
    end = int(time.time())
    start = end - interval * 60 * limit
    res = client.klines_v3(symbol=symbol, interval=interval, start=start, end=end, limit=limit)
    df = pd.DataFrame(res['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
    return df

def compute_indicators(df):
    df['ema21'] = EMAIndicator(df['close'], window=21).ema_indicator()
    df['rsi'] = RSIIndicator(df['close']).rsi()
    df['atr'] = AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
    return df

def fetch_fear_greed_index():
    try:
        url = "https://api.alternative.me/fng/"
        res = requests.get(url)
        res.raise_for_status()
        return int(res.json()['data'][0]['value'])
    except:
        return None

def train_model(df):
    df = compute_indicators(df)
    df = create_targets(df)
    df.dropna(inplace=True)
    features = ['ema21', 'rsi', 'atr']
    X = df[features]
    y = df['target']
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = XGBClassifier(use_label_encoder=False, eval_metric='logloss')
    model.fit(X_scaled, y)
    return model, scaler, features, (X_scaled, y)

def backtest_model(model, X_scaled, y_true):
    y_pred = model.predict(X_scaled)
    accuracy = (y_true == y_pred).mean()
    logging.info(f"Backtest Accuracy: {accuracy:.2%}")
    return accuracy

def generate_futures_signal(model, scaler, features, df, sentiment, profit_threshold=0.05):
    df = compute_indicators(df)
    df.dropna(inplace=True)
    latest = df.iloc[-1:]
    X = latest[features]
    X_scaled = scaler.transform(X)
    prob = model.predict_proba(X_scaled)[0][1]
    current_price = latest['close'].values[0]
    atr = latest['atr'].values[0]
    rsi = latest['rsi'].values[0]

    signal = 'hold'
    confidence = int(prob * 100 if prob >= 0.5 else (1 - prob) * 100)

    if prob > 0.5 and confidence >= CONFIDENCE_THRESHOLD and rsi < 70 and (sentiment is None or sentiment >= 40):
        signal = 'long'
        entry = current_price * 0.999
        sl = entry - atr * 1.5
        tp = entry + atr * 3
    elif prob < 0.5 and confidence >= CONFIDENCE_THRESHOLD and rsi > 30 and (sentiment is None or sentiment >= 40):
        signal = 'short'
        entry = current_price * 1.001
        sl = entry + atr * 1.5
        tp = entry - atr * 3
    else:
        return 'hold', confidence, current_price, None, None, None

    if abs(tp - entry) / entry < profit_threshold:
        return 'hold', confidence, current_price, entry, sl, tp

    return signal, confidence, current_price, entry, sl, tp

def place_futures_order(symbol, side, price, sl, tp):
    try:
        order = client_private.place_order_v3(
            symbol=symbol,
            orderType="Limit",
            side=side.upper(),
            price=round(price, 4),
            qty=1,
            reduceOnly=False,
            timeInForce="GTC"
        )
        return order['data']['orderId']
    except Exception as e:
        logging.error(f"Failed to place order for {symbol}: {e}")
        return None

# Flask Keepalive
app = Flask('')
@app.route('/')
def home():
    return "Futures Bot is running"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    Thread(target=run_flask).start()

async def run():
    symbols = fetch_all_futures_symbols()
    sentiment = fetch_fear_greed_index()
    logging.info(f"Fear & Greed Index: {sentiment}")

    best_trade = None
    summary = []

    for symbol in symbols:
        try:
            df = fetch_klines(symbol)
            model, scaler, features, (X_scaled, y_true) = train_model(df)
            accuracy = backtest_model(model, X_scaled, y_true)
            signal, confidence, price, entry, sl, tp = generate_futures_signal(model, scaler, features, df, sentiment)

            summary.append(f"{symbol}: {confidence}% - {signal.upper()} - Acc: {accuracy:.2%}")

            if signal != 'hold' and confidence >= CONFIDENCE_THRESHOLD:
                if best_trade is None or confidence > best_trade['confidence']:
                    best_trade = {
                        'symbol': symbol,
                        'signal': signal,
                        'confidence': confidence,
                        'price': price,
                        'entry': entry,
                        'sl': sl,
                        'tp': tp
                    }
        except Exception as e:
            logging.error(f"Error for {symbol}: {e}")

    if best_trade:
        symbol = best_trade['symbol']
        signal = best_trade['signal']
        confidence = best_trade['confidence']
        entry = best_trade['entry']
        sl = best_trade['sl']
        tp = best_trade['tp']
        price = best_trade['price']

        position = open_positions.get(symbol)

        if position is None:
            order_id = place_futures_order(symbol, signal, entry, sl, tp)
            if order_id:
                open_positions[symbol] = {'signal': signal, 'entry': entry, 'sl': sl, 'tp': tp, 'order_id': order_id}
                msg = f"\n📈 {symbol} FUTURES SIGNAL ({signal.upper()})\n"
                msg += f"Confidence: {confidence}%\nEntry: {entry}\nSL: {sl}\nTP: {tp}\nCurrent Price: {price}\nFear & Greed Index: {sentiment}"
                await send_alert(msg)
        else:
            updated = False
            if abs(position['tp'] - tp) / position['tp'] > TP_SL_UPDATE_THRESHOLD:
                position['tp'] = tp
                updated = True
            if abs(position['sl'] - sl) / position['sl'] > TP_SL_UPDATE_THRESHOLD:
                position['sl'] = sl
                updated = True

            if updated:
                try:
                    if 'order_id' in position:
                        client_private.cancel_order_v3(symbol=symbol, orderId=position['order_id'])
                        logging.info(f"Cancelled old order {position['order_id']} for {symbol}")
                except Exception as e:
                    logging.warning(f"Failed to cancel order: {e}")

                new_order_id = place_futures_order(symbol, signal, entry, sl, tp)
                if new_order_id:
                    position['order_id'] = new_order_id

                msg = f"\n🔄 {symbol} TP/SL UPDATED\n"
                msg += f"Signal: {signal.upper()}\nNew SL: {sl}\nNew TP: {tp}\nCurrent Price: {price}"
                await send_alert(msg)
            else:
                logging.info(f"{symbol}: Position unchanged. No TP/SL update needed.")
    else:
        logging.info("No trade met the 99% confidence threshold.")

    await send_alert("📊 Cycle Summary:\n" + "\n".join(summary))
    await asyncio.sleep(90)
    await run()

if __name__ == '__main__':
    keep_alive()
    asyncio.run(run())
