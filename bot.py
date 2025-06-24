import os
import asyncio
import aiohttp
from web3 import Web3
from web3.exceptions import TransactionNotFound
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import time

load_dotenv()

class EnhancedArbitrageBot:
    def __init__(self):
        self.BSC_RPCS = [
            "https://bsc-dataseed.binance.org/",
            "https://bsc-dataseed1.defibit.io/",
            "https://bsc-dataseed1.ninicoin.io/"
        ]
        self.web3 = self._setup_web3()
        self.private_key = os.getenv("PRIVATE_KEY")
        if not self.private_key:
            raise ValueError("PRIVATE_KEY environment variable not set.")
        self.account = self.web3.eth.account.from_key(self.private_key)
        self.address = self.account.address
        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.zerox_api_key = os.getenv("ZEROX_API_KEY")
        if not self.zerox_api_key:
            raise ValueError("ZEROX_API_KEY environment variable not set.")

        self.USDT = Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955")
        self.BUSD = Web3.to_checksum_address("0xe9e7cea3dedca5984780Bafc599bD69aDd087D56")
        self.SLIPPAGE = 0.002
        self.AMOUNT_USDT = 95 * 10**18
        self.MIN_PROFIT = 0.3 * 10**18

        self.session = None
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.gas_strategy = "medium"
        self.bnb_price = None

        self.ERC20_ABI = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]

    def _setup_web3(self):
        for rpc in self.BSC_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 5}))
                if w3.is_connected():
                    print(f"Connected to BSC RPC: {rpc}")
                    return w3
            except Exception as e:
                print(f"Could not connect to {rpc}: {e}")
                continue
        raise Exception("No RPC connection available")

    async def _get_bnb_price(self):
        url = "https://bsc.api.0x.org/swap/v1/price"
        params = {
            "sellToken": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "buyToken": self.USDT,
            "sellAmount": str(10**18)
        }
        headers = {
            "0x-api-key": self.zerox_api_key
        }
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(float(data['price']) * 1e18)
                else:
                    print(f"0x price API error: {resp.status} - {await resp.text()}")
        except Exception as e:
            print(f"Error getting BNB price: {e}")
        return None

    async def _update_gas_parameters(self):
        try:
            self.bnb_price = await self._get_bnb_price() or self.bnb_price or 300 * 10**18
            current_gas = self.web3.eth.gas_price
            try:
                block = self.web3.eth.get_block('latest')
                base_fee = block.baseFeePerGas if 'baseFeePerGas' in block else current_gas
            except Exception as e:
                print(f"Could not get baseFeePerGas, using current_gas: {e}")
                base_fee = current_gas

            if self.gas_strategy == "aggressive":
                self.gas_price = min(int(current_gas * 1.3), int(base_fee * 2), 20 * 10**9)
            else:
                self.gas_price = min(int(current_gas * 1.15), int(base_fee * 1.5), 10 * 10**9)
            print(f"Gas Price (Wei): {self.gas_price / 1e9:.2f} Gwei (Current: {current_gas / 1e9:.2f} Gwei)")
        except Exception as e:
            print(f"Error updating gas parameters: {e}")
            self.gas_price = self.web3.eth.gas_price

    async def _send_telegram(self, message):
        if not self.session or not self.telegram_token or not self.chat_id:
            print("Telegram not configured or session not active.")
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            async with self.session.post(url, data=data, timeout=5) as resp:
                if resp.status != 200:
                    response_text = await resp.text()
                    print(f"Telegram API error: {resp.status} - {response_text}")
        except Exception as e:
            print(f"Error sending Telegram message: {e}")

    async def _get_quote(self, sell_token, buy_token, sell_amount):
        if not self.session:
            return None
        url = "https://bsc.api.0x.org/swap/v1/quote"
        params = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": str(sell_amount),
            "slippagePercentage": self.SLIPPAGE,
            "takerAddress": self.address,
            "affiliateAddress": "0x0000000000000000000000000000000000000000",
            "gasPrice": str(self.gas_price)
        }
        headers = {
            "0x-api-key": self.zerox_api_key
        }
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error_text = await resp.text()
                    print(f"0x API error ({resp.status}): {error_text}")
        except Exception as e:
            print(f"Error getting 0x quote: {e}")
        return None

    def _get_token_balance(self, token_address):
        contract = self.web3.eth.contract(
            address=token_address,
            abi=self.ERC20_ABI
        )
        return contract.functions.balanceOf(self.address).call()

    def _execute_swap(self, quote):
        try:
            current_nonce = self.web3.eth.get_transaction_count(self.address)
            txn = {
                'from': quote['from'],
                'to': quote['to'],
                'data': quote['data'],
                'value': int(quote['value']),
                'gas': min(int(quote['gas']) * 13 // 10, 800000),
                'gasPrice': self.gas_price,
                'nonce': current_nonce,
            }
            signed = self.web3.eth.account.sign_transaction(txn, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"Sent transaction: {self.web3.to_hex(tx_hash)}")
            return self.web3.to_hex(tx_hash)
        except Exception as e:
            print(f"Error executing swap: {e}")
            raise e

    async def _wait_for_confirmation(self, tx_hash, timeout=45):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                receipt = self.web3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    print(f"Transaction {tx_hash} confirmed. Status: {receipt.status}")
                    return receipt.status == 1
                await asyncio.sleep(1.5)
            except TransactionNotFound:
                print(f"Transaction {tx_hash} not found yet...")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Error checking transaction receipt: {e}")
                await asyncio.sleep(2)
        print(f"Transaction {tx_hash} not confirmed within timeout.")
        return False

    async def _calculate_net_profit(self, profit_wei, tx1_gas_used, tx2_gas_used):
        if not self.bnb_price:
            self.bnb_price = await self._get_bnb_price() or 300 * 10**18
        total_gas_wei = (tx1_gas_used + tx2_gas_used) * self.gas_price
        gas_cost_usdt_wei = (total_gas_wei * self.bnb_price) // (10**18)
        return profit_wei - gas_cost_usdt_wei

    async def _check_arbitrage(self):
        print("\nChecking for arbitrage opportunities...")
        usdt_busd_task = self._get_quote(self.USDT, self.BUSD, self.AMOUNT_USDT)
        busd_usdt_task = self._get_quote(self.BUSD, self.USDT, self.AMOUNT_USDT)
        quote1, quote2 = await asyncio.gather(usdt_busd_task, busd_usdt_task)
        opportunities = []

        # Path: USDT -> BUSD -> USDT
        if quote1 and quote2:
            amount_out_usdt_to_busd = int(quote1['buyAmount'])
            final_quote_busd_to_usdt = await self._get_quote(self.BUSD, self.USDT, amount_out_usdt_to_busd)
            if final_quote_busd_to_usdt:
                final_amount_usdt = int(final_quote_busd_to_usdt['buyAmount'])
                gross_profit_usdt_wei = final_amount_usdt - self.AMOUNT_USDT
                estimated_gas_tx1 = int(quote1['gas'])
                estimated_gas_tx2 = int(final_quote_busd_to_usdt['gas'])
                net_profit_usdt_wei = await self._calculate_net_profit(
                    gross_profit_usdt_wei,
                    estimated_gas_tx1,
                    estimated_gas_tx2
                )
                profit_percent = (net_profit_usdt_wei / self.AMOUNT_USDT) * 100 if self.AMOUNT_USDT else 0
                opportunities.append({
                    "direction": "USDT→BUSD→USDT",
                    "quote1": quote1,
                    "quote2": final_quote_busd_to_usdt,
                    "net_profit_wei": net_profit_usdt_wei,
                    "gross_profit_wei": gross_profit_usdt_wei,
                    "profit_percent": profit_percent
                })

        # Path: BUSD -> USDT -> BUSD
        current_busd_balance = self._get_token_balance(self.BUSD)
        if quote2 and current_busd_balance >= self.AMOUNT_USDT:
            amount_out_busd_to_usdt = int(quote2['buyAmount'])
            final_quote_usdt_to_busd = await self._get_quote(self.USDT, self.BUSD, amount_out_busd_to_usdt)
            if final_quote_usdt_to_busd:
                final_amount_busd = int(final_quote_usdt_to_busd['buyAmount'])
                gross_profit_busd_wei = final_amount_busd - self.AMOUNT_USDT
                estimated_gas_tx1 = int(quote2['gas'])
                estimated_gas_tx2 = int(final_quote_usdt_to_busd['gas'])
                net_profit_busd_wei = await self._calculate_net_profit(
                    gross_profit_busd_wei,
                    estimated_gas_tx1,
                    estimated_gas_tx2
                )
                profit_percent = (net_profit_busd_wei / self.AMOUNT_USDT) * 100 if self.AMOUNT_USDT else 0
                opportunities.append({
                    "direction": "BUSD→USDT→BUSD",
                    "quote1": quote2,
                    "quote2": final_quote_usdt_to_busd,
                    "net_profit_wei": net_profit_busd_wei,
                    "gross_profit_wei": gross_profit_busd_wei,
                    "profit_percent": profit_percent
                })
        if not opportunities:
            print("No arbitrage opportunities found at this time.")
            return None, None, 0, None, 0
        best_opp = max(opportunities, key=lambda x: x["net_profit_wei"])
        return (best_opp["quote1"], best_opp["quote2"],
                best_opp["net_profit_wei"], best_opp["direction"],
                best_opp["profit_percent"])

    async def _execute_arbitrage(self, quote1, quote2, direction, net_profit_usd, profit_percent):
        try:
            await self._update_gas_parameters()
            telegram_message = (
                f"<b>🎯 Found Arbitrage!</b>\n"
                f"<b>Direction:</b> {direction}\n"
                f"<b>Est. Net Profit:</b> {net_profit_usd:.6f} USDT\n"
                f"<b>Profitability:</b> {profit_percent:.4f}%\n"
                f"Attempting execution..."
            )
            await self._send_telegram(telegram_message)
            tx1_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote1
            )
            if not await self._wait_for_confirmation(tx1_hash, 30):
                await self._send_telegram(
                    f"⚠️ <b>Arbitrage Failed (TX1)</b>\n"
                    f"Direction: {direction}\n"
                    f"TX1: <a href='https://bscscan.com/tx/{tx1_hash}'>{tx1_hash[:10]}...</a>\n"
                    f"Reason: TX1 not confirmed or failed."
                )
                return False
            tx2_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote2
            )
            if not await self._wait_for_confirmation(tx2_hash, 30):
                await self._send_telegram(
                    f"⚠️ <b>Arbitrage Failed (TX2)</b>\n"
                    f"Direction: {direction}\n"
                    f"TX1: <a href='https://bscscan.com/tx/{tx1_hash}'>{tx1_hash[:10]}...</a>\n"
                    f"TX2: <a href='https://bscscan.com/tx/{tx2_hash}'>{tx2_hash[:10]}...</a>\n"
                    f"Reason: TX2 not confirmed or failed."
                )
                return False
            tx1_link = f"https://bscscan.com/tx/{tx1_hash}"
            tx2_link = f"https://bscscan.com/tx/{tx2_hash}"
            await self._send_telegram(
                f"✅ <b>Arbitrage Completed!</b>\n"
                f"<b>Direction:</b> {direction}\n"
                f"<b>Net Profit:</b> {net_profit_usd:.6f} USDT\n"
                f"<b>Profitability:</b> {profit_percent:.4f}%\n"
                f"TX1: <a href='{tx1_link}'>View on BscScan</a>\n"
                f"TX2: <a href='{tx2_link}'>View on BscScan</a>"
            )
            return True
        except Exception as e:
            await self._send_telegram(f"❌ <b>Arbitrage Execution Error!</b>\nDirection: {direction}\nError: {str(e)}")
            print(f"Error during arbitrage execution: {e}")
            return False

    async def run(self):
    await self._send_telegram("🚀 <b>Enhanced Arbitrage Bot Started!</b>")
    try:
        print(f"Arbitrage Found: {direction} | Est. Net Profit: {net_profit_usd:.6f} USDT ({profit_percent:.4f}%)")
        await asyncio.sleep(10)
    except Exception as e:
        print(f"Main loop error: {e}")
        await asyncio.sleep(30)

if __name__ == "__main__":
    bot = EnhancedArbitrageBot()
    asyncio.run(bot.run())

