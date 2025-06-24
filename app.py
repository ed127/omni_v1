from flask import Flask
import threading
from bot import EnhancedArbitrageBot
import asyncio

app = Flask(__name__)

@app.route('/')
def home():
    return "Arbitrage Bot is running!"

def start_bot():
    bot = EnhancedArbitrageBot()
    asyncio.run(bot.run())

# Start the bot in a background thread when the server starts
threading.Thread(target=start_bot, daemon=True).start()
